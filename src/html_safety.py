"""Strips executable content from AI-returned resume HTML.

The section editor, keyword weaving and fit-to-2-pages features ask the AI
to return HTML, which is then shown in the same-origin preview. Resume and
JD text is untrusted, so a crafted resume could steer the model into
emitting script; this removes anything that can run, leaving the resume's
text, structure, classes and data-* hooks untouched.
"""

import re

from bs4 import BeautifulSoup

_DANGEROUS_TAGS = ("script", "iframe", "object", "embed", "frame", "frameset", "base", "form", "meta", "link")
_URL_ATTRS = ("href", "src", "action", "formaction", "xlink:href")
_SCRIPT_URL_RE = re.compile(r"^\s*(javascript|vbscript|data):", re.IGNORECASE)


def strip_active_content(html_content):
    soup = BeautifulSoup(html_content, "html.parser")
    for tag in soup.find_all(_DANGEROUS_TAGS):
        tag.decompose()
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            value = tag.attrs[attr]
            value = " ".join(value) if isinstance(value, list) else str(value)
            if attr.lower().startswith("on") or (attr.lower() in _URL_ATTRS and _SCRIPT_URL_RE.match(value)):
                del tag.attrs[attr]
    return str(soup)
