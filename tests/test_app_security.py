import time

import pytest
from fastapi.testclient import TestClient

import app as app_module
client = TestClient(app_module.app)

# Real job ids are UUID4s — the app rejects anything else.
JOB1 = "11111111-1111-4111-8111-111111111111"
JOB2 = "22222222-2222-4222-8222-222222222222"
IDLE = "33333333-3333-4333-8333-333333333333"
ACTIVE = "44444444-4444-4444-8444-444444444444"


@pytest.fixture(autouse=True)
def _clean_job_status():
    app_module.job_status.clear()
    yield
    app_module.job_status.clear()


def test_health_endpoint():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_job_status_404_for_unknown_job():
    res = client.get("/api/job-status/does-not-exist")
    assert res.status_code == 404


def test_preview_404_for_unknown_job():
    res = client.get("/api/preview/does-not-exist")
    assert res.status_code == 404


def _mock_page_conversion(monkeypatch, page_counts):
    """Mocks the docx/pdf conversion chain _check_live_page_count relies
    on, so tests never shell out to LibreOffice. page_counts is consumed
    one value per compact-level attempt (see each converter's COMPACT_LEVELS)."""
    counts = iter(page_counts)
    for converter in (app_module.docx_gen, app_module.docx_sf):
        monkeypatch.setattr(converter, "convert_resume_to_docx", lambda src, dst, level=0: open(dst, "wb").write(b"x"))
    monkeypatch.setattr(app_module.pdf_converter, "convert_docx_to_pdf_as", lambda src, dst: None)
    monkeypatch.setattr(app_module.pdf_converter, "count_pdf_pages", lambda path: next(counts))


def test_check_live_page_count_stops_at_first_level_under_2(monkeypatch):
    import asyncio
    _mock_page_conversion(monkeypatch, [3, 1])
    result = asyncio.run(app_module._check_live_page_count("<html><body>x</body></html>"))
    assert result == 1


def test_check_live_page_count_returns_none_on_conversion_failure(monkeypatch):
    import asyncio
    monkeypatch.setattr(app_module.docx_gen, "convert_resume_to_docx", lambda src, dst, level=0: open(dst, "wb").write(b"x"))
    monkeypatch.setattr(app_module.pdf_converter, "convert_docx_to_pdf_as", lambda src, dst: (_ for _ in ()).throw(Exception("boom")))
    result = asyncio.run(app_module._check_live_page_count("<html><body>x</body></html>"))
    assert result is None


def test_edit_section_dedupes_skills_and_returns_page_count(monkeypatch, tmp_path):
    submitted_html = (
        '<html><body><div class="skill-line" data-section-id="skill_0"><strong>A:</strong> Python</div>'
        '<div data-section-id="sec1">old</div></body></html>'
    )
    html_path = tmp_path / "temp.html"
    html_path.write_text(submitted_html, encoding="utf-8")
    app_module.job_status[JOB1] = {
        "status": "ready_for_edit", "created_at": time.time(), "temp_html": str(html_path),
    }
    # The section editor's own output introduces a near-duplicate ("python")
    # of the already-listed "Python" — dedupe must catch it regardless.
    monkeypatch.setattr(
        app_module.section_editor, "edit_section",
        lambda html, section_id, instruction: html.replace(
            '<div data-section-id="sec1">old</div>',
            '<div class="skill-line" data-section-id="skill_1"><strong>B:</strong> python</div>',
        ),
    )
    _mock_page_conversion(monkeypatch, [1])

    res = client.post(f"/api/edit-section/{JOB1}", data={"section_id": "sec1", "instruction": "x", "edited_html": submitted_html})
    assert res.status_code == 200
    data = res.json()
    assert data["page_count"] == 1
    assert "<strong>B:</strong>" not in data["html"]  # emptied by dedup, whole line removed
    assert data["html"].count("Python") + data["html"].count("python") == 1


def test_add_section_returns_page_count(monkeypatch, tmp_path):
    html_path = tmp_path / "temp.html"
    html_path.write_text('<html><body><div data-section-id="ref">ref</div></body></html>', encoding="utf-8")
    app_module.job_status[JOB1] = {
        "status": "ready_for_edit", "created_at": time.time(), "temp_html": str(html_path),
    }
    monkeypatch.setattr(app_module.section_editor, "add_section", lambda html, ref_id, instruction: html + "<div>new</div>")
    _mock_page_conversion(monkeypatch, [2])

    res = client.post(f"/api/add-section/{JOB1}", data={"reference_section_id": "ref", "instruction": "x", "edited_html": "<html></html>"})
    assert res.status_code == 200
    assert res.json()["page_count"] == 2


