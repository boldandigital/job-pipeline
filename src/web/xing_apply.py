"""
XING Apply Assistant — Phase 1.7 (semi-automated XING application helper).

Why this exists
---------------
The CUA-driven XING apply flow needs authenticated browser cookies (XING login
session). We do NOT have those in the server-side pipeline. The user explicitly
said "we will not auto-apply" to XING.

Instead, this module renders a side-by-side helper UI that:

  - Loads the XING job URL in an iframe (XING's actual application page).
  - Renders a control panel with all the field values the user needs to fill.
  - Provides a "Copy to clipboard" button so the user can paste them into
    XING's form fields (since the iframe is cross-origin and postMessage
    cannot cross the boundary).
  - Lists the 3 attachment files (CV, Anschreiben, XING message draft) with
    download paths the user can drag into the XING file inputs.
  - Exposes a `ready` endpoint that flips the DB status to 'queued' — the
    dashboard can then pick it up as "ready to submit manually".

Cross-origin reality
--------------------
xing.com is NOT localhost. The browser blocks postMessage from our parent
to XING's iframe content, AND XING sets X-Frame-Options that may block
embedding entirely. The HTML page documents this honestly and falls back to
a manual workflow: copy values, paste into the form, attach files, click
XING's own Submit. We never try to script the submit.

Endpoints
---------
  GET  /app/xing/<job_id>                 — render helper page (HTML)
  POST /api/v1/xing/fill/<job_id>         — return field values JSON
  POST /api/v1/xing/ready/<job_id>        — mark status='queued' in DB
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

# Import module-level constants from .app. These are safe because they are
# set when app.py's top level runs, BEFORE it reaches our include_router()
# call. We deliberately do NOT import the function helpers here (that would
# create a circular import); instead we look them up lazily at request time
# via the `_app_*` wrappers defined further down.
from .app import BATCHES_DIR, PROJECT_ROOT, re_clean_slug  # noqa: E402

log = logging.getLogger("web.xing_apply")

router = APIRouter()

# ---------------------------------------------------------------------------
# Field computation — pulled from config/lars-cv-data.json so the user only
# edits the JSON to update them everywhere.
# ---------------------------------------------------------------------------

CV_DATA_PATH = PROJECT_ROOT / "config" / "lars-cv-data.json"

# Hard-coded XING-specific asks (override the JSON for XING because the user
# keeps these as the XING default regardless of job).
XING_SALARY_DEFAULT = "100.000 - 130.000 EUR / year"
XING_START_DATE_DEFAULT = "Ab sofort / nach 4 Wochen Kündigungsfrist"


def _load_cv_data() -> Dict[str, Any]:
    """Load lars-cv-data.json (cached per process)."""
    if not CV_DATA_PATH.exists():
        log.warning("lars-cv-data.json missing at %s", CV_DATA_PATH)
        return {}
    try:
        with CV_DATA_PATH.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        log.warning("could not parse lars-cv-data.json: %s", exc)
        return {}


def _xing_message_body(draft_path: Path) -> str:
    """Extract the 'paste into XING Nachricht field' body from the draft.

    The draft files have a header block delimited by `-----` lines. We
    return everything after the LAST separator — that's the actual
    application message, not the metadata.
    """
    if not draft_path.exists():
        return ""
    text = draft_path.read_text(encoding="utf-8")
    # Split on the dashed separator lines; the body lives after the
    # second occurrence (first is the title, second is the section header).
    parts = re.split(r"-{5,}", text)
    # The first non-empty chunk is the title; the second is "Anschreiben / ..."
    # header; everything from the third onwards is the actual message.
    body_chunks = [p.strip() for p in parts[2:] if p.strip()]
    return "\n\n".join(body_chunks).strip()


def _build_xing_payload(job: Dict[str, Any], batches_dir: Path) -> Dict[str, Any]:
    """Compose the JSON payload the side panel consumes.

    Returns a dict with:
      - job:        { id, title, company, location, url, score }
      - fields:     { name, email, phone, salary, start_date, location }
      - message:    { filename, body }   (the XING draft text)
      - attachments: [ { kind, filename, path, download_url } ]
      - manual_steps: [ "...", ... ]      (instructions the page prints)
    """
    cv_data = _load_cv_data()
    personal = cv_data.get("personal", {}) if isinstance(cv_data, dict) else {}

    # Locate the batch dir for this job
    company_slug = re_clean_slug(job.get("company") or "")
    batch_dir: Optional[Path] = None
    if batches_dir.exists():
        for date_dir in sorted(batches_dir.iterdir(), reverse=True):
            if not date_dir.is_dir() or date_dir.name.startswith("apply-"):
                continue
            candidate = date_dir / company_slug
            if candidate.is_dir():
                batch_dir = candidate
                break

    attachments: List[Dict[str, Any]] = []
    message: Dict[str, str] = {"filename": "", "body": "", "path": ""}
    if batch_dir is not None:
        # CV (most recent first)
        cv_files = sorted(batch_dir.glob("CV_*.pdf"))
        if cv_files:
            cv = cv_files[-1]
            # Use just the path-from-batches_dir as the download URL, since
            # /api/batches/<path> takes a path relative to batches_dir.
            rel_to_batches = str(cv.relative_to(batches_dir))
            rel_to_root = (
                str(cv.relative_to(PROJECT_ROOT))
                if str(cv).startswith(str(PROJECT_ROOT))
                else str(cv)
            )
            attachments.append({
                "kind": "cv",
                "filename": cv.name,
                "path": rel_to_root,
                "download_url": f"/api/batches/{rel_to_batches}",
            })
        # Cover letter
        cl_files = sorted(batch_dir.glob("CL_*.pdf"))
        if cl_files:
            cl = cl_files[-1]
            rel_to_batches = str(cl.relative_to(batches_dir))
            rel_to_root = (
                str(cl.relative_to(PROJECT_ROOT))
                if str(cl).startswith(str(PROJECT_ROOT))
                else str(cl)
            )
            attachments.append({
                "kind": "cover_letter",
                "filename": cl.name,
                "path": rel_to_root,
                "download_url": f"/api/batches/{rel_to_batches}",
            })
        # XING message draft
        drafts = sorted(batch_dir.glob("xing_application_draft.txt"))
        if drafts:
            draft = drafts[-1]
            rel_to_root = (
                str(draft.relative_to(PROJECT_ROOT))
                if str(draft).startswith(str(PROJECT_ROOT))
                else str(draft)
            )
            message = {
                "filename": draft.name,
                "body": _xing_message_body(draft),
                "path": rel_to_root,
            }

    fields = {
        "name": personal.get("name") or "Lars Zimmermann",
        "email": personal.get("email") or "lars.z@icloud.com",
        "phone": personal.get("phone") or "+32 456 97 01 66",
        "location": personal.get("location") or "Aarschot, Flemish Region, Belgium",
        "salary": XING_SALARY_DEFAULT,
        "start_date": XING_START_DATE_DEFAULT,
    }

    manual_steps = [
        "1. Sign in to XING (x.com or xing.com) in a separate tab while logged in.",
        "2. Open the job URL below in that tab (XING blocks iframes when not signed in).",
        f"3. Click 'Easy Apply' / 'Schnell bewerben' / 'Jetzt bewerben' on the XING page.",
        "4. In the side panel here, click 'Copy all to clipboard' — paste the values into the XING form fields.",
        "5. Drag the CV and Anschreiben PDFs from the side panel into XING's file upload fields.",
        "6. Paste the XING message body (Nachricht / Anschreiben) into XING's textarea.",
        "7. Click 'Ready' below to mark this job as queued in the dashboard.",
        "8. Switch back to the XING tab and click their 'Bewerbung absenden' / 'Submit' button.",
    ]

    return {
        "job": {
            "id": job.get("id"),
            "title": job.get("title") or "",
            "company": job.get("company") or "",
            "location": job.get("location") or "",
            "url": job.get("url") or "",
            "score": job.get("score") or 0,
        },
        "fields": fields,
        "message": message,
        "attachments": attachments,
        "manual_steps": manual_steps,
    }


# ---------------------------------------------------------------------------
# Lazy lookup helpers — keep app.py's function-level imports out of our
# module-level imports to break the circular import.
# ---------------------------------------------------------------------------

def _batches_dir() -> Path:
    """Return the current BATCHES_DIR from .app (resolved per call so tests
    that monkeypatch `app.BATCHES_DIR` see the patched value)."""
    from . import app as _app
    return _app.BATCHES_DIR


def _app_check_auth(request: Request) -> bool:
    from .app import _check_auth
    return _check_auth(request)


def _app_require_auth(request: Request) -> None:
    from .app import _require_auth
    _require_auth(request)


def _app_fetch_one_job(request: Request, job_id: int) -> Dict[str, Any]:
    from .app import _fetch_one_job
    return _fetch_one_job(request, job_id)


def _app_update_status(request: Request, job_id: int, new_status: str) -> None:
    from .app import _update_status
    _update_status(request, job_id, new_status)


# ---------------------------------------------------------------------------
# Page route — GET /app/xing/<job_id>
# ---------------------------------------------------------------------------

@router.get("/app/xing/{job_id}", response_class=HTMLResponse)
def xing_apply_page(request: Request, job_id: int):
    """Render the XING Apply Assistant page.

    Auth: requires a valid session cookie (or Basic legacy header). The page
    is protected because it embeds the user's job URLs + personal data.
    """
    if not _app_check_auth(request):
        # Send to login. The page is HTML so we can't 401; redirect to the
        # marketing/login page instead.
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/", status_code=302)

    # Pre-compute the payload so the page is rendered with values inlined —
    # this also doubles as a 404 if the job_id doesn't exist.
    job = _app_fetch_one_job(request, job_id)
    payload = _build_xing_payload(job, _batches_dir())
    html_path = PROJECT_ROOT / "web" / "static" / "xing-apply.html"
    if not html_path.exists():
        return HTMLResponse(
            "<h1>XING Apply Assistant</h1>"
            "<p>Helper page template missing on disk.</p>",
            status_code=500,
        )
    template = html_path.read_text(encoding="utf-8")

    # Inline the payload as a JSON <script> tag so the page renders without
    # needing to call /api/v1/xing/fill separately (faster first paint, also
    # means the page is fully usable even if the JS fetch fails).
    payload_json = json.dumps(payload, ensure_ascii=False)
    rendered = template.replace(
        "__XING_PAYLOAD__",
        payload_json.replace("</", "<\\/"),
    )
    return HTMLResponse(rendered)


# ---------------------------------------------------------------------------
# API: GET the field values as JSON (used by the side-panel JS for refresh)
# ---------------------------------------------------------------------------

@router.get("/api/v1/xing/fill/{job_id}")
def xing_fill_get(request: Request, job_id: int):
    """Return the field values + attachments for a job as JSON.

    The HTML page inlines these at render time, so this endpoint is only
    needed if the user opens the dev tools and wants to refresh values
    without reloading the page.
    """
    _app_require_auth(request)
    job = _app_fetch_one_job(request, job_id)
    return _build_xing_payload(job, _batches_dir())


# ---------------------------------------------------------------------------
# API: POST ready — flips DB status to 'queued' so the dashboard knows the
# user has prepped the application and is about to click Submit manually.
# ---------------------------------------------------------------------------

@router.post("/api/v1/xing/ready/{job_id}")
def xing_mark_ready(request: Request, job_id: int):
    """Mark the job as ready_to_submit in the DB.

    Sets status='queued' and stamps updated_at. The dashboard's "approved"
    filter is separate from this; the user can re-flip to 'sent' after
    they actually click XING's Submit button.
    """
    _app_require_auth(request)
    # Verify the job exists (raises 404 if not).
    _app_fetch_one_job(request, job_id)
    _app_update_status(request, job_id, "queued")
    log.info("XING apply: job %s marked ready_to_submit (status=queued)", job_id)
    return {
        "ok": True,
        "id": job_id,
        "status": "queued",
        "next_step": "Switch to the XING tab and click Submit. Then mark as 'sent'.",
    }


# ---------------------------------------------------------------------------
# Convenience: download endpoints for the 3 attachments. The iframe can't
# attach files across origins, but the user can drag these URLs into the
# browser's downloads and then upload from disk.
# ---------------------------------------------------------------------------

@router.get("/api/v1/xing/attachment/{job_id}/{kind}")
def xing_attachment(request: Request, job_id: int, kind: str):
    """Serve the CV / CL / XING message draft for a given job.

    kind: 'cv' | 'cover_letter' | 'message'
    """
    _app_require_auth(request)
    job = _app_fetch_one_job(request, job_id)
    company_slug = re_clean_slug(job.get("company") or "")

    # Find the batch dir (newest first)
    target: Optional[Path] = None
    batches = _batches_dir()
    if batches.exists():
        for date_dir in sorted(batches.iterdir(), reverse=True):
            if not date_dir.is_dir() or date_dir.name.startswith("apply-"):
                continue
            candidate = date_dir / company_slug
            if not candidate.is_dir():
                continue
            if kind == "cv":
                matches = sorted(candidate.glob("CV_*.pdf"))
            elif kind == "cover_letter":
                matches = sorted(candidate.glob("CL_*.pdf"))
            elif kind == "message":
                matches = sorted(candidate.glob("xing_application_draft.txt"))
            else:
                raise HTTPException(status_code=400, detail=f"unknown kind: {kind}")
            if matches:
                target = matches[-1]
                break

    if target is None or not target.exists():
        raise HTTPException(
            status_code=404,
            detail=f"attachment '{kind}' not found for job {job_id}",
        )

    if kind == "message":
        return FileResponse(
            str(target),
            media_type="text/plain",
            filename=target.name,
        )
    return FileResponse(
        str(target),
        media_type="application/pdf",
        filename=target.name,
    )
