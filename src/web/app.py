"""
FastAPI app for the Job Approval UI.

Endpoints:
  GET  /                       — serve index.html (Captain's Bridge dashboard)
  GET  /static/{file}          — CSS / fonts

  Auth (Phase 1.1 — session cookies):
  POST /api/v1/auth/login      — {user_id, password} → set cookie
  POST /api/v1/auth/logout     — clear cookie
  GET  /api/v1/auth/me         — current user info

  Jobs (per-user via session):
  GET  /api/jobs               — list all jobs (filterable by status)
  GET  /api/jobs/{id}          — single job with render paths
  POST /api/jobs/{id}/approve  — set status='approved', send Discord notify
  POST /api/jobs/{id}/skip     — set status='skipped'
  GET  /api/stats              — counts for the top bar / sidebar
  GET  /api/batches/{company}/files/{file}  — serve PDF previews
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

from .auth import (
    check_bootstrap_admin,
    clear_session_cookie,
    current_user_id,
    hash_password,
    set_session_cookie,
    verify_password,
)
from . import rate_limit
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
# Startup: ensure global users DB schema exists (Phase 1.2)
# ---------------------------------------------------------------------------

@app.on_event("startup")
def _ensure_users_schema() -> None:
    """Phase 1.2: create data/users.db if missing. Idempotent."""
    try:
        from src.db import users_db as _users_db
        _users_db.ensure_schema()
        log.info("users_db schema ready at %s", _users_db.USERS_DB)
    except Exception as exc:  # pragma: no cover — surface in logs only
        log.warning("could not init users_db: %s", exc)


# ---------------------------------------------------------------------------
# Per-user request state
# ---------------------------------------------------------------------------

@app.middleware("http")
async def attach_user_state(request: Request, call_next):
    """Resolve user_id from session cookie (production) or env var (CLI/cron).

    Order of precedence:
    1. Session cookie (signed via itsdangerous) — production users
    2. LARS_USER_ID env var — CLI scripts + cron
    3. Admin fallback — for safety (never fails the request)
    """
    from src.db.jobs_db import ADMIN_USER_ID, get_user_id
    # 1. Session cookie
    uid = current_user_id(request)
    # 2. Env var (CLI/cron)
    if not uid:
        uid = get_user_id()
    # 3. Admin fallback
    if not uid:
        uid = ADMIN_USER_ID
    request.state.user_id = uid
    return await call_next(request)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
# Auth helpers (Phase 1.1 — session cookies)
# ---------------------------------------------------------------------------

def _check_auth(request: Request) -> bool:
    """True if the request has a valid session cookie (or Basic legacy header)."""
    # New: session cookie
    if current_user_id(request):
        return True
    # Legacy: HTTP Basic (kept so Ye.r old curl scripts still work during migration)
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:]).decode()
            user, _, pwd = decoded.partition(":")
            if user == ADMIN_USER and pwd == ADMIN_PASS:
                # Auto-create a session for the legacy Basic auth caller
                # so the rest of the pipeline (which reads the cookie) works.
                request.state.user_id = "lars"
                return True
        except Exception:
            pass
    return False


def _require_auth(request: Request):
    if not _check_auth(request):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized — please sign in",
            headers={"WWW-Authenticate": 'Basic realm="Captain\'s Bridge"'},
        )


def _check_token(request: Request) -> bool:
    """Alternative auth via ?token=base64(user:pass) — used by PDF iframes
    that can't send cookies cross-origin. (Kept for backward compat with v2.)"""
    token = request.query_params.get("token", "")
    if not token:
        return False
    try:
        decoded = base64.b64decode(token).decode()
        user, _, pwd = decoded.partition(":")
        if user == ADMIN_USER and pwd == ADMIN_PASS:
            return True
    except Exception:
        pass
    return False