def test_apply_keywords_returns_page_count(monkeypatch, tmp_path):
    html_path = tmp_path / "temp.html"
    html_path.write_text('<html><body><div class="skill-line" data-section-id="skill_0"><strong>A:</strong> Python</div></body></html>', encoding="utf-8")
    app_module.job_status[JOB1] = {
        "status": "ready_for_edit", "created_at": time.time(), "temp_html": str(html_path),
    }
    monkeypatch.setattr(app_module.keyword_tools, "apply_keyword_changes", lambda html, add, remove, custom: html)
    _mock_page_conversion(monkeypatch, [1])

    res = client.post(f"/api/apply-keywords/{JOB1}", data={"add_keywords": ["Rust"]})
    assert res.status_code == 200
    assert res.json()["page_count"] == 1


def test_fit_to_2_pages_condenses_wording_and_returns_page_count(monkeypatch, tmp_path):
    html_path = tmp_path / "temp.html"
    html_path.write_text(
        '<html><body><div class="skill-line" data-section-id="skill_0"><strong>A:</strong> Python</div>'
        '<p class="summary-text">Verbose summary text.</p></body></html>',
        encoding="utf-8",
    )
    app_module.job_status[JOB1] = {
        "status": "ready_for_edit", "created_at": time.time(), "temp_html": str(html_path),
    }

    captured = {}

    def fake_condense(html, required_keywords):
        captured["required_keywords"] = required_keywords
        return html.replace("Verbose summary text.", "Tight summary.")

    monkeypatch.setattr(app_module.keyword_tools, "condense_resume_to_fit_pages", fake_condense)
    _mock_page_conversion(monkeypatch, [2])

    res = client.post(f"/api/fit-to-2-pages/{JOB1}")
    assert res.status_code == 200
    data = res.json()
    assert data["page_count"] == 2
    assert "Tight summary." in data["html"]
    assert "Python" in captured["required_keywords"]  # kept the resume's own existing skills

    with open(html_path, encoding="utf-8") as f:
        assert "Tight summary." in f.read()


def test_fit_to_2_pages_500_on_ai_failure(monkeypatch, tmp_path):
    html_path = tmp_path / "temp.html"
    html_path.write_text('<html><body>content</body></html>', encoding="utf-8")
    app_module.job_status[JOB1] = {
        "status": "ready_for_edit", "created_at": time.time(), "temp_html": str(html_path),
    }

    def failing(html, required_keywords):
        raise Exception("AI down")

    monkeypatch.setattr(app_module.keyword_tools, "condense_resume_to_fit_pages", failing)
    res = client.post(f"/api/fit-to-2-pages/{JOB1}")
    assert res.status_code == 500


def test_fit_to_2_pages_404_for_unknown_job():
    res = client.post("/api/fit-to-2-pages/does-not-exist")
    assert res.status_code == 404


def _job_with_files(base_dir, job_id, **extra):
    """Creates a job whose folder holds an upload, a preview and a finished DOCX."""
    job_dir = base_dir / f"temp_{job_id}"
    job_dir.mkdir()
    (job_dir / "uploaded_resume.pdf").write_bytes(b"%PDF")
    (job_dir / "temp.html").write_text("<html></html>", encoding="utf-8")
    docx = job_dir / "Jane_Doe_Resume.docx"
    docx.write_bytes(b"docx bytes")
    app_module.job_status[job_id] = {
        "status": "completed", "progress": "Done", "created_at": time.time(), "last_active": time.time(),
        "temp_html": str(job_dir / "temp.html"), "final_path": str(docx), "docx_filename": docx.name, **extra,
    }
    return job_dir


def test_discard_job_deletes_all_of_its_files(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "BASE_DIR", str(tmp_path))
    job_dir = _job_with_files(tmp_path, JOB1)
    other_dir = _job_with_files(tmp_path, JOB2)

    res = client.post(f"/api/discard-job/{JOB1}")
    assert res.status_code == 200
    assert not job_dir.exists() and JOB1 not in app_module.job_status
    # Another user's job is untouched.
    assert other_dir.exists() and JOB2 in app_module.job_status
    # Discarding again (e.g. beacon after Back-to-upload) is harmless.
    assert client.post(f"/api/discard-job/{JOB1}").status_code == 200


