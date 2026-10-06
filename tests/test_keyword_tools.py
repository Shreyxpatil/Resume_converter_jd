import pytest

from src import ai_client
from src.keyword_tools import (
    compute_keyword_match,
    condense_resume_to_fit_pages,
    extract_jd_keywords,
    extract_resume_keywords,
)

HTML = """
<html><body>
<div class="skill-line"><strong>Languages:</strong> Python, Go, C++</div>
<div class="entry-block">
  <ul class="bullets"><li>Built services with FastAPI and Docker</li></ul>
</div>
</body></html>
"""


def test_compute_keyword_match_splits_present_and_missing():
    added, missing = compute_keyword_match(HTML, ["Python", "Docker", "Rust", "Kubernetes"])
    assert set(added) == {"Python", "Docker"}
    assert set(missing) == {"Rust", "Kubernetes"}


def test_compute_keyword_match_word_boundary_avoids_substring_false_positive():
    # "Go" should not match inside "Golang" or "Google" — a bare substring search would.
    html = "<html><body><p>We use Golang and Google Cloud.</p></body></html>"
    added, missing = compute_keyword_match(html, ["Go"])
    assert added == []
    assert missing == ["Go"]


def test_compute_keyword_match_handles_symbol_keywords_as_substring():
    html = "<html><body><p>Experience with C++ and Node.js</p></body></html>"
    added, missing = compute_keyword_match(html, ["C++", "Node.js"])
    assert set(added) == {"C++", "Node.js"}


def test_compute_keyword_match_empty_keywords_returns_empty_lists():
    added, missing = compute_keyword_match(HTML, [])
    assert added == []
    assert missing == []


def test_extract_resume_keywords_finds_skill_line_entries():
    skills = extract_resume_keywords(HTML)
    assert "Python" in skills
    assert "Go" in skills
    assert "C++" in skills


def test_extract_resume_keywords_empty_html_returns_empty_list():
    assert extract_resume_keywords("") == []
    assert extract_resume_keywords(None) == []


def test_compute_keyword_match_accepts_priority_dicts_and_preserves_shape():
    keywords = [
        {"term": "Python", "priority": "must-have"},
        {"term": "Rust", "priority": "must-have"},
        {"term": "Docker", "priority": "nice-to-have"},
    ]
    added, missing = compute_keyword_match(HTML, keywords)
    assert added == [{"term": "Python", "priority": "must-have"}, {"term": "Docker", "priority": "nice-to-have"}]
    assert missing == [{"term": "Rust", "priority": "must-have"}]


def test_extract_jd_keywords_parses_priority_from_ai_response(monkeypatch):
    fake_json = (
        '[{"term": "Python", "priority": "must-have"}, '
        '{"term": "Docker", "priority": "nice-to-have"}, '
        '{"term": "Python", "priority": "must-have"}]'
    )

    class FakeResponse:
        text = fake_json

    monkeypatch.setattr(ai_client, "generate_with_ai_fallback", lambda *a, **k: FakeResponse())

    result = extract_jd_keywords("some job description")
    assert result == [
        {"term": "Python", "priority": "must-have"},
        {"term": "Docker", "priority": "nice-to-have"},
    ]


def test_extract_jd_keywords_defaults_invalid_priority_to_nice_to_have(monkeypatch):
    class FakeResponse:
        text = '[{"term": "Python", "priority": "urgent"}, {"term": "Go"}]'

    monkeypatch.setattr(ai_client, "generate_with_ai_fallback", lambda *a, **k: FakeResponse())

    result = extract_jd_keywords("some job description")
    assert result == [
        {"term": "Python", "priority": "nice-to-have"},
        {"term": "Go", "priority": "nice-to-have"},
    ]


def test_extract_jd_keywords_recovers_terms_from_malformed_json(monkeypatch):
    # Truncated JSON — the regex fallback still salvages every complete
    # "term": "..." pair it can find, just without priority info.
    class FakeResponse:
        text = '[{"term": "Python", "priority": "must-have"}, {"term": "Docker"'

    monkeypatch.setattr(ai_client, "generate_with_ai_fallback", lambda *a, **k: FakeResponse())

    result = extract_jd_keywords("some job description")
    assert result == [
        {"term": "Python", "priority": "nice-to-have"},
        {"term": "Docker", "priority": "nice-to-have"},
    ]


def test_extract_jd_keywords_empty_description_returns_empty_list():
    assert extract_jd_keywords("") == []
    assert extract_jd_keywords("   ") == []


def test_condense_resume_to_fit_pages_returns_ai_response(monkeypatch):

    class FakeResponse:
        text = "<html><body>" + ("shortened content " * 50) + "</body></html>"

    monkeypatch.setattr(ai_client, "generate_with_ai_fallback", lambda *a, **k: FakeResponse())
    result = condense_resume_to_fit_pages("<html><body>" + ("x" * 500) + "</body></html>", ["Python"])
    assert "shortened content" in result


def test_condense_resume_to_fit_pages_prompt_includes_required_keywords(monkeypatch):
    captured = {}

    class FakeResponse:
        text = "<html><body>" + ("kept content " * 50) + "</body></html>"

    def fake_generate(prompt, **kwargs):
        captured["prompt"] = prompt
        return FakeResponse()

    monkeypatch.setattr(ai_client, "generate_with_ai_fallback", fake_generate)
    condense_resume_to_fit_pages("<html><body>" + ("x" * 500) + "</body></html>", ["Kubernetes", "Python"])
    assert "Kubernetes" in captured["prompt"]
    assert "Python" in captured["prompt"]
    assert "Do NOT delete any bullet" in captured["prompt"]


def test_condense_resume_to_fit_pages_rejects_incomplete_response(monkeypatch):

    class FakeResponse:
        text = "sorry, I can't help with that"

    monkeypatch.setattr(ai_client, "generate_with_ai_fallback", lambda *a, **k: FakeResponse())
    with pytest.raises(Exception):
        condense_resume_to_fit_pages("<html><body>" + ("x" * 500) + "</body></html>", [])
