from datetime import datetime
from types import SimpleNamespace

from bs4 import BeautifulSoup

import src.keyword_highlighter as keyword_highlighter
import src.keyword_tools as keyword_tools
import src.resume_parser_common as common
import src.resume_parser_general as parser_gen
import src.resume_parser_salesforce as parser_sf
from tests.template_fixtures import GENERAL_DATA, SALESFORCE_DATA, TEMPLATES_DIR, render_general_html, render_salesforce_html


def _fake_ai(monkeypatch, payload, captured):
    import json

    def fake_generate(prompt, models=None, groq_first=False):
        captured["prompt"] = prompt
        return SimpleNamespace(text="```json\n" + json.dumps(payload) + "\n```")

    monkeypatch.setattr(common.ai_client, "generate_with_ai_fallback", fake_generate)
    monkeypatch.setattr(common, "extract_text_from_path", lambda path: "RESUME TEXT")


# --- prompt assembly ---

def test_prompt_always_carries_honesty_rules_and_optional_parts():
    prompt = common.build_prompt("SYS", "USER", job_description="Need Apex", extra_instructions="Be brief")
    assert "NEVER invent or alter factual/discrete fields" in prompt
    assert "NEVER drop or omit a bullet" in prompt
    assert "Need Apex" in prompt and "Be brief" in prompt

    bare = common.build_prompt("SYS", "USER")
    assert "JD TAILORING" not in bare and "ADDITIONAL USER INSTRUCTIONS" not in bare


def test_without_jd_descriptions_are_verbatim_and_with_jd_they_are_tailored():
    no_jd = common.build_prompt("SYS", "USER")
    assert "PROJECT DESCRIPTIONS ARE COPIED VERBATIM" in no_jd
    assert "JD TAILORING MODE" not in no_jd

    with_jd = common.build_prompt("SYS", "USER", job_description="Requires 10+ years of Apex")
    assert "PROJECT DESCRIPTIONS ARE COPIED VERBATIM" not in with_jd
    assert "rewrite every bullet so it speaks to this JD" in with_jd
    # Tailoring never lifts the honesty rules.
    assert "NEVER invent or alter factual/discrete fields" in with_jd
    assert "MUST satisfy the JD's stated/implied minimum" not in with_jd


# --- verbatim project descriptions ---

PDF_LIKE_RESUME_TEXT = """Priya Sharma
Projects
Retail Lead Management Revamp
• Built Apex trigger framework that auto-assigns leads to 300+ sales
reps based on territory rules.
• Developed LWC dashboards that cut weekly pipeline-review prep time
by 40%.
- Wrote test classes keeping org code coverage above 90%.
"""


def test_restore_keeps_exact_bullets_and_strips_symbols():
    restored = common.restore_verbatim_bullets(
        ["• Built Apex trigger framework that auto-assigns leads to 300+ sales reps based on territory rules."],
        PDF_LIKE_RESUME_TEXT,
    )
    assert restored == ["Built Apex trigger framework that auto-assigns leads to 300+ sales reps based on territory rules."]


def test_restore_undoes_paraphrase_and_added_keywords():
    restored = common.restore_verbatim_bullets([
        "Developed scalable LWC dashboards that reduced weekly pipeline-review preparation time by 40%.",
        "Wrote Apex test classes keeping org code coverage above 90% using Salesforce DX.",
    ], PDF_LIKE_RESUME_TEXT)
    assert restored == [
        "Developed LWC dashboards that cut weekly pipeline-review prep time by 40%.",
        "Wrote test classes keeping org code coverage above 90%.",
    ]


def test_restore_leaves_unlocatable_or_short_bullets_alone():
    restored = common.restore_verbatim_bullets(["Led migration to Kubernetes for 12 microservices", "Apex"], PDF_LIKE_RESUME_TEXT)
    assert restored == ["Led migration to Kubernetes for 12 microservices", "Apex"]


def test_extraction_writes_source_wording_even_if_ai_paraphrases(monkeypatch, tmp_path):
    data = dict(SALESFORCE_DATA)
    data["projects_descriptions"] = [dict(SALESFORCE_DATA["projects_descriptions"][0], description=[
        "Engineered a robust Apex trigger framework automatically assigning leads to 300+ sales reps based on territory rules.",
    ])]
    _fake_ai(monkeypatch, {common.TOP_KEY: data}, {})
    monkeypatch.setattr(common, "extract_text_from_path", lambda path: PDF_LIKE_RESUME_TEXT)
    out = tmp_path / "out.html"
    assert parser_sf.extract_resume_data("r.pdf", f"{TEMPLATES_DIR}/resume_template_salesforce.jinja2.html", str(out)) is True
    li = BeautifulSoup(out.read_text(encoding="utf-8"), "html.parser").find("li").get_text(strip=True)
    assert li == "Built Apex trigger framework that auto-assigns leads to 300+ sales reps based on territory rules."


# --- general parser ---