def test_sweep_discards_idle_jobs_and_orphan_folders_but_keeps_active_ones(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "BASE_DIR", str(tmp_path))
    idle_dir = _job_with_files(tmp_path, IDLE, last_active=time.time() - app_module.JOB_TTL_SECONDS - 10)
    active_dir = _job_with_files(tmp_path, ACTIVE)
    orphan_dir = tmp_path / "temp_from-a-previous-run"
    orphan_dir.mkdir()
    legacy_output = tmp_path / "output"
    legacy_output.mkdir()
    (legacy_output / "old_resume.docx").write_bytes(b"x")

    app_module.sweep_abandoned_jobs()

    assert not idle_dir.exists() and IDLE not in app_module.job_status
    assert not orphan_dir.exists()
    assert not legacy_output.exists()
    assert active_dir.exists() and ACTIVE in app_module.job_status


def test_activity_keeps_a_long_editing_session_alive(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "BASE_DIR", str(tmp_path))
    job_dir = _job_with_files(tmp_path, JOB1, created_at=time.time() - app_module.JOB_TTL_SECONDS - 10,
                              last_active=time.time() - app_module.JOB_TTL_SECONDS - 10)
    assert client.get(f"/api/preview/{JOB1}").status_code == 200  # user is still working
    app_module.sweep_abandoned_jobs()
    assert job_dir.exists() and JOB1 in app_module.job_status


def test_download_docx_serves_the_jobs_own_file(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "BASE_DIR", str(tmp_path))
    _job_with_files(tmp_path, JOB1)
    res = client.get(f"/api/download-docx/{JOB1}")
    assert res.status_code == 200
    assert res.content == b"docx bytes"
    assert "Jane_Doe_Resume.docx" in res.headers["content-disposition"]


def test_download_docx_404s_before_finalize_and_for_unknown_job(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "BASE_DIR", str(tmp_path))
    job_dir = _job_with_files(tmp_path, JOB1)
    (job_dir / "Jane_Doe_Resume.docx").unlink()
    assert client.get(f"/api/download-docx/{JOB1}").status_code == 404
    assert client.get("/api/download-docx/nope").status_code == 404


def test_old_shared_output_route_is_gone():
    assert client.get("/output/anything.docx").status_code == 404


def test_require_job_helper_raises_404_and_400():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        app_module._require_job("missing")
    assert exc_info.value.status_code == 404

    app_module.job_status[JOB2] = {"status": "processing", "created_at": time.time()}
    with pytest.raises(HTTPException) as exc_info:
        app_module._require_job(JOB2, require_status="ready_for_edit")
    assert exc_info.value.status_code == 400


def test_safe_error_message_strips_base_dir_and_truncates():
    err = Exception(app_module.BASE_DIR + "/secret_internal_path/error " + ("x" * 500))
    msg = app_module._safe_error_message(err)
    assert app_module.BASE_DIR not in msg
    assert len(msg) <= 300


# --- LOGIN PIN GATE ---
# Uses its own TestClient (a fresh cookie jar) rather than the module-level
# `client`, which is deliberately pre-authenticated for every other test file.

# --- REGRESSION: editing/re-downloading must keep working after the first
# download (job status "completed"), not just while "ready_for_edit". Before
# this fix, downloading once flipped a job to "completed" and permanently
# locked out every editing/finalize route for it — so a manual edit made
# after that first download would silently fail to apply on the next
# download (the reported bug: "edit on site, download PDF, edits missing").

def test_require_job_accepts_a_tuple_of_statuses():
    app_module.job_status[JOB1] = {"status": "completed", "created_at": time.time()}
    data = app_module._require_job(JOB1, require_status=("ready_for_edit", "completed"))
    assert data["status"] == "completed"


def test_require_job_tuple_still_rejects_other_statuses():
    from fastapi import HTTPException

    app_module.job_status[JOB1] = {"status": "processing", "created_at": time.time()}
    with pytest.raises(HTTPException) as exc_info:
        app_module._require_job(JOB1, require_status=("ready_for_edit", "completed"))
    assert exc_info.value.status_code == 400