def _check_any_auth(request: Request) -> bool:
    """Accept session cookie, HTTP Basic, OR ?token= query param (iframes)."""
    return _check_auth(request) or _check_token(request)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _connect(request: Request = None) -> sqlite3.Connection:
    """Open a connection to the current user's jobs DB.

    Resolution order for the user_id:
    1. request.state.user_id (production, set by the auth middleware)
    2. LARS_USER_ID env var (CLI scripts + cron)
    3. Admin fallback (so the function never fails)

    For the admin user: prefer the module-level DB_PATH (test-monkeypatchable),
    otherwise fall through to connect_user_db (which auto-creates schema).
    For any other user: always go through connect_user_db (isolated DB + schema).
    """
    from src.db.jobs_db import ADMIN_USER_ID, connect_user_db, get_user_id
    env_user_id = os.getenv("LARS_USER_ID")
    request_user_id = None
    if request is not None and hasattr(request, "state") and hasattr(request.state, "user_id"):
        request_user_id = request.state.user_id
    effective_user_id = request_user_id or env_user_id or ADMIN_USER_ID

    if effective_user_id == ADMIN_USER_ID and str(DB_PATH) != str(_LEGACY_DB):
        # Tests monkeypatch DB_PATH to a temp file — honor that for the admin user
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        # Apply schema if missing (e.g. fresh test fixture)
        from src.db.jobs_db import _USER_JOBS_SCHEMA
        conn.executescript(_USER_JOBS_SCHEMA)
        conn.commit()
        return conn

    return connect_user_db(user_id=effective_user_id)


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


# ---------------------------------------------------------------------------
# Auth endpoints (Phase 1.1 — session cookies)
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    user_id: str
    password: str


@app.post("/api/v1/auth/login")
def auth_login(body: LoginRequest):
    """Verify credentials, set the session cookie, return the user info.

    Phase 1.1: only the bootstrap admin user (user_id='lars', env-var password).
    Phase 1.2: real users table + bcrypt (PBKDF2). Bootstrap admin still wins
    via check_bootstrap_admin — keeps existing scripts working unchanged.
    """
    # 1. Bootstrap admin — exact behaviour preserved
    if check_bootstrap_admin(body.user_id, body.password):
        response = JSONResponse(
            {
                "ok": True,
                "user": {
                    "id": body.user_id,
                    "name": "Lars Zimmermann" if body.user_id == "lars" else body.user_id,
                    "plan": "admin" if body.user_id == "lars" else "free",
                },
            }
        )
        set_session_cookie(response, body.user_id)
        return response

    # 2. Real users (Phase 1.2). user_id may be "lars" (admin in users DB)
    # or an email address. Look up by id first, fall back to email.
    from src.db import users_db as _users_db
    row = _users_db.get_user_by_id(body.user_id)
    if row is None and "@" in body.user_id:
        row = _users_db.get_user_by_email(body.user_id)
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not row["verified"]:
        raise HTTPException(
            status_code=403,
            detail="Email not verified — check your inbox for the 6-digit code",
        )
    response = JSONResponse(
        {
            "ok": True,
            "user": {
                "id": row["id"],
                "email": row["email"],
                "name": row["name"],
                "plan": row["plan"] or "free",
            },
        }
    )
    set_session_cookie(response, row["id"])
    return response


@app.post("/api/v1/auth/logout")
def auth_logout():
    """Clear the session cookie."""
    response = JSONResponse({"ok": True})
    clear_session_cookie(response)
    return response


# ---------------------------------------------------------------------------
# Phase 1.2 — signup, verify, forgot/reset (real users DB)
# ---------------------------------------------------------------------------

class SignupRequest(BaseModel):
    email: str
    password: str
    name: Optional[str] = None


class VerifyRequest(BaseModel):
    user_id: str
    code: str


class ForgotRequest(BaseModel):
    email: str


class ResetRequest(BaseModel):
    token: str
    new_password: str


def _client_ip(request: Request) -> str:
    """Best-effort client IP. Honors X-Forwarded-For for proxy setups."""
    fwd = request.headers.get("X-Forwarded-For")
    if fwd:
        return fwd.split(",")[0].strip()
    if request.client:
        return request.client.host or "unknown"
    return "unknown"


