"""Shared pieces of resume extraction used by both template parsers
(resume_parser_general.py and resume_parser_salesforce.py).

Each template asks the AI for its own JSON schema and maps the result onto
its own Jinja2 variables — everything else (honesty rules, JD tailoring,
extra user instructions, the Gemini/Groq fallback call, JSON cleanup, and
rendering) is identical, so it lives here once.
"""

import json
import re
from difflib import SequenceMatcher

from jinja2 import Template

from src import ai_client
from src.file_extract import extract_text_from_path
from src.logging_config import get_logger

logger = get_logger(__name__)

TOP_KEY = "Extract and Synthesize Candidate's Resume"

# Appended to every template's own system prompt. Rules A-C always apply;
# the description rule depends on whether a JD was given (see build_prompt):
# without one, project points are copied word for word; with one, they're
# rewritten to fit it — but A-C still forbid inventing facts or dropping points.
HONESTY_RULES = (
    "HARD RULES (apply to every field, and override any other instruction below, including JD tailoring):\n"
    "A. NEVER invent or alter factual/discrete fields: total years of experience, certifications, employment "
    "dates, company names, and project names must always be taken directly and only from the source resume "
    "text. Never write a years-of-experience figure or a seniority word (e.g. 'Senior', 'Lead') anywhere "
    "unless the candidate's real experience/title supports it — even if the target JD asks for more.\n"
    "B. NEVER drop or omit a bullet point, achievement, or project/experience entry that appears in the source "
    "resume — every point the candidate wrote must appear in the output.\n"
    "C. PLAIN TEXT ONLY: never wrap any word in markdown syntax like **bold**, *italic*, or `code` — this text "
    "is rendered as-is into a Word document.\n"
)

# Used only when NO job description is given.
VERBATIM_DESCRIPTIONS_RULE = (
    "D. PROJECT DESCRIPTIONS ARE COPIED VERBATIM: every 'description' bullet must be the candidate's own text, "
    "word for word, exactly as it appears in the source resume — same wording, same order, no rephrasing, "
    "shortening, summarizing, merging, or added keywords. Keep projects in the same order as the resume. You "
    "may only split a paragraph into its existing sentences, and strip bullet symbols like '•' or '-'."
)


def clean_json_response(text):
    text = re.sub(r"^```json\s*|\s*```$", "", (text or "").strip(), flags=re.MULTILINE)
    return text.strip()


def get_jd_alignment_prompt(job_description):
    return (
        "\n\nJD TAILORING MODE — rewrite the resume to fit this job description:\n"
        "This OVERRIDES any 'extract as-is / do not rephrase / do not summarize / exactly as written' rule above "
        "for the project descriptions and every other descriptive section — but never the HARD RULES.\n"
        "1. PROJECT DESCRIPTIONS: rewrite every bullet so it speaks to this JD — lead with the JD-relevant part of "
        "the work, use the JD's own terminology for things the candidate actually did, and highlight matching "
        "responsibilities, outcomes and technologies. Keep every bullet (HARD RULE B) and every real number, "
        "metric and technology in it; write each as one fluent sentence, never a bare keyword list. Where the "
        "candidate's work genuinely involved a JD technology or practice, name it explicitly.\n"
        "2. ORDER: put the most JD-relevant projects first, and the most JD-relevant skills first in every "
        "skills/expertise field.\n"
        "3. OTHER SECTIONS: phrase the skills/expertise categories and the current job role wording to match the "
        "JD's vocabulary, using only skills and titles the resume genuinely supports.\n"
        "4. Never claim experience, certifications, dates, companies or project names the resume doesn't show "
        "(HARD RULE A) — if the JD asks for something the candidate lacks, leave it out; the app lists missing "
        "JD keywords separately so the user can decide.\n"
        "Keep the exact JSON schema and field names unchanged.\n"
        f"TARGET JOB DESCRIPTION:\n\"\"\"\n{job_description}\n\"\"\"\n"
    )


def get_extra_instructions_prompt(extra_instructions):
    return (
        "\n\nADDITIONAL USER INSTRUCTIONS:\n"
        f"\"\"\"\n{extra_instructions}\n\"\"\"\n"
        "Follow these together with all the rules above; the JSON schema, field names and HARD RULES still apply."
    )


def build_prompt(system_prompt, user_prompt, job_description=None, extra_instructions=None):
    has_jd = bool(job_description and job_description.strip())
    rules = HONESTY_RULES if has_jd else HONESTY_RULES + VERBATIM_DESCRIPTIONS_RULE
    prompt = f"{system_prompt}\n\n{rules}\n\n{user_prompt}\n\nReturn ONLY valid JSON - no markdown, no extra text."
    if has_jd:
        prompt += get_jd_alignment_prompt(job_description.strip())
    if extra_instructions and extra_instructions.strip():
        prompt += get_extra_instructions_prompt(extra_instructions.strip())
    return prompt


_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
JSON_ATTEMPTS = 2  # the model occasionally returns malformed JSON; one retry usually fixes it