def test_preview_works_after_job_already_completed(tmp_path):
    html_path = tmp_path / "preview.html"
    html_path.write_text("<html><body>hi</body></html>", encoding="utf-8")
    app_module.job_status[JOB1] = {
        "status": "completed", "created_at": time.time(), "temp_html": str(html_path),
    }
    res = client.get(f"/api/preview/{JOB1}")
    assert res.status_code == 200


def test_finalize_resume_re_finalizes_a_job_that_was_already_completed(monkeypatch, tmp_path):
    """The exact regression: editing again and re-downloading after the first
    download must actually re-run finalize, not be rejected as 'not ready'."""
    html_path = tmp_path / "temp.html"
    html_path.write_text("<html><body>original</body></html>", encoding="utf-8")
    final_path = tmp_path / "Resume.docx"
    final_path.write_bytes(b"old docx bytes")

    monkeypatch.setattr(app_module.docx_gen, "convert_resume_to_docx", lambda src, dst, level=0: None)
    monkeypatch.setattr(app_module.pdf_converter, "convert_docx_to_pdf_as", lambda src, dst: None)
    monkeypatch.setattr(app_module.pdf_converter, "count_pdf_pages", lambda path: 1)
    monkeypatch.setattr(app_module.ats_check, "check_docx_parseability", lambda *a, **k: [])

    app_module.job_status[JOB1] = {
        "status": "completed", "created_at": time.time(), "temp_html": str(html_path),
        "final_path": str(final_path), "docx_filename": "Resume.docx", "choice": "general",
    }

    res = client.post(f"/api/finalize-resume/{JOB1}", data={"edited_html": "<html><body>edited!</body></html>"})
    assert res.status_code == 200
    assert html_path.read_text(encoding="utf-8") == "<html><body>edited!</body></html>"
    assert app_module.job_status[JOB1]["status"] == "completed"
    assert app_module.job_status[JOB1]["page_count"] == 1


def test_finalize_resume_shrinks_compact_level_until_it_fits_2_pages(monkeypatch, tmp_path):
    """The actual 'strictly max 2 pages' enforcement: if the normal layout
    renders to more than 2 pages, finalize must retry at denser compact
    levels — never drop content — until it fits (or the presets run out)."""
    html_path = tmp_path / "temp.html"
    html_path.write_text("<html><body>content</body></html>", encoding="utf-8")
    final_path = tmp_path / "Resume.docx"

    seen_levels = []

    def fake_convert_docx(src, dst, level=0):
        seen_levels.append(level)
        with open(dst, "wb") as f:
            f.write(b"docx bytes")

    # Simulate: 3 pages at level 0, 3 pages at level 1, finally fits at level 2.
    page_counts_by_call = iter([3, 3, 1])

    monkeypatch.setattr(app_module.docx_gen, "convert_resume_to_docx", fake_convert_docx)
    monkeypatch.setattr(app_module.pdf_converter, "convert_docx_to_pdf_as", lambda src, dst: None)
    monkeypatch.setattr(app_module.pdf_converter, "count_pdf_pages", lambda path: next(page_counts_by_call))
    monkeypatch.setattr(app_module.ats_check, "check_docx_parseability", lambda *a, **k: [])

    app_module.job_status[JOB1] = {
        "status": "completed", "created_at": time.time(), "temp_html": str(html_path),
        "final_path": str(final_path), "docx_filename": "Resume.docx", "choice": "general",
    }

    res = client.post(f"/api/finalize-resume/{JOB1}", data={"edited_html": "<html><body>content</body></html>"})
    assert res.status_code == 200
    assert seen_levels == [0, 1, 2]
    assert app_module.job_status[JOB1]["compact_level"] == 2
    assert app_module.job_status[JOB1]["page_count"] == 1
    assert app_module.job_status[JOB1]["status"] == "completed"


# --- template selection (General / Salesforce) ---

def test_convert_rejects_unknown_template(tmp_path):
    res = client.post(
        "/api/convert-resume",
        files={"resume_file": ("r.pdf", b"%PDF-1.4", "application/pdf")},
        data={"template_choice": "minimal"},
    )
    assert res.status_code == 400
    assert "Unknown template" in res.json()["detail"]


