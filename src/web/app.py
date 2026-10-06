"""
FastAPI app for the Job Approval UI.

Endpoints:
  GET  /                       — serve index.html (Captain's Bridge dashboard)
  GET  /static/{file}          — CSS / fonts
  GET  /api/jobs               — list all jobs (filterable by status)
  GET  /api/jobs/{id}          — single job with render paths
  POST /api/jobs/{id}/approve  — set status='approved', send Discord notify
  POST /api/jobs/{id}/skip     — set status='skipped'
  GET  /api/stats              — counts for the top bar / sidebar
  GET  /api/batches/{company}/files/{file}  — serve PDF previews

Auth: simple HTTP Basic (single user = Lars). For localhost only.
"""
from __future__ import annotations

import base64
import logging
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .template import index_page

log = logging.getLogger("web.app")

# Paths
PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = PROJECT_ROOT / "web" / "static"
# Per-user paths (Phase 1 multi-tenant): admin user "lars" keeps legacy paths
from src.db.jobs_db import (
    ADMIN_USER_ID,
    LEGACY_BATCHES as _LEGACY_BATCHES,
    LEGACY_DB as _LEGACY_DB,
    get_user_batches_dir,
    get_user_db_path,
)
# Module-level aliases (legacy single-user, also used by tests for monkeypatch)
DB_PATH = _LEGACY_DB
BATCHES_DIR = _LEGACY_BATCHES
DISCORD_SCRIPT = PROJECT_ROOT / "scripts" / "discord_send.py"

# Auth (single-user for now, session-cookie in Phase 4) — read from .env or fallback
ADMIN_USER = os.getenv("LARS_USER", "lars")
ADMIN_PASS = os.getenv("LARS_PASS", "captain")

app = FastAPI(title="Job Approval UI", version="1.0.0")


# ---------------------------------------------------------------------------
# Per-user request state
# ---------------------------------------------------------------------------

