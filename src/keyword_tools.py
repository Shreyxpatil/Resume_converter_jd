import re
import json
from bs4 import BeautifulSoup

from src import ai_client
from src.logging_config import get_logger

logger = get_logger(__name__)


def _clean_json_response(text):
    cleaned = re.sub(r'^```json\s*|^```\s*|\s*```$', '', text, flags=re.MULTILINE)
    return cleaned.strip()


VALID_PRIORITIES = ("must-have", "nice-to-have")


def extract_jd_keywords(job_description):
    """Returns a deduped list of {"term": ..., "priority": "must-have"|"nice-to-have"}
    for atomic skill/tool/technology names explicitly named in the JD.

    Priority lets the UI (and the overall match score) weight a missing
    required skill much worse than a missing "nice to have" one — a resume
    can be near-perfect on keyword count and still be missing the one thing
    that actually gets it rejected.
    """
    if not job_description or not job_description.strip():
        return []

    prompt = f"""Extract ONLY the specific tool, technology, platform, framework, library, programming language, certification, and technical skill names explicitly mentioned in this job description.

For each one, classify it as "must-have" (the JD states or strongly implies it's required — e.g. "required", "must have", listed as a core qualification/responsibility) or "nice-to-have" (stated as "preferred", "a plus", "bonus", "nice to have", or only mentioned in passing). If genuinely unclear, use "nice-to-have".

Output a JSON array of objects, using the JD's own wording for each term:
[{{"term": "Python", "priority": "must-have"}}, {{"term": "Docker", "priority": "nice-to-have"}}]

Do NOT include experience durations (e.g. "5+ years"), soft skills (e.g. "communication", "teamwork"), or generic phrases (e.g. "problem solving", "best practices"). Only concrete, nameable skills/tools/technologies/certifications.
No duplicates. Output ONLY the JSON array, no explanations, no markdown fences.

JOB DESCRIPTION:
\"\"\"
{job_description}
\"\"\"
"""
    response = ai_client.generate_with_ai_fallback(prompt, models=ai_client.GEMINI_MODELS_TO_TRY, groq_first=False)
    cleaned = _clean_json_response(response.text)
    try:
        raw_items = json.loads(cleaned)
    except Exception:
        # Fallback if the model's JSON is malformed: first try to salvage "term"
        # values out of {"term": "..."} pairs; if none are found (e.g. it ignored
        # the object format and returned a flat string array), fall back further
        # to treating every quoted string as a bare term.
        term_matches = re.findall(r'"term"\s*:\s*"([^"]+)"', cleaned)
        raw_items = [
            {"term": t} for t in (term_matches or re.findall(r'"([^"]+)"', cleaned))
        ]

    seen = set()
    result = []
    for item in raw_items:
        if isinstance(item, dict):
            term = str(item.get("term", "")).strip()
            priority = item.get("priority")
        else:
            term = str(item).strip()
            priority = None
        priority = priority if priority in VALID_PRIORITIES else "nice-to-have"
        key = term.lower()
        if term and key not in seen and len(term) <= 60:
            seen.add(key)
            result.append({"term": term, "priority": priority})
    return result


def extract_resume_keywords(html_content):
    """Extracts skills and tools explicitly mentioned in skills/expertise sections of the resume HTML."""
    if not html_content:
        return []

    soup = BeautifulSoup(html_content, "html.parser")
    skills = set()

    # 1. From class names matching skill/expertise/tech/badge/pill
    skill_class_re = re.compile(r'skill|expertise|tech|badge|pill', re.I)
    for el in soup.find_all(class_=skill_class_re):
        # Skip wrappers (e.g. the General template's expertise-category /
        # skill-list) whose children are matched on their own — reading the
        # wrapper's flattened text would glue a category heading and several
        # "Area: tools" lines into one junk "skill".
        if el.find(class_=skill_class_re):
            continue
        text = el.get_text(" ", strip=True)
        if ":" in text:
            text = text.split(":", 1)[1]
        for part in re.split(r'[,;•|/]+', text):
            clean = part.strip()
            if 2 <= len(clean) <= 45 and clean.upper() not in ("NA", "N/A") and not clean.lower().startswith(('http', 'click', 'experience', 'project', 'duration', 'role')):
                skills.add(clean)

    # 2. From table rows
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) >= 2:
            label = tds[0].get_text().lower()
            if any(k in label for k in ["expertise", "skill", "development tool", "database", "technolog"]):
                val = tds[1].get_text(" ", strip=True)
                for part in re.split(r'[,;•|/]+', val):
                    clean = part.strip()
                    if ":" in clean:
                        clean = clean.split(":", 1)[1].strip()
                    if 2 <= len(clean) <= 45 and clean.upper() not in ("NA", "N/A") and not clean.lower().startswith(('http', 'click')):
                        skills.add(clean)

    return sorted(list(skills), key=lambda x: x.lower())