@app.post("/api/v1/auth/signup")
def auth_signup(body: SignupRequest, request: Request):
    """Create a new user row and emit a 6-digit verify code (console in dev).

    Returns:
      200 { ok, user_id, verify_url, warnings? }    on success
      400 { ok: false, error: 'invalid_email' }      on bad email
      409 { ok: false, error: 'email_taken' }       on duplicates
      429 { ok: false, error: 'rate_limited', retry_after }  on bucket empty

    The verify_url is the path the UI can open to show a "enter code" form.
    In dev mode the code itself is also logged to stdout (so the user can
    copy-paste without checking fake email servers).
    """
    from src.db import users_db as _users_db

    # 1. Email shape check
    if not _users_db.is_valid_email(body.email):
        return JSONResponse(
            {"ok": False, "error": "invalid_email"},
            status_code=400,
        )

    # 2. Rate limit (5 signups / IP / hour)
    ip = _client_ip(request)
    allowed, retry_after = rate_limit.rate_limit_signup(ip)
    if not allowed:
        return JSONResponse(
            {"ok": False, "error": "rate_limited", "retry_after": retry_after},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )

    # 3. Duplicate check (case-insensitive via normalise_email)
    if _users_db.email_taken(body.email):
        return JSONResponse(
            {"ok": False, "error": "email_taken"},
            status_code=409,
        )

    # 4. Build the row
    pwd_hash = hash_password(body.password)
    try:
        uid = _users_db.create_user(
            email=body.email,
            password_hash=pwd_hash,
            name=(body.name or None),
            plan="free",
        )
    except Exception as exc:
        # Defensive: another writer raced us on the unique index
        log.warning("signup insert failed: %s", exc)
        return JSONResponse(
            {"ok": False, "error": "email_taken"},
            status_code=409,
        )

    # 5. Issue verify code, log it
    code = _users_db.generate_verify_code()
    expires_at = _users_db.set_verify_code(uid, code)
    log.info(
        "[signup] %s code=%s expires=%s",
        body.email, code, expires_at,
    )
    print(
        f"\n⚓ CaptainApply verify code for {body.email}: {code}\n"
        f"   expires at {expires_at}\n"
        f"   user_id={uid}\n"
    )

    # 6. Soft warning for <10-char passwords (per spec: warn, don't fail)
    warnings: list[str] = []
    if _users_db.is_weak_password(body.password):
        warnings.append("password_too_short")

    return {
        "ok": True,
        "user_id": uid,
        "verify_url": f"/app/verify?user_id={uid}",
        "warnings": warnings,
    }


@app.post("/api/v1/auth/verify")
def auth_verify(body: VerifyRequest):
    """Consume a 6-digit code, mark the user verified, set a session cookie.

    Returns:
      200 { ok, user }  on success (Set-Cookie attached)
      400 { ok: false, error: 'invalid_code' | 'too_many_attempts' | 'expired' }
    """
    from src.db import users_db as _users_db

    user = _users_db.get_user_by_verify(body.user_id)
    if user is None:
        # Either no code on file, or already verified (and code was cleared).
        # The "already verified" case should still succeed as a no-op.
        existing = _users_db.get_user_by_id(body.user_id)
        if existing and existing["verified"]:
            response = JSONResponse(
                {"ok": True, "user": _users_db.user_to_dict(existing), "already_verified": True}
            )
            set_session_cookie(response, existing["id"])
            return response
        return JSONResponse(
            {"ok": False, "error": "no_pending_verification"},
            status_code=400,
        )

    if user["verify_invalidated"]:
        return JSONResponse(
            {"ok": False, "error": "too_many_attempts"},
            status_code=400,
        )

    # Expiry check
    expires_at = user["verify_expires_at"]
    if expires_at:
        from datetime import datetime, timezone
        try:
            if datetime.now(timezone.utc) > datetime.fromisoformat(expires_at):
                return JSONResponse(
                    {"ok": False, "error": "expired"},
                    status_code=400,
                )
        except Exception:
            pass

    # Code match (constant-time)
    import hmac as _hmac
    if not _hmac.compare_digest(str(user["verify_code"] or ""), str(body.code)):
        attempts = _users_db.increment_verify_attempts(body.user_id)
        if attempts >= _users_db.MAX_VERIFY_ATTEMPTS:
            return JSONResponse(
                {"ok": False, "error": "too_many_attempts"},
                status_code=400,
            )
        return JSONResponse(
            {"ok": False, "error": "invalid_code"},
            status_code=400,
        )

    # Success
    _users_db.mark_verified(body.user_id)
    refreshed = _users_db.get_user_by_id(body.user_id)
    if refreshed is None:
        # Race: user deleted between code check and mark_verified. Should never
        # happen in single-process dev but be defensive.
        raise HTTPException(status_code=500, detail="user vanished mid-verify")
    response = JSONResponse(
        {"ok": True, "user": _users_db.user_to_dict(refreshed)}
    )
    set_session_cookie(response, body.user_id)
    return response