def parse_ai_json(text):
    """Parses the model's JSON, tolerating the one malformation it actually
    produces in practice — a trailing comma before } or ] (seen in a real
    run as "Expecting value" at the start of a line). Raises ValueError if
    the text still isn't valid JSON."""
    cleaned = clean_json_response(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return json.loads(_TRAILING_COMMA_RE.sub(r"\1", cleaned))


def request_resume_json(prompt, resume_path):
    """Sends the prompt plus the resume's plain text through the shared
    Gemini→Groq fallback chain and returns (data, resume_text): the parsed
    top-level data dict (the object under TOP_KEY) — or None if the response
    still wasn't usable JSON after JSON_ATTEMPTS tries — plus the source
    text, for restore_verbatim_bullets. Raises if every AI provider failed —
    that's a config/quota problem the caller should surface, not a parsing
    failure."""
    resume_text = extract_text_from_path(resume_path)
    full_prompt = f"{prompt}\n\nRESUME CONTENT:\n\n{resume_text}"

    for attempt in range(1, JSON_ATTEMPTS + 1):
        try:
            response = ai_client.generate_with_ai_fallback(full_prompt, models=ai_client.GEMINI_MODELS_TO_TRY, groq_first=False)
        except ValueError as e:
            logger.error(f"Parser config error: {e}")
            return None, resume_text
        except Exception as e:
            raise Exception("All AI models failed, check API key, quota, or network connection.") from e

        try:
            parsed = parse_ai_json(response.text)
        except ValueError as e:
            logger.warning(f"AI returned invalid JSON (attempt {attempt}/{JSON_ATTEMPTS}): {e}")
            continue

        if isinstance(parsed, dict):
            data = parsed.get(TOP_KEY, parsed)
            if isinstance(data, dict):
                return data, resume_text
        logger.warning(f"AI JSON had the wrong shape (attempt {attempt}/{JSON_ATTEMPTS})")

    logger.error("Giving up: AI never returned usable resume JSON.")
    return None, resume_text


# Bullet symbols, including the private-use glyphs Word's Symbol/Wingdings
# bullets turn into when a PDF is text-extracted (e.g. U+F0B7).
_BULLET_PREFIX_RE = re.compile(r"^\s*(?:[-–—•●▪◦*·➢►▶✓✔■□○◆❖➤\ue000-\uf8ff]|\d{1,2}[.)])\s*(?=\S)")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")
_MIN_RESTORE_WORDS = 4      # shorter bullets are too ambiguous to locate
_MIN_MATCH_RATIO = 0.6      # word-sequence similarity needed to accept a source match


def _norm_words(text):
    return [w for w in (re.sub(r"[^\w%+#]", "", t.lower()) for t in text.split()) if w]


_MAX_LINES_PER_BULLET = 8   # a PDF can wrap one long bullet over several lines


def _source_units(resume_text):
    """Every stretch of the candidate's own text a bullet could have come
    from: each run of up to _MAX_LINES_PER_BULLET consecutive lines (a PDF
    wraps long bullets; headings sit right above them), plus each run's
    individual sentences (a paragraph may be split into one point per
    sentence). Runs never cross a blank line. The closest one wins."""
    blocks, current = [], []
    for raw in (resume_text or "").splitlines():
        line = _BULLET_PREFIX_RE.sub("", raw.strip()).strip()
        if line:
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)

    candidates = set()
    for lines in blocks:
        for start in range(len(lines)):
            for end in range(start + 1, min(len(lines), start + _MAX_LINES_PER_BULLET) + 1):
                run = " ".join(lines[start:end])
                candidates.add(run)
                candidates.update(_SENTENCE_SPLIT_RE.split(run))
    return [(c, _norm_words(c)) for c in sorted(candidates) if c]  # sorted: ties resolve the same way every run


def restore_verbatim_bullets(bullets, resume_text):
    """Deterministic backstop for VERBATIM_DESCRIPTIONS_RULE (no-JD runs
    only — with a JD the bullets are meant to be rewritten): maps every description
    bullet the AI returned back onto the candidate's own words in the source
    resume text, so a paraphrase, trimmed word or added keyword never reaches
    the output. Bullets that can't be located confidently (too short, or no
    close enough source bullet/sentence) are kept as the AI returned them,
    minus any bullet symbol."""
    units = _source_units(resume_text)
    restored = []
    for bullet in bullets:
        text = _BULLET_PREFIX_RE.sub("", bullet).strip()
        words = _norm_words(text)
        best, best_ratio = None, 0.0
        if len(words) >= _MIN_RESTORE_WORDS:
            for unit_text, unit_words in units:
                ratio = SequenceMatcher(None, unit_words, words, autojunk=False).ratio()
                if ratio > best_ratio:
                    best, best_ratio = unit_text, ratio
        restored.append(best if best is not None and best_ratio >= _MIN_MATCH_RATIO else text)
    return restored


def as_text_list(value):
    """Normalizes a field the model may return as a list, a newline-joined
    string, or a single string into a list of non-empty strings."""
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [line.strip() for line in value.split("\n") if line.strip()]
    return []


def join_or_na(value):
    """Comma-joins a list field (or passes a string through), using "NA"
    for empty values — the convention both table templates display."""
    if isinstance(value, list):
        items = [str(v).strip() for v in value if str(v).strip()]
        return ", ".join(items) if items else "NA"
    value = str(value or "").strip()
    return value or "NA"


def render_template(template_file_path, output_html_path, template_vars):
    with open(template_file_path, "r", encoding="utf-8") as f:
        template_str = f.read()
    # autoescape: resume/JD/AI text is untrusted — it must render as text in
    # the preview, never as HTML/script (e.g. a resume containing "<img onerror=…>").
    rendered_html = Template(template_str, autoescape=True).render(**template_vars)
    with open(output_html_path, "w", encoding="utf-8") as f:
        f.write(rendered_html)
