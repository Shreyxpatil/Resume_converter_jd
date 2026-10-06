

import sys
import os

# Ensure UTF-8 output encoding on Windows to prevent charmap encoding errors with emojis/unicode
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Form, APIRouter
from fastapi.responses import FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from bs4 import BeautifulSoup
import uuid
import shutil
import tempfile
import time
from typing import Optional, Dict, Any, List
import glob
import asyncio
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor

# Thread pool for blocking parser/converter calls
_executor = ThreadPoolExecutor(max_workers=10)

# --- IMPORTS ---
import src.resume_parser_general as parser_gen
import src.resume_parser_salesforce as parser_sf
import src.docx_converter_general as docx_gen
import src.docx_converter_salesforce as docx_sf
import src.keyword_tools as keyword_tools
import src.jd_extractor as jd_extractor
import src.pdf_converter as pdf_converter
import src.section_editor as section_editor
import src.ats_check as ats_check
import src.keyword_highlighter as keyword_highlighter
from src.html_safety import strip_active_content
from src.logging_config import get_logger

logger = get_logger(__name__)

# --- APP SETUP ---
@asynccontextmanager
async def _lifespan(_app):
    # Anything on disk at startup belongs to a session that can't be resumed
    # (job state is in memory), so clear it, then keep sweeping idle jobs.
    sweep_abandoned_jobs()
    sweeper = asyncio.create_task(_sweep_periodically())
    yield
    sweeper.cancel()


app = FastAPI(lifespan=_lifespan)

# CORS: the frontend is served same-origin by this same app, so it never needs
# cross-origin access to its own API — the only reason to widen this is a
# separately-hosted client. Default to just this app's own origin(s) rather
# than allow_origins=["*"], and let ALLOWED_ORIGINS override for that case.
_default_port = os.getenv("PORT", "8000")
_allowed_origins_env = os.getenv("ALLOWED_ORIGINS")
ALLOWED_ORIGINS = (
    [o.strip() for o in _allowed_origins_env.split(",") if o.strip()]
    if _allowed_origins_env
    else [f"http://localhost:{_default_port}", f"http://127.0.0.1:{_default_port}"]
)
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_methods=["*"], allow_headers=["*"])
router = APIRouter(tags=["Resume API"])

# Directories
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.makedirs(os.path.join(BASE_DIR, "frontend"), exist_ok=True)

# State
# Nothing is kept once a user leaves: every file for a conversion (upload,
# preview HTML, generated DOCX/PDF) lives in that job's own temp_<job_id>/
# folder, which is deleted when the page tells us the user left
# (/api/discard-job), or — as a fallback if that never arrives (browser
# crash, lost network) — after JOB_TTL_SECONDS without any activity.
job_status = {}
JOB_TTL_SECONDS = int(os.getenv("JOB_TTL_SECONDS", str(60 * 60)))
SWEEP_INTERVAL_SECONDS = 5 * 60
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "15")) * 1024 * 1024

# Templates — each one pairs a Jinja2 layout with the parser that fills it
# and the DOCX converter that renders it. Modules (not functions) are stored
# so lookups happen at call time (keeps monkeypatching in tests simple).
DEFAULT_TEMPLATE = "general"
TEMPLATES = {
    "general": {"file": "resume_template_general.jinja2.html", "parser": parser_gen, "converter": docx_gen},
    "salesforce": {"file": "resume_template_salesforce.jinja2.html", "parser": parser_sf, "converter": docx_sf},
}
# The original General template was called "classic" in older clients.
TEMPLATE_ALIASES = {"classic": "general"}


def _resolve_template_choice(choice):
    choice = (choice or DEFAULT_TEMPLATE).strip().lower()
    choice = TEMPLATE_ALIASES.get(choice, choice)
    if choice not in TEMPLATES:
        raise HTTPException(status_code=400, detail=f"Unknown template '{choice}' (expected one of: {', '.join(TEMPLATES)})")
    return choice


def _converter_for(choice):
    return TEMPLATES.get(choice, TEMPLATES[DEFAULT_TEMPLATE])["converter"]


def _parser_for(choice):
    return TEMPLATES.get(choice, TEMPLATES[DEFAULT_TEMPLATE])["parser"]

# --- MODELS ---
class ConversionResponse(BaseModel):
    job_id: str
    status: str
    message: str
    download_url: Optional[str] = None
    file_count: Optional[int] = None

