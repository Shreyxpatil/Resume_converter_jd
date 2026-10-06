"""Verifies the generated DOCX actually round-trips to readable, correctly
ordered text the way a real ATS parser would extract it — not just that it
looks right visually in Word.

Confirmed concretely, not hypothetical: `file_extract`'s DOCX text
extraction reads every paragraph first, then appends all table text
afterward — it does not interleave them in document order. Both templates
here (General and Salesforce) are table-based company formats, so a
paragraph placed between tables (e.g. the "Projects" title bar) is read
before the table content above it, and any content split between
paragraphs and tables can come out detached from the rows it belongs to —
exactly the kind of naive-DOCX-parsing failure real ATS backends built on
similar libraries (or docx2txt, which drops table text entirely) are known
to hit. This module measures that on the actual exported file.

Deterministic, no LLM call. Runs
read-only against an already-generated DOCX; never blocks or modifies it.
"""

from bs4 import BeautifulSoup

from src.file_extract import extract_text_from_docx_path
from src.logging_config import get_logger

logger = get_logger(__name__)

# The candidate's name should be at (or very near) the top of any linear
# reading of the document. Finding it deep into the extracted text is a
# direct, simple signal that table content is being read out of order.
NAME_POSITION_RATIO_THRESHOLD = 0.15

# Below this fraction of the source resume's word count surviving the
# round-trip, treat it as a content-loss signal rather than formatting noise.
WORD_COUNT_PARITY_THRESHOLD = 0.7


def check_docx_parseability(docx_path, source_html):
    """Returns a list of issue dicts (same shape as the app's other
    issues) describing ATS-parseability risks found by re-extracting the
    generated DOCX and comparing its actual reading order against the
    source HTML it was built from. Never raises — a failure to even read
    the DOCX becomes its own (informational) issue rather than propagating."""
    try:
        extracted_text = extract_text_from_docx_path(docx_path)
    except Exception as e:
        logger.warning(f"ATS parseability check failed to re-read {docx_path}: {e}")
        return [{
            "category": "ats_parseability",
            "severity": "medium",
            "section_id": None,
            "message": "Could not verify the exported DOCX re-parses cleanly.",
            "suggestion": "Open the DOCX in Word to confirm it looks correct before submitting it.",
            "ai_instruction": None,
        }]

    extracted_lower = extracted_text.lower()
    total_len = len(extracted_lower) or 1
    soup = BeautifulSoup(source_html, "html.parser")
    issues = []

    name_issue = _check_name_position(soup, extracted_lower, total_len)
    if name_issue:
        issues.append(name_issue)

    disordered = _find_entries_with_bullets_before_title(soup, extracted_lower)
    if disordered:
        issues.append({
            "category": "ats_parseability",
            "severity": "high",
            "section_id": None,
            "message": f'{len(disordered)} entr{"y" if len(disordered) == 1 else "ies"} (e.g. "{disordered[0]}") have their role/company/dates appearing AFTER their own bullet points when the DOCX is read as plain text — a linear-reading ATS would see the bullets with no attached context.',
            "suggestion": "This is a direct consequence of the 2-column table layout used for role/duration rows — some ATS parsers read all paragraph text before any table text.",
            "ai_instruction": None,
        })

    source_word_count = len(soup.get_text(" ", strip=True).split())
    extracted_word_count = len(extracted_text.split())
    if source_word_count > 0 and extracted_word_count < source_word_count * WORD_COUNT_PARITY_THRESHOLD:
        issues.append({
            "category": "ats_parseability",
            "severity": "medium",
            "section_id": None,
            "message": f"The exported DOCX re-extracts to only {extracted_word_count} words vs. {source_word_count} in the resume itself — some content may not be machine-readable at all.",
            "suggestion": "Some ATS systems will see meaningfully less of this resume than what's visible in Word.",
            "ai_instruction": None,
        })

    return issues


def _extract_name_text(soup):
    name_el = soup.find(class_="name")
    return name_el.get_text(strip=True) if name_el else ""


def _check_name_position(soup, extracted_lower, total_len):
    name_text = _extract_name_text(soup)
    if not name_text:
        return None

    pos = extracted_lower.find(name_text.lower())
    if pos == -1:
        return {
            "category": "ats_parseability",
            "severity": "high",
            "section_id": None,
            "message": "Candidate name isn't found at all when the exported DOCX is re-parsed as plain text.",
            "suggestion": "Open the DOCX in Word to confirm the header rendered correctly.",
            "ai_instruction": None,
        }

    ratio = pos / total_len
    if ratio > NAME_POSITION_RATIO_THRESHOLD:
        return {
            "category": "ats_parseability",
            "severity": "high",
            "section_id": None,
            "message": f"Candidate name is found {round(ratio * 100)}% of the way into the document when read as plain text, instead of at the very top — a linear-reading ATS may lose track of whose resume this is.",
            "suggestion": "This happens because name/contact info sits inside a table while the rest of the resume is plain paragraphs — some parsers read all paragraph text before any table content.",
            "ai_instruction": None,
        }
    return None


def _find_entries_with_bullets_before_title(soup, extracted_lower):
    """For each entry-block, checks whether its title's position in the
    extracted text comes AFTER its own first bullet's position — the
    direct, measurable version of "role/dates got detached from the work
    that follows them"."""
    disordered = []
    for block in soup.find_all(class_="entry-block"):
        title_el = block.find(class_="entry-title")
        first_bullet = block.find("li")
        if not title_el or not first_bullet:
            continue

        title_text = title_el.get_text(strip=True)
        bullet_text = first_bullet.get_text(strip=True)
        if not title_text or not bullet_text:
            continue

        title_pos = extracted_lower.find(title_text.lower())
        bullet_pos = extracted_lower.find(bullet_text.lower()[:60])
        if title_pos != -1 and bullet_pos != -1 and title_pos > bullet_pos:
            disordered.append(title_text)

    return disordered
