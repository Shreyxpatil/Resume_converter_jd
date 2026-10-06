"""Bolds skill/keyword mentions inside the summary and bullet text so a
recruiter's skim lands directly on the skills that matter — a standard
resume-writing technique this app didn't do until now.

Deterministic (no LLM call): this is regex/BeautifulSoup over terms the
candidate has already listed themselves (their own Skills section) plus,
when available, JD-matched keywords — never a judgment call about what
"counts" as a skill, so there's nothing here to get wrong the way an LLM
guess could.
"""

import re

from bs4 import BeautifulSoup, NavigableString

# Only plain alnum+space terms are bolded — a term containing symbols (e.g.
# "C++", "Node.js") would need compute_keyword_match's substring-vs-boundary
# branching to match safely with regex word boundaries, which isn't worth
# the added complexity here for what's a purely cosmetic pass.
_SAFE_TERM_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 ]*$')

# Empty-row placeholders the table templates render ("NA") — never skills.
_PLACEHOLDER_VALUES = {"NA", "N/A", "NONE"}


def _collect_skill_terms(soup):
    terms = set()
    for el in soup.find_all(class_=re.compile(r'skill-line')):
        text = el.get_text(" ", strip=True)
        if ":" in text:
            text = text.split(":", 1)[1]
        for part in re.split(r'[,;•|/]+', text):
            clean = part.strip()
            if 2 <= len(clean) <= 40 and clean.upper() not in _PLACEHOLDER_VALUES:
                terms.add(clean)
    return terms


_SKILL_NORMALIZE_RE = re.compile(r'[^a-z0-9]')


def _normalize_skill_term(term):
    """Collapses case/spacing/hyphenation/simple-pluralization differences
    so "Vector Database" and "vector databases", or "HuggingFace
    Transformers" and "Hugging Face Transformers", compare equal."""
    normalized = _SKILL_NORMALIZE_RE.sub('', term.lower())
    if len(normalized) > 4 and normalized.endswith('s'):
        normalized = normalized[:-1]
    return normalized


def dedupe_skill_lines(html_content):
    """Removes duplicate/near-duplicate skill mentions across the resume's
    top Skills section. The same term sometimes ends up listed more than
    once — either the original extraction categorizing it under more than
    one heading (e.g. "Pinecone" under both "Vector Stores" and
    "Databases"), or repeated keyword-weaving passes each adding a skill
    without checking what's already listed elsewhere in a different
    case/spacing variant (e.g. "Vector Database" added when "vector
    databases" is already there). Deterministic — no LLM, so nothing here
    can misjudge what counts as a duplicate beyond simple normalization.

    Keeps the first occurrence, in whichever category it appeared first,
    and drops later duplicates; a category left with nothing is removed
    entirely rather than showing a bare label. Scoped to skill-line
    elements with a skill_N section id (the top Skills section) — per-
    entry "Tech Stack" lines under Experience/Projects are untouched,
    since overlapping tech across different real projects is expected,
    not a duplication bug."""
    soup = BeautifulSoup(html_content, "html.parser")
    changed = False
    seen = set()

    for el in soup.find_all(class_="skill-line"):
        section_id = el.get("data-section-id") or ""
        if not section_id.startswith("skill_"):
            continue

        strong_tag = el.find("strong")
        if strong_tag is None:
            continue
        text_node = strong_tag.next_sibling
        if text_node is None:
            continue

        original_text = str(text_node)
        kept = []
        for part in original_text.split(","):
            part = part.strip()
            if not part:
                continue
            key = _normalize_skill_term(part)
            if key in seen:
                changed = True
                continue
            seen.add(key)
            kept.append(part)

        if not kept:
            el.decompose()
            changed = True
            continue

        new_text = " " + ", ".join(kept)
        if new_text != original_text:
            changed = True
            text_node.replace_with(new_text)

    return str(soup) if changed else html_content


def bold_skill_mentions(html_content, extra_terms=None):
    """Wraps mentions of the candidate's own skills (from the Skills
    section) and any extra_terms (e.g. matched JD keywords) in <strong>,
    within the summary and bullet text only. Returns the modified HTML
    unchanged if there's nothing to bold. Idempotent — text already inside
    a <strong> or <a> is skipped, so calling this again on already-bolded
    HTML won't double-wrap anything."""
    soup = BeautifulSoup(html_content, "html.parser")

    terms = _collect_skill_terms(soup)
    for t in extra_terms or []:
        t = (t or "").strip()
        if 2 <= len(t) <= 40:
            terms.add(t)

    safe_terms = sorted((t for t in terms if _SAFE_TERM_RE.match(t)), key=len, reverse=True)
    if not safe_terms:
        return html_content

    pattern = re.compile(r'\b(' + '|'.join(re.escape(t) for t in safe_terms) + r')\b', re.IGNORECASE)

    targets = []
    summary = soup.find(class_="summary-text")
    if summary:
        targets.append(summary)
    targets.extend(soup.find_all("li"))

    for el in targets:
        _bold_matches_in_element(el, pattern, soup)

    return str(soup)


def _bold_matches_in_element(el, pattern, soup):
    for text_node in list(el.find_all(string=True)):
        parent_name = getattr(text_node.parent, "name", None)
        if parent_name in ("strong", "a"):
            continue

        original = str(text_node)
        if not pattern.search(original):
            continue

        pieces = pattern.split(original)
        # re.split with a capturing group interleaves [before, match, after, match, ...]
        # — odd indices are the matched terms, everything else is plain text.
        new_nodes = []
        for i, piece in enumerate(pieces):
            if not piece:
                continue
            if i % 2 == 1:
                strong_tag = soup.new_tag("strong")
                strong_tag.string = piece
                new_nodes.append(strong_tag)
            else:
                new_nodes.append(NavigableString(piece))

        if new_nodes:
            text_node.replace_with(*new_nodes)