class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    progress: str
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

# --- UTILS ---

def _job_dir(job_id):
    return os.path.join(BASE_DIR, f"temp_{job_id}")


def _discard_job(job_id):
    """Deletes everything stored for one job — its temp folder (upload,
    preview, DOCX, PDF) and its in-memory state. Safe to call twice."""
    if not _is_job_id(job_id):
        return
    job_status.pop(job_id, None)
    shutil.rmtree(_job_dir(job_id), ignore_errors=True)


def sweep_abandoned_jobs():
    """Fallback cleanup for users who left without the page managing to
    tell us: discards jobs idle longer than JOB_TTL_SECONDS, plus any
    temp_* folder no live job owns (e.g. left behind by a server restart —
    job state is in memory only, so those can never be resumed anyway)."""
    now = time.time()
    stale_ids = [
        jid for jid, data in job_status.items()
        if now - data.get("last_active", data.get("created_at", now)) > JOB_TTL_SECONDS
    ]
    for jid in stale_ids:
        _discard_job(jid)
    if stale_ids:
        logger.info(f"Discarded {len(stale_ids)} abandoned job(s)")

    for d in glob.glob(os.path.join(BASE_DIR, "temp_*")):
        if os.path.isdir(d) and os.path.basename(d)[len("temp_"):] not in job_status:
            shutil.rmtree(d, ignore_errors=True)

    # Older versions kept finished resumes in a shared output/ folder.
    shutil.rmtree(os.path.join(BASE_DIR, "output"), ignore_errors=True)


async def _sweep_periodically():
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        try:
            sweep_abandoned_jobs()
        except Exception as e:
            logger.warning(f"Abandoned-job sweep failed: {e}")


ALLOWED_RESUME_EXTENSIONS = {".pdf", ".docx"}


def _is_job_id(job_id):
    """Job ids are server-generated UUID4s — anything else is never a real job."""
    try:
        return str(uuid.UUID(job_id, version=4)) == job_id
    except (ValueError, TypeError, AttributeError):
        return False


def _require_job(job_id, require_status=None):
    """Shared job-lookup + status-check used by nearly every route — replaces
    the same two `if`/raise checks that used to be duplicated ~10 times.
    require_status accepts either a single status string or a tuple/list of
    acceptable ones (e.g. editing routes accept both "ready_for_edit" and
    "completed", since downloading once shouldn't lock the job forever)."""
    if not _is_job_id(job_id) or job_id not in job_status:
        raise HTTPException(status_code=404, detail="Job not found")
    data = job_status[job_id]
    data["last_active"] = time.time()
    if require_status:
        allowed = {require_status} if isinstance(require_status, str) else set(require_status)
        if data["status"] not in allowed:
            raise HTTPException(status_code=400, detail=f"Not ready (status={data['status']})")
    return data


def _save_upload_enforcing_limit(file_obj, dest_path):
    """Streams an uploaded file to disk in chunks, aborting (and cleaning up)
    if it exceeds MAX_UPLOAD_BYTES rather than buffering unbounded input."""
    written = 0
    try:
        with open(dest_path, "wb") as out:
            while True:
                chunk = file_obj.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)}MB)",
                    )
                out.write(chunk)
    except HTTPException:
        if os.path.exists(dest_path):
            os.remove(dest_path)
        raise


def _safe_error_message(e, fallback="An internal error occurred"):
    """Full exception detail (including tracebacks) goes to the server log
    only — job_status/HTTP responses get a short, local-path-free message so
    provider error bodies or filesystem layout don't leak to the browser."""
    msg = str(e).strip() or fallback
    msg = msg.replace(BASE_DIR, "").strip()
    return msg[:300]


def _derive_professional_filename(temp_html_path, fallback_filename):
    """Once extraction succeeds we know the candidate's real name — prefer a
    clean "FirstName_LastName_Resume.docx" over the generic upload-derived
    name (`converted_resume_<original filename>.docx`), which reads as more
    polished to a recruiter at zero cost."""
    try:
        with open(temp_html_path, "r", encoding="utf-8") as f:
            soup = BeautifulSoup(f.read(), "html.parser")
        name_el = soup.find(class_="name")
        name_text = name_el.get_text(strip=True) if name_el else ""
    except Exception:
        return fallback_filename

    clean = "".join(c for c in name_text if c.isalnum() or c in (" ", "-", "_")).strip()
    clean = "_".join(clean.split())
    if not clean:
        return fallback_filename
    return f"{clean}_Resume.docx"