def test_general_extract_end_to_end_with_fake_ai(monkeypatch, tmp_path):
    captured = {}
    _fake_ai(monkeypatch, {common.TOP_KEY: GENERAL_DATA}, captured)
    out = tmp_path / "out.html"
    ok = parser_gen.extract_resume_data(
        "resume.pdf", f"{TEMPLATES_DIR}/resume_template_general.jinja2.html", str(out),
        job_description="Need Go",
    )
    assert ok is True
    assert "RESUME TEXT" in captured["prompt"] and "Need Go" in captured["prompt"]
    soup = BeautifulSoup(out.read_text(encoding="utf-8"), "html.parser")
    assert soup.find(class_="name").get_text(strip=True) == "Jane Doe"
    assert len(soup.find_all(class_="entry-block")) == 2


def test_general_extract_accepts_response_without_top_key(monkeypatch, tmp_path):
    _fake_ai(monkeypatch, GENERAL_DATA, {})
    out = tmp_path / "out.html"
    assert parser_gen.extract_resume_data("r.pdf", f"{TEMPLATES_DIR}/resume_template_general.jinja2.html", str(out)) is True


def test_general_extract_returns_false_on_bad_json(monkeypatch, tmp_path):
    monkeypatch.setattr(common.ai_client, "generate_with_ai_fallback", lambda *a, **k: SimpleNamespace(text="not json"))
    monkeypatch.setattr(common, "extract_text_from_path", lambda path: "x")
    assert parser_gen.extract_resume_data("r.pdf", f"{TEMPLATES_DIR}/resume_template_general.jinja2.html", str(tmp_path / "o.html")) is False


def test_general_missing_role_falls_back_to_current_title_not_invented_one():
    projects = parser_gen.transform_projects_data([{"project_name": "X", "Role": "NA", "Duration": ""}], fallback_role="Backend Engineer")
    assert projects[0]["role"] == "Backend Engineer"
    assert projects[0]["duration"] == "NA"


def test_general_template_vars_format_lists():
    v = parser_gen.build_template_vars({"name": "Candidate Name", "development_tool": [], "database": ["MySQL", "Redis"]})
    assert v["name"] == "NA"
    assert v["development_tools"] == "NA"
    assert v["databases"] == "MySQL, Redis"


# --- salesforce parser ---

def test_salesforce_duration_is_computed_in_months():
    assert parser_sf.calculate_duration_string("Jan 2022 - Dec 2022") == "12 Months"
    assert parser_sf.calculate_duration_string("Mar 2024 - Present", now=datetime(2024, 5, 1)) == "3 Months"
    assert parser_sf.calculate_duration_string("") == "N/A"
    assert parser_sf.calculate_duration_string("29 Months") == "29 Months"


def test_salesforce_titles_are_numbered():
    projects = parser_sf.transform_projects_data([
        {"project_name": "A", "Clouds": "Sales Cloud", "description": "one"},
        "garbage",
        {"project_name": "B", "Clouds": "NA"},
    ])
    assert [p["title"] for p in projects] == ["Project 1: (Sales Cloud)", "Project 2:"]
    assert projects[0]["description"] == ["one"]


def test_salesforce_template_marks_name_and_skill_rows(tmp_path):
    _path, html = render_salesforce_html(tmp_path)
    soup = BeautifulSoup(html, "html.parser")
    assert soup.find(class_="name").get_text(strip=True) == "Raj Kumar"
    assert {"Apex", "LWC", "Sales Cloud"} <= set(keyword_tools.extract_resume_keywords(html))


# --- shared feature behavior on the real templates ---

def test_general_resume_keywords_have_no_glued_category_junk(tmp_path):
    _path, html = render_general_html(tmp_path)
    skills = keyword_tools.extract_resume_keywords(html)
    assert {"Python", "Go", "AWS", "Docker"} <= set(skills)
    assert not [s for s in skills if ":" in s]


def test_placeholder_na_is_never_bolded(tmp_path):
    data = dict(SALESFORCE_DATA, ticketing_case_management=[])
    data["projects_descriptions"] = [dict(SALESFORCE_DATA["projects_descriptions"][0], description=["Handled NA region rollout with Apex"])]
    _path, html = render_salesforce_html(tmp_path, data)
    bolded = keyword_highlighter.bold_skill_mentions(html)
    soup = BeautifulSoup(bolded, "html.parser")
    strongs = [s.get_text() for s in soup.find("li").find_all("strong")]
    assert "Apex" in strongs and "NA" not in strongs


def test_general_skill_dedupe_works_on_template_markup(tmp_path):
    data = dict(GENERAL_DATA)
    data["expertise"] = [{"category_name": "C", "skills": [
        {"skill_name": "Languages", "details": ["Python"]},
        {"skill_name": "Scripting", "details": ["python", "Bash"]},
    ]}]
    _path, html = render_general_html(tmp_path, data)
    deduped = BeautifulSoup(keyword_highlighter.dedupe_skill_lines(html), "html.parser")
    lines = [el.get_text(" ", strip=True) for el in deduped.find_all(class_="skill-line")]
    assert lines == ["Languages: Python", "Scripting: Bash"]