@app.post("/api/v1/auth/forgot")
def auth_forgot(body: ForgotRequest):
    """Generate a reset token (if the email exists) and always return 200.

    Never reveals whether the email exists — that's the whole point of the
    "always 200" shape. Token is printed to console in dev mode.
    """
    from src.db import users_db as _users_db

    user = _users_db.get_user_by_email(body.email)
    if user is not None:
        token = _users_db.generate_reset_token()
        expires_at = _users_db.set_reset_token(user["id"], token)
        log.info(
            "[forgot] %s token=%s expires=%s",
            body.email, token, expires_at,
        )
        print(
            f"\n⚓ CaptainApply reset token for {body.email}: {token}\n"
            f"   expires at {expires_at}\n"
        )
    # No 'verify_url' — the token IS the URL.
    return {"ok": True}


@app.post("/api/v1/auth/reset")
def auth_reset(body: ResetRequest):
    """Consume a reset token and write the new password hash."""
    from src.db import users_db as _users_db

    user = _users_db.consume_reset_token(body.token)
    if user is None:
        return JSONResponse(
            {"ok": False, "error": "invalid_or_expired_token"},
            status_code=400,
        )
    if _users_db.is_weak_password(body.new_password):
        return JSONResponse(
            {"ok": False, "error": "password_too_short"},
            status_code=400,
        )
    new_hash = hash_password(body.new_password)
    _users_db.update_password(user["id"], new_hash)
    _users_db.clear_reset_token(user["id"])
    return {"ok": True}


# ---------------------------------------------------------------------------