# --- ROUTES ---

@router.get("/")
async def serve_frontend():
    # This file is under active development — never let the browser cache a stale
    # copy of the editor's JS (e.g. a normal refresh silently serving old logic).
    return FileResponse(
        os.path.join(BASE_DIR, "frontend", "index.html"),
        headers={"Cache-Control": "no-store"},
    )

@router.get("/download.png")
async def serve_logo():
    path = os.path.join(BASE_DIR, "download.png")
    if not os.path.exists(path): raise HTTPException(status_code=404, detail="Logo not found")
    return FileResponse(path)

BADGE_DIR = os.path.join(BASE_DIR, "src", "certificates_badges")

@router.get("/certificates_badges/{image}")
async def serve_badge(image: str):
    # Only the bundled badge images, never an arbitrary path.
    if image not in docx_sf.CERT_BADGE_MAP.values():
        raise HTTPException(status_code=404, detail="Badge not found")
    return FileResponse(os.path.join(BADGE_DIR, image))

@router.get("/health")
async def health():
    return {"status": "ok", "active_jobs": len(job_status)}

@router.post("/api/extract-jd-text")
async def extract_jd_text(jd_file: UploadFile = File(...)):
    file_bytes = await jd_file.read()
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)}MB)")
    try:
        text = await _run_blocking(jd_extractor.extract_text_from_upload, file_bytes, jd_file.filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Failed to extract JD text")
        raise HTTPException(status_code=500, detail=f"Failed to extract text: {_safe_error_message(e)}")

    if not text.strip():
        raise HTTPException(status_code=400, detail="No extractable text found in this file.")

    return {"text": text.strip()}

@router.post("/api/convert-resume", response_model=ConversionResponse)
async def convert_single_resume(
    background_tasks: BackgroundTasks,
    resume_file: UploadFile = File(...),
    template_choice: str = Form(DEFAULT_TEMPLATE),
    output_name: Optional[str] = Form(None),
    job_description: Optional[str] = Form(None),
    extra_instructions: Optional[str] = Form(None)
):
    template_choice = _resolve_template_choice(template_choice)
    file_ext = os.path.splitext(resume_file.filename or "")[1].lower()
    if file_ext not in ALLOWED_RESUME_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Please upload the resume as a PDF or DOCX file.")
    sweep_abandoned_jobs()
    
    # 1. Setup Job
    job_id = str(uuid.uuid4())
    temp_dir = _job_dir(job_id)
    os.makedirs(temp_dir, exist_ok=True)

    # 2. Save File
    resume_path = os.path.join(temp_dir, f"uploaded_resume{file_ext}")
    try:
        _save_upload_enforcing_limit(resume_file.file, resume_path)
    except HTTPException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    # 3. Determine Output Name
    if output_name:
        clean = "".join(c for c in output_name if c.isalnum() or c in (' ', '-', '_')).rstrip().replace(' ', '_')
    else:
        original = os.path.splitext(resume_file.filename)[0]
        clean = "".join(c for c in original if c.isalnum() or c in (' ', '-', '_')).rstrip().replace(' ', '_')

    docx_filename = f"converted_resume_{clean}.docx"
    final_path = os.path.join(temp_dir, docx_filename)
    template_path = os.path.join(BASE_DIR, "templates", TEMPLATES[template_choice]["file"])

    # 4. Initialize Status
    job_status[job_id] = {
        "status": "processing",
        "progress": "Starting...",
        "file_count": 1,
        "docx_filename": docx_filename,
        "choice": template_choice,
        "created_at": time.time(),
        "last_active": time.time(),
    }

    # 5. Background Task — extraction only; DOCX is generated after user review via /api/finalize-resume
    background_tasks.add_task(
        worker_single_extract,
        job_id, resume_path, template_path, final_path, template_choice, job_description, extra_instructions
    )

    return ConversionResponse(job_id=job_id, status="processing", message="Started", download_url=f"/api/download-docx/{job_id}", file_count=1)

@router.get("/api/preview/{job_id}")
async def get_preview(job_id: str):
    data = _require_job(job_id, require_status=("ready_for_edit", "completed"))
    if "temp_html" not in data:
        raise HTTPException(status_code=400, detail=f"Preview not ready (status={data['status']})")
    with open(data["temp_html"], "r", encoding="utf-8") as f:
        html = f.read()
    return Response(content=html, media_type="text/html")

@router.post("/api/finalize-resume/{job_id}", response_model=ConversionResponse)
async def finalize_resume(job_id: str, background_tasks: BackgroundTasks, edited_html: str = Form(...)):
    data = _require_job(job_id, require_status=("ready_for_edit", "completed"))

    with open(data["temp_html"], "w", encoding="utf-8") as f:
        f.write(edited_html)

    job_status[job_id]["status"] = "processing"
    job_status[job_id]["progress"] = "Generating DOCX..."
    background_tasks.add_task(worker_single_finalize, job_id)

    return ConversionResponse(job_id=job_id, status="processing", message="Finalizing", download_url=f"/api/download-docx/{job_id}", file_count=1)

def _get_html_path_for_job(data):
    if "temp_html" not in data:
        raise HTTPException(status_code=400, detail=f"Preview not ready (status={data['status']})")
    return data["temp_html"]

def _keyword_term(kw):
    return kw["term"] if isinstance(kw, dict) else kw

@router.get("/api/keyword-status/{job_id}")
async def get_keyword_status(job_id: str):
    data = _require_job(job_id, require_status=("ready_for_edit", "completed"))

    jd_keywords = data.get("jd_keywords") or []
    html_path = _get_html_path_for_job(data)
    with open(html_path, "r", encoding="utf-8") as f:
        html_content = f.read()

    resume_skills = keyword_tools.extract_resume_keywords(html_content)
    
    if jd_keywords:
        added, missing = keyword_tools.compute_keyword_match(html_content, jd_keywords)
    else:
        added = resume_skills
        missing = []

    return {
        "jd_keywords": jd_keywords,
        "added": added,
        "missing": missing,
        "resume_skills": resume_skills,
        "has_jd": bool(data.get("job_description")),
        "job_description": data.get("job_description") or ""
    }

@router.post("/api/analyze-jd-keywords/{job_id}")
async def analyze_jd_keywords(job_id: str, job_description: str = Form(...)):
    data = _require_job(job_id, require_status=("ready_for_edit", "completed"))

    if not job_description.strip():
        raise HTTPException(status_code=400, detail="Job description cannot be empty")

    keywords = await _run_blocking(keyword_tools.extract_jd_keywords, job_description)
    data["jd_keywords"] = keywords
    data["job_description"] = job_description

    html_path = _get_html_path_for_job(data)
    with open(html_path, "r", encoding="utf-8") as f:
        html_content = f.read()

    added, missing = keyword_tools.compute_keyword_match(html_content, keywords)
    resume_skills = keyword_tools.extract_resume_keywords(html_content)

    return {
        "jd_keywords": keywords,
        "added": added,
        "missing": missing,
        "resume_skills": resume_skills,
        "has_jd": True,
        "job_description": job_description
    }

@router.post("/api/apply-keywords/{job_id}")
async def apply_keywords(
    job_id: str,
    add_keywords: Optional[List[str]] = Form(None),
    remove_keywords: Optional[List[str]] = Form(None),
    custom_skills: Optional[List[str]] = Form(None),
):
    data = _require_job(job_id, require_status=("ready_for_edit", "completed"))

    html_path = _get_html_path_for_job(data)
    with open(html_path, "r", encoding="utf-8") as f:
        html_content = f.read()

    try:
        new_html = await _run_blocking(
            keyword_tools.apply_keyword_changes,
            html_content, add_keywords or [], remove_keywords or [], custom_skills or []
        )
    except Exception as e:
        logger.exception(f"apply_keyword_changes failed for job {job_id}")
        raise HTTPException(status_code=500, detail=_safe_error_message(e))
    new_html = strip_active_content(new_html)

    # Re-bold after weaving in new keywords so freshly-added skill mentions
    # get the same visual treatment as everything else — cheap regex pass,
    # not another AI call, and idempotent against already-bolded text.
    new_html = await _run_blocking(keyword_highlighter.dedupe_skill_lines, new_html)
    all_added_terms = [*(add_keywords or []), *(custom_skills or [])]
    new_html = await _run_blocking(keyword_highlighter.bold_skill_mentions, new_html, all_added_terms)

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(new_html)

    return {"status": "updated", "page_count": await _check_live_page_count(new_html, data.get("choice"))}

@router.post("/api/fit-to-2-pages/{job_id}")
async def fit_resume_to_2_pages(job_id: str):
    """User-triggered ("Fit to 2 pages" button, shown whenever a live edit
    reports page_count > 2): tightens bullet/summary wording across the
    whole resume via keyword_tools.condense_resume_to_fit_pages, keeping
    every bullet/section/entry and every skill currently listed in the
    Skills section — this changes wording, never structure, facts, or
    keywords. Runs once; if it's still over 2 pages afterward (genuinely
    too much real content even condensed), that's reported honestly rather
    than looping or dropping anything."""
    data = _require_job(job_id, require_status=("ready_for_edit", "completed"))
    html_path = _get_html_path_for_job(data)
    with open(html_path, "r", encoding="utf-8") as f:
        html_content = f.read()

    required_keywords = await _run_blocking(keyword_tools.extract_resume_keywords, html_content)

    try:
        new_html = await _run_blocking(keyword_tools.condense_resume_to_fit_pages, html_content, required_keywords)
    except Exception as e:
        logger.exception(f"Fit-to-2-pages failed for job {job_id}")
        raise HTTPException(status_code=500, detail=_safe_error_message(e))
    new_html = strip_active_content(new_html)

    new_html = await _run_blocking(keyword_highlighter.dedupe_skill_lines, new_html)
    new_html = await _run_blocking(keyword_highlighter.bold_skill_mentions, new_html, required_keywords)

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(new_html)

    return {"status": "updated", "html": new_html, "page_count": await _check_live_page_count(new_html, data.get("choice"))}

@router.post("/api/edit-section/{job_id}")
async def edit_section(
    job_id: str,
    section_id: str = Form(...),
    instruction: str = Form(...),
    edited_html: str = Form(...),
):
    data = _require_job(job_id, require_status=("ready_for_edit", "completed"))

    html_path = _get_html_path_for_job(data)

    try:
        new_html = await _run_blocking(
            section_editor.edit_section, edited_html, section_id, instruction
        )
    except Exception as e:
        logger.exception(f"edit_section failed for job {job_id}")
        raise HTTPException(status_code=500, detail=_safe_error_message(e))
    new_html = strip_active_content(new_html)

    # Dedupe in case this edit touched a skill-line (e.g. "add Kubernetes to
    # my skills") and it — or a case/spacing variant of it — was already
    # listed under a different category heading.
    new_html = await _run_blocking(keyword_highlighter.dedupe_skill_lines, new_html)

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(new_html)

    return {"status": "updated", "html": new_html, "page_count": await _check_live_page_count(new_html, data.get("choice"))}

@router.post("/api/add-section/{job_id}")
async def add_section(
    job_id: str,
    reference_section_id: str = Form(...),
    instruction: str = Form(...),
    edited_html: str = Form(...),
):
    data = _require_job(job_id, require_status=("ready_for_edit", "completed"))

    html_path = _get_html_path_for_job(data)

    try:
        new_html = await _run_blocking(
            section_editor.add_section, edited_html, reference_section_id, instruction
        )
    except Exception as e:
        logger.exception(f"add_section failed for job {job_id}")
        raise HTTPException(status_code=500, detail=_safe_error_message(e))
    new_html = strip_active_content(new_html)

    new_html = await _run_blocking(keyword_highlighter.dedupe_skill_lines, new_html)

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(new_html)

    return {"status": "added", "html": new_html, "page_count": await _check_live_page_count(new_html, data.get("choice"))}

@router.get("/api/job-status/{job_id}", response_model=JobStatusResponse)
async def get_job_status(job_id: str):
    data = _require_job(job_id)

    resp = JobStatusResponse(job_id=job_id, status=data["status"], progress=data["progress"])

    if data["status"] == "completed":
        resp.result = {"download_url": f"/api/download-docx/{job_id}"}
        if data.get("ats_warnings"): resp.result["ats_warnings"] = data["ats_warnings"]
        if data.get("page_count"): resp.result["page_count"] = data["page_count"]
    elif data["status"] == "error":
        resp.error = data.get("error")

    return resp

@router.post("/api/discard-job/{job_id}")
async def discard_job(job_id: str):
    """Called by the page when the user leaves (tab closed/refreshed, or
    "Back to upload"): deletes the upload, preview and generated files right
    away. POST so navigator.sendBeacon can reach it during page unload;
    always 200, since a job that's already gone is already discarded."""
    _discard_job(job_id)
    return {"status": "discarded"}

def _finished_docx_path(job_id):
    data = _require_job(job_id)
    docx_path = data.get("final_path")
    if not docx_path or not os.path.exists(docx_path):
        raise HTTPException(status_code=404, detail="DOCX not generated yet — finalize the resume first")
    return data, docx_path

@router.get("/api/download-docx/{job_id}")
async def download_docx(job_id: str):
    data, docx_path = _finished_docx_path(job_id)
    return FileResponse(path=docx_path, filename=data["docx_filename"])

@router.get("/api/download-pdf/{job_id}")
async def download_pdf(job_id: str):
    data, docx_path = _finished_docx_path(job_id)
    pdf_path = os.path.splitext(docx_path)[0] + ".pdf"
    if not os.path.exists(pdf_path) or os.path.getmtime(pdf_path) < os.path.getmtime(docx_path):
        try:
            await _run_blocking(pdf_converter.convert_docx_to_pdf_as, docx_path, pdf_path)
        except Exception as e:
            logger.exception(f"PDF conversion failed for job {job_id}")
            raise HTTPException(status_code=500, detail=f"PDF conversion failed: {_safe_error_message(e)}")

    return FileResponse(path=pdf_path, filename=os.path.splitext(data["docx_filename"])[0] + ".pdf")

# --- WORKERS (THE BRAINS) ---

async def _run_blocking(fn, *args):
    """Run a blocking (sync) function in the thread pool so the event loop stays free."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, fn, *args)


async def _check_live_page_count(html_content, choice=None):
    """Real (not estimated) page count for the resume as it stands right
    now — renders to DOCX/PDF at increasingly compact typography presets,
    same deterministic shrink-only check worker_single_finalize performs at
    download time (the template converter's COMPACT_LEVELS), so an edit that
    pushes the resume over 2 pages is surfaced immediately rather than only
    discovered after downloading. Content is never touched — only shrunk;
    if it's still over 2 pages even at the smallest preset, that's reported
    honestly, not silently forced. Uses scratch files only — never touches
    the job's real output DOCX/PDF. Returns None if conversion fails."""
    tmp_dir = tempfile.mkdtemp(prefix="page_check_")
    try:
        tmp_html = os.path.join(tmp_dir, "check.html")
        with open(tmp_html, "w", encoding="utf-8") as f:
            f.write(html_content)
        tmp_docx = os.path.join(tmp_dir, "check.docx")
        tmp_pdf = os.path.join(tmp_dir, "check.pdf")

        converter = _converter_for(choice)
        max_level = len(converter.COMPACT_LEVELS) - 1
        page_count = None
        for compact_level in range(max_level + 1):
            await _run_blocking(converter.convert_resume_to_docx, tmp_html, tmp_docx, compact_level)
            try:
                await _run_blocking(pdf_converter.convert_docx_to_pdf_as, tmp_docx, tmp_pdf)
                page_count = await _run_blocking(pdf_converter.count_pdf_pages, tmp_pdf)
            except Exception as e:
                logger.warning(f"Live page-count check failed: {e}")
                return None
            if page_count <= 2:
                break
        return page_count
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def worker_single_extract(job_id, resume_path, template_path, final_path, choice, job_description=None, extra_instructions=None):
    """Analyzes the JD's keywords (if a JD was given), then extracts the
    resume into the chosen template and pauses for review."""
    try:
        jd_keywords = []
        if job_description:
            job_status[job_id]["progress"] = "Analyzing JD keywords..."
            try:
                jd_keywords = await _run_blocking(keyword_tools.extract_jd_keywords, job_description)
            except Exception as e:
                logger.warning(f"JD keyword extraction failed for job {job_id}: {e}")
                jd_keywords = []

        job_status[job_id]["progress"] = "Extracting Data..."
        temp_html = os.path.join(os.path.dirname(resume_path), "temp.html")

        success = await _run_blocking(
            _parser_for(choice).extract_resume_data, resume_path, template_path, temp_html,
            job_description, extra_instructions
        )
        if not success:
            raise Exception("AI Extraction Failed")

        new_docx_filename = _derive_professional_filename(temp_html, job_status[job_id]["docx_filename"])
        if new_docx_filename != job_status[job_id]["docx_filename"]:
            job_status[job_id]["docx_filename"] = new_docx_filename
            final_path = os.path.join(os.path.dirname(resume_path), new_docx_filename)

        with open(temp_html, "r", encoding="utf-8") as f:
            html_content = f.read()
        # The extraction prompt sometimes categorizes the same skill under more
        # than one heading — deterministic dedup catches that regardless of how
        # the prompt behaves, before the cosmetic bolding pass runs.
        deduped_html = await _run_blocking(keyword_highlighter.dedupe_skill_lines, html_content)
        extra_bold_terms = [_keyword_term(kw) for kw in jd_keywords]
        bolded_html = await _run_blocking(keyword_highlighter.bold_skill_mentions, deduped_html, extra_bold_terms)
        if bolded_html != html_content:
            with open(temp_html, "w", encoding="utf-8") as f:
                f.write(bolded_html)

        job_status[job_id]["status"] = "ready_for_edit"
        job_status[job_id]["progress"] = "Ready for review"
        job_status[job_id]["temp_html"] = temp_html
        job_status[job_id]["final_path"] = final_path
        job_status[job_id]["choice"] = choice
        job_status[job_id]["jd_keywords"] = jd_keywords
        job_status[job_id]["job_description"] = job_description or ""
    except Exception as e:
        if job_id not in job_status:
            return  # user left mid-run and the job was discarded — nothing to report
        logger.exception(f"worker_single_extract failed for job {job_id}")
        job_status[job_id]["status"] = "error"
        job_status[job_id]["error"] = _safe_error_message(e)


async def worker_single_finalize(job_id):
    """Converts the (possibly user-edited) preview HTML into the final DOCX,
    then enforces the 2-page limit mechanically: render to PDF, count the
    real page count, and if it's over 2, regenerate the DOCX at a denser
    typography preset (smaller font/margins/spacing — see the template
    converter's COMPACT_LEVELS) and re-check, up to the smallest
    safe preset. Content is never touched here — only shrunk to fit, exactly
    like the app's earlier fix that stopped the AI from dropping bullets to
    hit a page target; this is the deterministic backstop for the same goal."""
    try:
        data = job_status[job_id]
        temp_html = data["temp_html"]
        final_path = data["final_path"]
        pdf_path = os.path.splitext(final_path)[0] + ".pdf"

        converter = _converter_for(data.get("choice"))
        page_count = None
        compact_level = 0
        max_level = len(converter.COMPACT_LEVELS) - 1
        for compact_level in range(max_level + 1):
            await _run_blocking(converter.convert_resume_to_docx, temp_html, final_path, compact_level)
            try:
                await _run_blocking(pdf_converter.convert_docx_to_pdf_as, final_path, pdf_path)
                page_count = await _run_blocking(pdf_converter.count_pdf_pages, pdf_path)
            except Exception as e:
                logger.warning(f"Page-count verification failed for job {job_id}: {e}")
                page_count = None
                break
            if page_count <= 2:
                break
            if compact_level < max_level:
                job_status[job_id]["progress"] = f"Resume is {page_count} pages — shrinking layout to fit 2 pages..."

        job_status[job_id]["page_count"] = page_count
        job_status[job_id]["compact_level"] = compact_level

        try:
            with open(temp_html, "r", encoding="utf-8") as f:
                html_content = f.read()
            job_status[job_id]["ats_warnings"] = await _run_blocking(
                ats_check.check_docx_parseability, final_path, html_content
            )
        except Exception as e:
            logger.warning(f"ATS parseability check failed for job {job_id}: {e}")

        job_status[job_id]["status"] = "completed"
        job_status[job_id]["progress"] = "Done"
    except Exception as e:
        if job_id not in job_status:
            return  # user left mid-run and the job was discarded — nothing to report
        logger.exception(f"worker_single_finalize failed for job {job_id}")
        job_status[job_id]["status"] = "error"
        job_status[job_id]["error"] = _safe_error_message(e)


app.include_router(router)

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)