def test_salesforce_preview_shows_matching_badges_and_company_logo(tmp_path):
    _path, html = render_salesforce_html(tmp_path)
    srcs = [img["src"] for img in BeautifulSoup(html, "html.parser").find_all("img")]
    # Fixture holds "Salesforce Certified Administrator" + "Platform Developer I".
    assert srcs == ["certificates_badges/admin_badge.png", "certificates_badges/pd1_badge.png", "download.png"]


def test_salesforce_preview_has_no_badges_without_matching_certs(tmp_path):
    _path, html = render_salesforce_html(tmp_path, dict(SALESFORCE_DATA, certifications=[]))
    srcs = [img["src"] for img in BeautifulSoup(html, "html.parser").find_all("img")]
    assert srcs == ["download.png"]


DOCX_LIKE_RESUME_TEXT = """Projects
Insurance Claims Service Portal
Role: Salesforce Developer | Duration: Jun 2021 - Dec 2022 | Industry: Insurance
Implemented Service Cloud case routing with Omni-Channel for a 120-agent support team.
Built an Experience Cloud portal letting policyholders file and track claims online.
Healthcare Data Migration
Role: Junior Salesforce Developer | Duration: Jan 2021 - May 2021 | Industry: Healthcare
Migrated 1.2M patient-contact records from a legacy CRM using Data Loader and batch Apex."""


def test_restore_never_glues_heading_lines_onto_a_bullet():
    bullets = [
        "Implemented Service Cloud case routing with Omni-Channel for a 120-agent support team.",
        "Migrated 1.2M patient-contact records from a legacy CRM using Data Loader and batch Apex.",
        "Built an Experience Cloud portal enabling policyholders to file and track insurance claims online.",
    ]
    assert common.restore_verbatim_bullets(bullets, DOCX_LIKE_RESUME_TEXT) == [
        bullets[0],
        bullets[1],
        "Built an Experience Cloud portal letting policyholders file and track claims online.",
    ]


def test_restore_strips_pdf_private_use_bullet_glyphs():
    text = " Wrote test classes keeping org code coverage above 90%.\n Built Apex triggers for lead routing."
    assert common.restore_verbatim_bullets([" Wrote test classes keeping org code coverage above 90%."], text) == [
        "Wrote test classes keeping org code coverage above 90%."
    ]



def test_with_jd_tailored_bullets_are_kept_not_reverted(monkeypatch, tmp_path):
    tailored = "Engineered a scalable Apex trigger framework that auto-assigns leads to 300+ sales reps based on territory rules."
    data = dict(SALESFORCE_DATA)
    data["projects_descriptions"] = [dict(SALESFORCE_DATA["projects_descriptions"][0], description=[tailored])]
    _fake_ai(monkeypatch, {common.TOP_KEY: data}, {})
    monkeypatch.setattr(common, "extract_text_from_path", lambda path: PDF_LIKE_RESUME_TEXT)
    out = tmp_path / "out.html"
    assert parser_sf.extract_resume_data(
        "r.pdf", f"{TEMPLATES_DIR}/resume_template_salesforce.jinja2.html", str(out), job_description="Needs Apex at scale",
    ) is True
    li = BeautifulSoup(out.read_text(encoding="utf-8"), "html.parser").find("li").get_text(strip=True)
    assert li == tailored


def test_resume_text_is_escaped_never_injected_as_html(tmp_path):
    # A resume (or AI output) containing markup must render as text — the
    # preview is same-origin, so raw HTML here would run script in the editor.
    payload = '<img src=x onerror=alert(1)>'
    data = dict(GENERAL_DATA, name=f"Jane {payload}")
    data["projects_descriptions"] = [dict(GENERAL_DATA["projects_descriptions"][0], description=[f"Built {payload} things"])]
    _path, html = render_general_html(tmp_path, data)
    soup = BeautifulSoup(html, "html.parser")
    assert soup.find("img", attrs={"onerror": True}) is None
    assert payload in soup.find(class_="name").get_text()


def test_trailing_commas_in_ai_json_are_tolerated():
    assert common.parse_ai_json('```json\n{"a": [1, 2,], "b": {"c": 3,},}\n```') == {"a": [1, 2], "b": {"c": 3}}


def test_extraction_retries_once_after_invalid_json(monkeypatch, tmp_path):
    import json
    replies = iter(["{ this is not json", json.dumps({common.TOP_KEY: GENERAL_DATA})])
    calls = []

    def fake_generate(prompt, models=None, groq_first=False):
        calls.append(1)
        return SimpleNamespace(text=next(replies))

    monkeypatch.setattr(common.ai_client, "generate_with_ai_fallback", fake_generate)
    monkeypatch.setattr(common, "extract_text_from_path", lambda path: "x")
    out = tmp_path / "o.html"
    assert parser_gen.extract_resume_data("r.pdf", f"{TEMPLATES_DIR}/resume_template_general.jinja2.html", str(out)) is True
    assert len(calls) == 2