@app.middleware("http")
async def attach_user_state(request: Request, call_next):
    """Resolve user_id from env (Phase 1) or session (Phase 4) on every request."""
    from src.db.jobs_db import get_user_id
    request.state.user_id = get_user_id(request=request)
    return await call_next(request)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _check_auth(request: Request) -> bool:
    """Validate HTTP Basic auth header."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode()
        user, _, pwd = decoded.partition(":")
        return user == ADMIN_USER and pwd == ADMIN_PASS
    except Exception:
        return False


def _require_auth(request: Request):
    if not _check_auth(request):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": 'Basic realm="Captain\'s Bridge"'},
        )


def _check_token(request: Request) -> bool:
    """Alternative auth via ?token=base64(user:pass) for iframe use."""
    token = request.query_params.get("token", "")
    if not token:
        return False
    try:
        decoded = base64.b64decode(token).decode()
        user, _, pwd = decoded.partition(":")
        return user == ADMIN_USER and pwd == ADMIN_PASS
    except Exception:
        return False


def _check_any_auth(request: Request) -> bool:
    """Accept either HTTP Basic header OR ?token= query param."""
    return _check_auth(request) or _check_token(request)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _connect(request: Request = None) -> sqlite3.Connection:
    """Open a connection to the current user's jobs DB.

    Phase 1 single-tenant: uses the module-level DB_PATH (test-monkeypatchable).
    Phase 4 multi-tenant: will resolve user_id from request.state.user_id and
    return connect_user_db(user_id=...) instead. For now, the admin user
    already maps to the legacy DB_PATH via jobs_db.LEGACY_DB.
    """
    from src.db.jobs_db import ADMIN_USER_ID, get_user_id
    env_user_id = os.getenv("LARS_USER_ID")
    request_user_id = None
    if request is not None and hasattr(request, "state") and hasattr(request.state, "user_id"):
        request_user_id = request.state.user_id
    # If a per-user id is explicitly set in env/request AND it differs from
    # the admin user, route to that user's isolated DB (Phase 4 readiness).
    effective_user_id = request_user_id or env_user_id
    if effective_user_id and effective_user_id != ADMIN_USER_ID:
        from src.db.jobs_db import connect_user_db
        return connect_user_db(user_id=effective_user_id)
    # Default: use the module-level DB_PATH (which can be monkeypatched for tests,
    # and which equals LEGACY_DB = data/jobs.db for production).
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_job(row: sqlite3.Row, batches_dir: Path) -> Dict[str, Any]:
    """Map a DB row to a JSON-serialisable job dict the UI can consume.

    Search for the latest CV + Anschreiben PDFs across all daily batch dirs.
    """
    job_id = row["id"]
    company = row["company"] or "Unknown"
    company_slug = re_clean_slug(company)
    cv_path = None
    cl_path = None

    # Find the most recent batch dir that has CV/CL for this company
    # Sort dates descending so the newest is preferred
    if batches_dir.exists():
        # Look for any <date>/<company_slug>/CV_*.pdf and CL_*.pdf
        candidates = []
        for date_dir in batches_dir.iterdir():
            if not date_dir.is_dir() or date_dir.name.startswith("apply-"):
                continue
            job_dir = date_dir / company_slug
            if job_dir.is_dir():
                cv_files = sorted(job_dir.glob("CV_*.pdf"))
                cl_files = sorted(job_dir.glob("CL_*.pdf"))
                if cv_files or cl_files:
                    candidates.append((date_dir.name, job_dir, cv_files, cl_files))
        if candidates:
            # Most recent date first
            candidates.sort(key=lambda x: x[0], reverse=True)
            date_name, job_dir, cv_files, cl_files = candidates[0]
            if cv_files:
                cv_path = f"data/batches/{date_name}/{company_slug}/{cv_files[-1].name}"
            if cl_files:
                cl_path = f"data/batches/{date_name}/{company_slug}/{cl_files[-1].name}"

    return {
        "id": job_id,
        "title": row["title"] or "",
        "company": company,
        "location": row["location"] or "Remote",
        "url": row["url"] or "",
        "career_url": row["career_url"] or "",
        "score": int(row["score"] or 0),
        "source": row["source"] or "unknown",
        "status": row["status"] or "new",
        "description": _clean_description(row["description"] or "")[:2000],
        "salary": _guess_salary_band(row["title"] or "", row["description"] or ""),
        "cv_path": cv_path,
        "cover_letter_path": cl_path,
        "created_at": row["created_at"] or "",
        "updated_at": row["updated_at"] or "",
    }


import re


def re_clean_slug(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "_", s)[:30]


def _clean_description(text: str) -> str:
    """Strip noise prefixes from scraped descriptions."""
    # "About this job" / "Über diese Position" / "Job Description" headers
    text = re.sub(r"^(?:About this job|Über diese Position|Job Description|Aufgabe|Stelle)\s*\n+", "", text, flags=re.IGNORECASE)
    # XING noise markers
    text = re.sub(r"\n*(?:Ähnliche Jobs|Similar jobs|Job merken|Noch \d+ Tage?|Vor \d+ Tagen?)\s*\n*", "\n", text)
    return text.strip()


def _guess_salary_band(title: str, description: str) -> str:
    """Best-effort extract from description or fall back to expected band.

    Recognises:
      €60,000 – €75,000      (comma thousands)
      €60k – €75k           (k suffix)
      60.000 € – 75.000 €   (DACH dots)
      60k–75k               (no currency)
      60–75 thousand        (word "thousand"/tausend")
      50-60k                (trailing k)
    """
    text = (title + " " + description).lower()
    # €–€ patterns (most specific first)
    matches = re.findall(r"€\s*(\d{2,3}(?:[.,]\d{3})?)\s*[–\-]\s*€?\s*(\d{2,3}(?:[.,]\d{3})?)", text)
    for a, b in matches:
        if int(a.split(",")[0].split(".")[0]) >= 30:
            return f"€{a}–€{b}"
    # "k" patterns with both ends
    matches = re.findall(r"(\d{2,3})\s*k?\s*[–\-]\s*(\d{2,3})\s*k", text)
    if matches:
        a, b = matches[0]
        return f"€{a}k–€{b}k"
    # Trailing k: "50-60k"
    matches = re.findall(r"(\d{2,3})\s*-\s*(\d{2,3})\s*k\b", text)
    if matches:
        a, b = matches[0]
        return f"€{a}k–€{b}k"
    # "tausend" patterns
    matches = re.findall(r"(\d{2,3})\s*[–\-]\s*(\d{2,3})\s*tausend", text)
    if matches:
        a, b = matches[0]
        return f"€{a}k–€{b}k"
    # Fall back: title-based heuristic
    title_low = title.lower()
    if "found" in title_low or "intern" in title_low:
        return "TBD"
    return "€60k–€75k"  # Default based on user's salary floor


def _fetch_jobs(request: Request, status_filter: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    conn = _connect(request)
    try:
        if status_filter:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY score DESC LIMIT ?",
                (status_filter, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY score DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_job(r, _batches_dir_for(request)) for r in rows]
    finally:
        conn.close()


def _fetch_one_job(request: Request, job_id: int) -> Dict[str, Any]:
    conn = _connect(request)
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"job {job_id} not found")
        return _row_to_job(row, _batches_dir_for(request))
    finally:
        conn.close()


def _update_status(request: Request, job_id: int, new_status: str) -> None:
    conn = _connect(request)
    try:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
            (new_status, now, job_id),
        )
        conn.commit()
    finally:
        conn.close()


def _batches_dir_for(request: Request) -> Path:
    """Return the current user's batches directory.

    Honors the module-level BATCHES_DIR for tests (monkeypatchable).
    Routes to per-user dir if a non-admin user_id is set.
    """
    from src.db.jobs_db import ADMIN_USER_ID, get_user_batches_dir
    env_user_id = os.getenv("LARS_USER_ID")
    request_user_id = None
    if request is not None and hasattr(request, "state") and hasattr(request.state, "user_id"):
        request_user_id = request.state.user_id
    effective_user_id = request_user_id or env_user_id
    if effective_user_id and effective_user_id != ADMIN_USER_ID:
        return get_user_batches_dir(user_id=effective_user_id)
    return BATCHES_DIR


# ---------------------------------------------------------------------------
# Discord notify
# ---------------------------------------------------------------------------

def _discord_notify(action: str, job: Dict[str, Any]) -> None:
    """Send a one-line Discord notification when a job is approved/skipped."""
    if not DISCORD_SCRIPT.exists():
        log.warning("discord_send.py not found — skipping notify")
        return
    msg = f"⚓ Captain {action} **{job['title']}** at {job['company']} (score {job['score']})"
    try:
        subprocess.run(
            ["python3", str(DISCORD_SCRIPT), msg],
            check=False,
            timeout=10,
            capture_output=True,
        )
    except Exception as exc:
        log.warning("discord notify failed: %s", exc)


# ---------------------------------------------------------------------------
# Static / index
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
def root():
    return index_page()


# ---------------------------------------------------------------------------
# API: jobs
# ---------------------------------------------------------------------------

class ApproveRequest(BaseModel):
    note: Optional[str] = None


@app.get("/api/jobs")
def list_jobs(
    request: Request,
    status: Optional[str] = None,
    limit: int = 50,
):
    _require_auth(request)
    return _fetch_jobs(request, status_filter=status, limit=limit)


@app.get("/api/jobs/{job_id}")
def get_job(request: Request, job_id: int):
    _require_auth(request)
    return _fetch_one_job(request, job_id)


@app.post("/api/jobs/{job_id}/approve")
def approve_job(request: Request, job_id: int, body: ApproveRequest | None = None):
    _require_auth(request)
    job = _fetch_one_job(request, job_id)
    _update_status(request, job_id, "approved")
    _discord_notify("approved", job)
    return {"ok": True, "id": job_id, "status": "approved"}


@app.post("/api/jobs/{job_id}/skip")
def skip_job(request: Request, job_id: int):
    _require_auth(request)
    job = _fetch_one_job(request, job_id)
    _update_status(request, job_id, "skipped")
    _discord_notify("skipped", job)
    return {"ok": True, "id": job_id, "status": "skipped"}


# ---------------------------------------------------------------------------
# API: stats
# ---------------------------------------------------------------------------

@app.get("/api/stats")
def stats(request: Request):
    _require_auth(request)
    conn = _connect(request)
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        # Today
        new_today = conn.execute(
            "SELECT COUNT(*) as c FROM jobs WHERE date(created_at) = ?", (today,)
        ).fetchone()["c"]
        # Status counts
        rows = conn.execute(
            "SELECT status, COUNT(*) as c FROM jobs GROUP BY status"
        ).fetchall()
        by_status = {r["status"] or "new": r["c"] for r in rows}
        # High-fit (score >= 200)
        high_fit = conn.execute(
            "SELECT COUNT(*) as c FROM jobs WHERE score >= 200"
        ).fetchone()["c"]
        # Source breakdown (last 24h)
        source_rows = conn.execute(
            """SELECT source, COUNT(*) as c FROM jobs
               WHERE date(created_at) >= date('now', '-1 day')
               GROUP BY source"""
        ).fetchall()
        sources = {r["source"] or "unknown": r["c"] for r in source_rows}
        return {
            "today_new": new_today,
            "high_fit": high_fit,
            "by_status": by_status,
            "sources": sources,
            "salary_floor": 65000,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# API: serve PDFs (proxy via FastAPI so we can add auth)
# ---------------------------------------------------------------------------

@app.get("/api/batches/{path:path}")
def serve_batch_file(request: Request, path: str):
    """Serve a PDF from the current user's batches/.

    Accepts:
      - /api/batches/data/batches/<date>/<company>/CV_*.pdf  (full path from job.cv_path)
      - /api/batches/<date>/<company>/CV_*.pdf                 (date/company short form)
    """
    if not _check_any_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": 'Basic realm="Captain\'s Bridge"'})
    user_batches = _batches_dir_for(request)
    # Strip leading "data/batches/" if present (job.cv_path already includes it)
    if path.startswith("data/batches/"):
        path = path[len("data/batches/"):]
    full = (user_batches / path).resolve()
    if not str(full).startswith(str(user_batches.resolve())):
        raise HTTPException(status_code=403, detail="forbidden")
    if not full.exists() or not full.is_file():
        raise HTTPException(status_code=404, detail=f"file not found: {path}")
    if full.suffix.lower() != ".pdf":
        raise HTTPException(status_code=403, detail="only PDF allowed")
    return FileResponse(str(full), media_type="application/pdf")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}