@app.get("/api/v1/auth/me")
def auth_me(request: Request):
    """Return info about the currently signed-in user (or 401)."""
    uid = current_user_id(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Not signed in")
    # Phase 1.1: bootstrap admin still wins
    if uid == "lars":
        return {
            "id": "lars",
            "name": "Lars Zimmermann",
            "plan": "admin",
            "email": "lars.z@icloud.com",
        }
    # Phase 1.2: real users table
    from src.db import users_db as _users_db
    row = _users_db.get_user_by_id(uid)
    if row is not None:
        return {
            "id": row["id"],
            "email": row["email"],
            "name": row["name"] or "",
            "plan": row["plan"] or "free",
            "verified": bool(row["verified"]),
        }
    # Unknown UID — treat as signed-out
    raise HTTPException(status_code=401, detail="Not signed in")


# ---------------------------------------------------------------------------
# Phase 1.4 — Stripe billing (Checkout + Customer Portal + webhook)
# ---------------------------------------------------------------------------

class CheckoutRequest(BaseModel):
    plan: str  # "solo" or "pro"


def _billing_user_id(request: Request) -> str:
    """Resolve a billing-scoped user_id.

    The billing endpoints require a logged-in user (or at least the
    bootstrap admin via legacy Basic auth) — they cannot run anonymously
    because we need a user_id to attach the Stripe customer to.
    """
    if not _check_auth(request):
        raise HTTPException(
            status_code=401,
            detail="Sign in to manage billing",
            headers={"WWW-Authenticate": 'Basic realm="Captain\'s Bridge"'},
        )
    return request.state.user_id


@app.post("/api/v1/billing/checkout")
def billing_checkout(body: CheckoutRequest, request: Request):
    """Create a Checkout session for the requested plan.

    Returns ``{"url": ...}`` — either a real Stripe URL or a mock URL
    in dev mode that points at /api/v1/billing/mock-checkout.
    """
    uid = _billing_user_id(request)
    if body.plan not in ("solo", "pro"):
        raise HTTPException(
            status_code=400,
            detail="plan must be 'solo' or 'pro'",
        )
    from src.billing import stripe_client as _stripe
    session = _stripe.create_checkout_session(
        user_id=uid,
        plan=body.plan,
    )
    return {"ok": True, "url": session["url"], "session_id": session["session_id"]}


@app.post("/api/v1/billing/portal")
def billing_portal(request: Request):
    """Create a Customer Portal session for the current user."""
    uid = _billing_user_id(request)
    from src.billing import stripe_client as _stripe
    session = _stripe.create_customer_portal_session(user_id=uid)
    return {"ok": True, "url": session["url"], "session_id": session["session_id"]}


@app.get("/api/v1/billing/mock-checkout")
def billing_mock_checkout(
    request: Request,
    session_id: str,
    user_id: str = "",
    plan: str = "solo",
):
    """DEV-MODE-ONLY confirmation page that synthesises a webhook.

    The marketing page links /api/v1/billing/checkout → mock URL → this
    endpoint. We render a tiny "thanks" page, build a fake
    ``checkout.session.completed`` event, and run it through
    ``handle_event`` so the user's plan row is updated exactly the way
    a real Stripe webhook would have done.
    """
    from src.billing import stripe_client as _stripe
    from src.billing.stripe_client import _is_dev_mode
    if not _is_dev_mode():
        # In production this endpoint should never be hit — it would be
        # a sign someone is trying to forge a subscription.
        raise HTTPException(status_code=404, detail="not found")

    if not user_id:
        user_id = _billing_user_id(request)
    if plan not in ("solo", "pro"):
        plan = "solo"

    # Synthesise the same shape Stripe sends for checkout.session.completed
    fake_event = {
        "id": f"evt_dev_{session_id}",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": session_id,
                "mode": "subscription",
                "client_reference_id": user_id,
                "metadata": {"user_id": user_id, "plan": plan},
                "line_items": {
                    "data": [
                        {
                            "price": {"id": _stripe.PLAN_TO_PRICE.get(plan, "")},
                        }
                    ]
                },
            }
        },
    }
    _stripe.handle_event(fake_event)

    return HTMLResponse(
        f"""<!doctype html>
<html><head><meta charset='utf-8'><title>Mock checkout — CaptainApply</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
          background: #FAFAFA; color: #0F172A; margin: 0; padding: 60px 20px; }}
  .card {{ max-width: 480px; margin: 0 auto; background: #fff;
           border: 1px solid #E2E8F0; border-radius: 12px; padding: 32px;
           box-shadow: 0 4px 12px rgba(15,23,42,0.05); }}
  h1 {{ font-size: 22px; margin: 0 0 8px; }}
  p {{ color: #64748B; line-height: 1.5; }}
  .badge {{ display: inline-block; background: #10B98122; color: #10B981;
            padding: 2px 10px; border-radius: 99px; font-size: 12px;
            font-weight: 500; margin-bottom: 16px; }}
  a {{ display: inline-block; margin-top: 24px; background: #0F172A;
        color: #fff; text-decoration: none; padding: 10px 20px;
        border-radius: 6px; font-weight: 500; }}
  code {{ background: #F1F5F9; padding: 1px 6px; border-radius: 4px; font-size: 12px; }}
</style></head>
<body>
  <div class='card'>
    <div class='badge'>DEV MODE</div>
    <h1>Welcome aboard, Captain! ⚓</h1>
    <p>Your <b>{plan}</b> plan is now active. (No card was charged — this is the mock-checkout flow.)</p>
    <p>session: <code>{session_id}</code></p>
    <a href='/app/?upgraded=1'>Open the Bridge →</a>
  </div>
</body></html>""",
        status_code=200,
    )