def test_resolve_template_choice_accepts_both_templates_and_legacy_alias():
    assert app_module._resolve_template_choice("general") == "general"
    assert app_module._resolve_template_choice("Salesforce") == "salesforce"
    assert app_module._resolve_template_choice("classic") == "general"
    assert app_module._resolve_template_choice(None) == "general"


def test_live_page_count_uses_the_jobs_own_template_converter(monkeypatch):
    import asyncio
    used = []
    monkeypatch.setattr(app_module.docx_gen, "convert_resume_to_docx", lambda s, d, level=0: used.append("general") or open(d, "wb").write(b"x"))
    monkeypatch.setattr(app_module.docx_sf, "convert_resume_to_docx", lambda s, d, level=0: used.append("salesforce") or open(d, "wb").write(b"x"))
    monkeypatch.setattr(app_module.pdf_converter, "convert_docx_to_pdf_as", lambda src, dst: None)
    monkeypatch.setattr(app_module.pdf_converter, "count_pdf_pages", lambda path: 1)
    asyncio.run(app_module._check_live_page_count("<html><body>x</body></html>", "salesforce"))
    asyncio.run(app_module._check_live_page_count("<html><body>x</body></html>", "general"))
    assert used == ["salesforce", "general"]



def test_badge_route_serves_only_bundled_badges():
    res = client.get("/certificates_badges/admin_badge.png")
    assert res.status_code == 200 and res.headers["content-type"] == "image/png"
    for attempt in ["../../app.py", "..%2f..%2fapp.py", "nope.png"]:
        assert client.get(f"/certificates_badges/{attempt}").status_code == 404


def test_convert_rejects_non_resume_file_types(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "BASE_DIR", str(tmp_path))
    for name in ("malware.exe", "photo.png", "noextension"):
        res = client.post("/api/convert-resume", files={"resume_file": (name, b"data", "application/octet-stream")})
        assert res.status_code == 400
    assert not list(tmp_path.glob("temp_*"))  # nothing left on disk


def test_oversized_upload_leaves_no_temp_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(app_module, "MAX_UPLOAD_BYTES", 10)
    res = client.post("/api/convert-resume", files={"resume_file": ("r.pdf", b"x" * 100, "application/pdf")})
    assert res.status_code == 413
    assert not list(tmp_path.glob("temp_*"))


def test_non_uuid_job_ids_are_rejected_and_never_touch_disk(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "BASE_DIR", str(tmp_path))
    victim = tmp_path / "temp_.."
    victim.mkdir()
    for bad in ("job1", "not-a-uuid", "11111111-1111-1111-1111-111111111111"):  # last: not version 4
        assert client.get(f"/api/job-status/{bad}").status_code == 404
        assert client.post(f"/api/discard-job/{bad}").status_code == 200
    app_module._discard_job("..")  # the HTTP client would normalize ".." away, so call it directly
    assert victim.exists()


def test_ai_returned_html_is_stripped_of_script(monkeypatch, tmp_path):
    from src.html_safety import strip_active_content
    dirty = ('<div class="skill-line" data-section-id="skill_0" onclick="x()"><strong>A:</strong> Python'
             '<script>alert(1)</script><img src="download.png" onerror="alert(2)">'
             '<a href="javascript:alert(3)">l</a><a href="https://ok.example">ok</a></div>')
    clean = strip_active_content(dirty)
    assert "<script" not in clean and "onerror" not in clean and "onclick" not in clean and "javascript:" not in clean
    assert 'data-section-id="skill_0"' in clean and 'src="download.png"' in clean and "https://ok.example" in clean

    # And the section editor route applies it.
    html_path = tmp_path / "temp.html"
    html_path.write_text("<html><body><div data-section-id='s1'>old</div></body></html>", encoding="utf-8")
    app_module.job_status[JOB1] = {"status": "ready_for_edit", "created_at": time.time(), "temp_html": str(html_path)}
    monkeypatch.setattr(app_module.section_editor, "edit_section", lambda *a: "<html><body><div data-section-id='s1'>new<script>x</script></div></body></html>")
    monkeypatch.setattr(app_module, "_check_live_page_count", lambda *a: _none())
    res = client.post(f"/api/edit-section/{JOB1}", data={"section_id": "s1", "instruction": "x", "edited_html": "<html></html>"})
    assert res.status_code == 200
    assert "<script" not in res.json()["html"] and "<script" not in html_path.read_text(encoding="utf-8")


async def _none():
    return None
