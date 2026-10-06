import re
from bs4 import BeautifulSoup

from src import ai_client


def edit_section(html_content, section_id, instruction):
    """Applies a free-text instruction to ONE section of an already-rendered resume
    HTML doc (identified by its data-section-id), leaving everything else untouched."""
    soup = BeautifulSoup(html_content, "html.parser")
    section = soup.find(attrs={"data-section-id": section_id})
    if section is None:
        raise Exception("Section not found — the preview may be out of date, try reloading it.")

    section_html = str(section)

    prompt = f"""You are editing ONE section of an HTML resume document. Below is that section's HTML fragment in isolation. Apply ONLY the following instruction to it:

INSTRUCTION: "{instruction}"

RULES:
- Return ONLY the modified HTML fragment — same root tag name, and keep its "data-section-id" attribute and existing class attribute(s) exactly as given. Only the content inside (and other attributes the instruction targets, e.g. a href) should change.
- Do not add commentary, markdown fences, or explanations — output raw HTML only, starting directly with the opening tag.
- Do not touch anything unrelated to the instruction — do not rewrite bullets/text the instruction didn't ask about.
- Never wrap any word in markdown syntax like **bold** or `code` — this is rendered as literal text in a Word document.
- If the instruction is unrelated to this section's content or impossible to apply here, make the smallest reasonable change that satisfies it, or leave the fragment unchanged if nothing sensible applies.

SECTION HTML:
{section_html}
"""
    # This powers an interactive "Tell AI" edit — the user is watching a spinner, so
    # latency matters more than for bulk resume extraction. Groq is fast and currently
    # reliable, so try it first; only fall back to working through the Gemini list
    # (often several quota-exhausted attempts) if Groq itself fails.
    response = ai_client.generate_with_ai_fallback(prompt, models=ai_client.GEMINI_MODELS_TO_TRY, groq_first=True)
    new_fragment = response.text.strip()
    new_fragment = re.sub(r'^```html\s*|^```\s*|\s*```$', '', new_fragment, flags=re.MULTILINE).strip()

    if not new_fragment:
        raise Exception("AI returned an empty result — no changes applied.")

    new_section = BeautifulSoup(new_fragment, "html.parser")
    top_tags = [c for c in new_section.contents if getattr(c, "name", None)]
    if top_tags:
        top_tags[0]["data-section-id"] = section_id

    section.replace_with(*new_section.contents)
    return str(soup)


def add_section(html_content, reference_section_id, instruction):
    """Clones the reference section's HTML structure and asks the AI to rewrite it into
    a brand-new entry per the instruction, inserting it right after the reference with a
    fresh, unique data-section-id — used for "+ Add Experience/Project/..." buttons."""
    soup = BeautifulSoup(html_content, "html.parser")
    reference = soup.find(attrs={"data-section-id": reference_section_id})
    if reference is None:
        raise Exception("Reference section not found — the preview may be out of date, try reloading it.")

    prefix_match = re.match(r'^(.+)_(\d+)$', reference_section_id)
    if not prefix_match:
        raise Exception("This section can't be duplicated.")
    prefix = prefix_match.group(1)

    existing_nums = []
    for el in soup.find_all(attrs={"data-section-id": True}):
        m = re.match(rf'^{re.escape(prefix)}_(\d+)$', el.get("data-section-id", ""))
        if m:
            existing_nums.append(int(m.group(1)))
    new_id = f"{prefix}_{(max(existing_nums) + 1) if existing_nums else 0}"

    reference_html = str(reference)

    prompt = f"""You are adding a brand-new entry to a resume, modeled structurally on the example entry below. The example is a STRUCTURAL PATTERN ONLY — do not reuse its actual content, dates, or company/project names.

WHAT THE NEW ENTRY SHOULD BE ABOUT: "{instruction}"

RULES:
- Return ONLY the new entry's HTML — same tag name, structure, and CSS classes as the example (adapt the number of bullets/rows to fit the new content naturally).
- Set the root element's "data-section-id" attribute to "{new_id}".
- Write complete, plausible, well-written resume content for every field — do not leave placeholders or copy the example's text verbatim.
- Do not add commentary, markdown fences, or explanations — output raw HTML only, starting directly with the opening tag.
- Never wrap any word in markdown syntax like **bold** — this is rendered as literal text in a Word document.

EXAMPLE ENTRY (structure reference only — do not reuse its content):
{reference_html}
"""
    # This powers an interactive "Tell AI" edit — the user is watching a spinner, so
    # latency matters more than for bulk resume extraction. Groq is fast and currently
    # reliable, so try it first; only fall back to working through the Gemini list
    # (often several quota-exhausted attempts) if Groq itself fails.
    response = ai_client.generate_with_ai_fallback(prompt, models=ai_client.GEMINI_MODELS_TO_TRY, groq_first=True)
    new_fragment = response.text.strip()
    new_fragment = re.sub(r'^```html\s*|^```\s*|\s*```$', '', new_fragment, flags=re.MULTILINE).strip()

    if not new_fragment:
        raise Exception("AI returned an empty result — nothing added.")

    new_section = BeautifulSoup(new_fragment, "html.parser")
    top_tags = [c for c in new_section.contents if getattr(c, "name", None)]
    if top_tags:
        top_tags[0]["data-section-id"] = new_id

    last = reference
    for node in list(new_section.contents):
        last.insert_after(node)
        last = node

    return str(soup)