@app.post("/api/v1/billing/webhook")
async def billing_webhook(request: Request):
    """Stripe webhook receiver. Verifies signature, dispatches to handler.

    Note: this endpoint is NOT auth-gated by _check_auth — Stripe
    authenticates the request via the signature header, not a session.
    """
    payload = await request.body()
    sig_header = request.headers.get("Stripe-Signature", "")
    from src.billing import stripe_client as _stripe
    try:
        event = _stripe.verify_webhook(payload, sig_header)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"webhook invalid: {exc}")
    try:
        _stripe.handle_event(event)
    except Exception as exc:
        # Don't 500 — Stripe will retry on non-2xx, which we don't want
        # for logic bugs. Log and return 200.
        log.exception("[stripe] handle_event failed: %s", exc)
    return {"received": True}


@app.get("/api/v1/billing/me")
def billing_me(request: Request):
    """Return the current user's plan + limits + usage block.

    Used by the dashboard to render "5 / 10 jobs used" + the upgrade CTA.
    """
    uid = _billing_user_id(request)
    from src.billing import quota as _quota
    from src.billing import stripe_client as _stripe
    plan = _quota.get_user_plan(uid)
    limits = _quota.get_plan_limits(plan)
    # Best-effort current usage (jobs row count + today's render count).
    jobs_count = 0
    renders_today = 0
    try:
        conn = _connect(request)
        try:
            jobs_count = conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()["c"]
        finally:
            conn.close()
    except Exception:
        pass
    usage = _quota.build_usage_block(uid, jobs_count, renders_today)
    return {
        "user_id": uid,
        "plan": plan,
        "limits": limits,
        "usage": usage,
        "dev_mode": _stripe._is_dev_mode(),
    }


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    """Serve the marketing landing page (Phase 1.3 — public).

    The dashboard lives at /app — see app_dashboard() below.
    """
    marketing_path = STATIC_DIR / "marketing.html"
    if not marketing_path.exists():
        return HTMLResponse("<h1>CaptainApply</h1><p>Landing page not built yet.</p>")
    return HTMLResponse(marketing_path.read_text())


@app.get("/app/", response_class=HTMLResponse)
def app_dashboard():
    """The Captain's Bridge dashboard — protected by session auth."""
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
    jobs = _fetch_jobs(request, status_filter=status, limit=limit)
    # Phase 1.4: attach quota usage to the response (soft-warn, no 403).
    from src.billing import quota as _quota
    try:
        plan = _quota.get_user_plan(request.state.user_id)
        limits = _quota.get_plan_limits(plan)
        jobs_cap = limits["max_jobs"]
        near = (jobs_cap > 0 and len(jobs) >= int(jobs_cap * 0.8))
        exhausted = (jobs_cap > 0 and len(jobs) >= jobs_cap)
        return JSONResponse(
            jobs,
            headers={
                "X-Plan": plan,
                "X-Usage-Jobs-Used": str(len(jobs)),
                "X-Usage-Jobs-Cap": str(jobs_cap),
                "X-Usage-Near-Limit": "1" if near else "0",
                "X-Usage-Exhausted": "1" if exhausted else "0",
            },
        )
    except Exception:
        return jobs


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
# Phase 1.7 — XING Apply Assistant (semi-automated)
# ---------------------------------------------------------------------------
# Mounts the XING apply router which exposes:
#   GET  /app/xing/<job_id>                  — helper page (HTML)
#   GET  /api/v1/xing/fill/<job_id>          — field values (JSON)
#   POST /api/v1/xing/ready/<job_id>         — flip status to 'queued'
#   GET  /api/v1/xing/attachment/<job>/<k>  — serve CV/CL/message for download
# Phase 1.7 import is deferred to here so that xing_apply.py can see the
# auth + DB helpers it needs from this module (avoiding a circular import
# at module-load time).
from . import xing_apply as _xing_apply
app.include_router(_xing_apply.router)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}