def compute_keyword_match(html_content, keywords):
    """Splits keywords into (added, missing) based on whether they appear in the
    rendered resume text. Each item in `keywords` may be a plain string or a
    {"term": ..., "priority": ...} dict (from extract_jd_keywords) — either
    way the same shape is returned back in added/missing."""
    if not keywords:
        return [], []

    soup = BeautifulSoup(html_content, "html.parser")
    text = soup.get_text(" ").lower()

    # Normalize whitespace
    text = " " + re.sub(r'\s+', ' ', text) + " "

    added, missing = [], []
    for kw in keywords:
        term = kw["term"] if isinstance(kw, dict) else kw
        term_clean = term.strip().lower()
        if not term_clean:
            continue
        # Use boundary match if standard alphanumeric; substring if contains symbols like +, #, ., /
        if re.search(r'^[a-z0-9\s]+$', term_clean):
            pattern = r'\b' + re.escape(term_clean) + r'\b'
            found = bool(re.search(pattern, text))
        else:
            found = term_clean in text
        (added if found else missing).append(kw)

    return added, missing


def apply_keyword_changes(html_content, add_keywords, remove_keywords, custom_skills):
    """Uses Gemini to naturally weave in / strip out specific keywords from an already-rendered resume HTML, preserving structure."""
    all_additions = list(dict.fromkeys([*(add_keywords or []), *(custom_skills or [])]))
    all_removals = list(dict.fromkeys(remove_keywords or []))

    if not all_additions and not all_removals:
        return html_content

    instructions = []
    if all_additions:
        kw_list = ", ".join(all_additions)
        instructions.append(f"""ADD these keywords/skills: {kw_list}

For EACH keyword in that list, you MUST do BOTH steps below:
  (a) MANDATORY — Pick the single most relevant existing project/experience entry (based on its current tech stack/domain) and add a NEW bullet, or naturally expand an existing <li> bullet, inside that project's description list, written as one fluent natural sentence describing the candidate doing real work with it.
  (b) Also add it to the closest matching skills/expertise list AND that same project's tech stack line, for ATS keyword coverage.
Never wrap any word in markdown syntax like **bold**.""")

    if all_removals:
        kw_list = ", ".join(all_removals)
        instructions.append(
            f"REMOVE all mentions of these keywords/skills: {kw_list}. "
            "Delete them from skills/expertise lists and tech stacks, and rewrite any project bullet sentence that "
            "mentioned them so it still reads smoothly and naturally without that technology."
        )

    numbered = "\n\n".join(f"{i + 1}. {ins}" for i, ins in enumerate(instructions))
    prompt = f"""You are editing an existing HTML resume document. Below is the full HTML. Apply ONLY these changes:

{numbered}

RULES:
- Return the COMPLETE modified HTML document with the exact same structure/tags/CSS/classes as given — only text content changes.
- Do not touch the candidate's name, job titles, company/project names, dates, or any content unrelated to the requested changes.
- Do not add commentary, markdown fences, or explanations — output raw HTML only, starting with <!DOCTYPE or <html>.
- BEFORE you finalize your answer, re-check every keyword being added: does it appear inside at least one project's <li> bullet, as part of a full sentence — not only inside a skills list? If any keyword is missing from a project bullet, add it there before returning your answer.

HTML DOCUMENT:
{html_content}
"""
    response = ai_client.generate_with_ai_fallback(prompt, models=ai_client.GEMINI_MODELS_TO_TRY, groq_first=False)
    new_html = response.text.strip()
    new_html = re.sub(r'^```html\s*|^```\s*|\s*```$', '', new_html, flags=re.MULTILINE).strip()

    if len(new_html) < len(html_content) * 0.5 or "<body" not in new_html.lower():
        raise Exception("Keyword update response looked incomplete/invalid — no changes applied.")

    return new_html


def condense_resume_to_fit_pages(html_content, required_keywords=None):
    """User-triggered ("Fit to 2 pages" button) tightening pass — shortens
    bullet/summary WORDING across the whole resume so it prints shorter,
    without deleting any bullet, section, or entry, and without dropping
    any of the given required_keywords (the skills already present in the
    Skills section). Distinct from the app's automatic typography-only
    shrink (each template converter's COMPACT_LEVELS), which only changes
    font/margins, never wording — this changes wording, never structure or
    facts. Same guardrail as apply_keyword_changes: never invents, never
    removes a real accomplishment/number, only tightens phrasing."""
    required = ", ".join(required_keywords or []) or "(none specified — keep every skill/technology already mentioned)"

    prompt = f"""You are editing an existing HTML resume document to make it noticeably shorter so it prints on fewer pages, WITHOUT cutting anything essential.

STRICT RULES:
- Do NOT delete any bullet point, section, job entry, or project entry — every experience/project/bullet currently present must still be present, just written more concisely.
- Do NOT remove any of these skills/keywords, wherever they currently appear in the document: {required}
- Tighten each bullet's wording: shorter sentences, cut filler words, merge redundant phrasing — while keeping every concrete fact, technology name, and metric/number intact.
- Do NOT invent, exaggerate, or omit any real accomplishment, number, or fact that's currently stated.
- Return the COMPLETE modified HTML document with the exact same structure/tags/CSS/classes as given — only text content changes.
- Do not add commentary, markdown fences, or explanations — output raw HTML only, starting with <!DOCTYPE or <html>.

HTML DOCUMENT:
{html_content}
"""
    response = ai_client.generate_with_ai_fallback(prompt, models=ai_client.GEMINI_MODELS_TO_TRY, groq_first=False)
    new_html = response.text.strip()
    new_html = re.sub(r'^```html\s*|^```\s*|\s*```$', '', new_html, flags=re.MULTILINE).strip()

    if len(new_html) < len(html_content) * 0.3 or "<body" not in new_html.lower():
        raise Exception("Condense response looked incomplete/invalid — no changes applied.")

    return new_html
