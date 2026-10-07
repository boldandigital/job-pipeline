"""
FastAPI routes for the credential store.

The web layer exposes the same surface as the CLI's `captainauth show` — it
NEVER returns a secret value over HTTP. The set/delete/login/rotate calls
that need a secret are wired here for in-browser use, but they take the same
secrets the CLI does and never echo them back.

Auth: every route here requires a valid session cookie, and the resolved
user_id is the only one whose credentials can be touched. Even an admin
session cannot read another user's status — the routes are per-user, not
per-instance.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request, status

from ..auth.capture import (
    CaptureError,
    LoginTimeout,
    PlaywrightMissing,
    capture_session,
)
from ..auth.credentials import (
    CredentialNotFoundError,
    CredentialStore,
    CredentialsError,
    NoSuchUserError,
    PlatformNotSupported,
    UnknownPlatform,
    auth_file_path,
    credentials_status,
    public_platforms,
    set_credential,
)
from ..auth.platforms import normalize_platform
# Importing `.app` at module top would re-enter app.py mid-init and break the
# include_router() call at its tail. `xing_apply` already solves this with
# lazy lookups via thin `_app_*` wrappers — we do the same here.
from .auth import current_user_id


def _app_check_auth(request: Request) -> bool:
    from .app import _check_auth
    return _check_auth(request)

log = logging.getLogger("web.auth_credentials")

router = APIRouter()


def _resolve_user_id(request: Request, override: Optional[str] = None) -> str:
    """Return the user_id allowed to act on this request.

    Order of precedence:
      1. An explicit session cookie (the only path for the production web UI)
      2. ``request.state.user_id``, set by the legacy HTTP-Basic backdoor in
         ``app._check_auth`` so curl-style callers still work during migration
      3. A 401 if neither resolves — the API never guesses.

    Cross-tenant requests (the caller claims to be a different user than the
    session belongs to) are rejected with 403 even for an admin session.
    """
    session_uid = current_user_id(request)
    state_uid = getattr(getattr(request, "state", None), "user_id", None)
    uid = session_uid or state_uid
    if not uid:
        raise HTTPException(status_code=401, detail="sign in first")
    if override and override != uid:
        # The CLI can do anything; the web layer cannot.
        raise HTTPException(
            status_code=403,
            detail=f"forbidden — session is '{uid}', "
                   f"cannot act for '{override}'",
        )
    return uid


def _auth_file_for(request: Request) -> str:
    """Optional per-request override of the envelope path.

    Reads `CAPTAIN_AUTH_DIR` env (mirrors the CLI's behaviour) but does not
    accept an arbitrary path from the client — that would be a credential
    exfiltration vector.
    """
    return str(auth_file_path())


# ---------------------------------------------------------------------------
# GET /api/v1/auth/credentials
# ---------------------------------------------------------------------------

@router.get("/api/v1/auth/credentials")
def list_credentials(request: Request, user_id: Optional[str] = None):
    """Return the current user's credential status — never the secrets."""
    if not _app_check_auth(request):
        raise HTTPException(status_code=401, detail="not signed in")
    uid = _resolve_user_id(request, user_id)
    try:
        return credentials_status(uid)
    except CredentialsError as exc:
        log.warning("credentials status failed: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc))


# ---------------------------------------------------------------------------
# GET /api/v1/auth/credentials/platforms — public catalog
# ---------------------------------------------------------------------------

@router.get("/api/v1/auth/credentials/platforms")
def list_platforms():
    return {"platforms": public_platforms()}


# ---------------------------------------------------------------------------
# POST /api/v1/auth/credentials  — store a credential
# ---------------------------------------------------------------------------

@router.post("/api/v1/auth/credentials")
async def create_credential(request: Request):
    """Store a credential for the current user.

    Body: ``{ "platform": "xing", "cookies": [...], "username": "...",
    "password": "...", "method": "cookies", "notes": "..." }``. At least one
    of `cookies` or (username + password) must be present.

    Returns the same secret-free status dict the GET endpoint produces.
    """
    if not _app_check_auth(request):
        raise HTTPException(status_code=401, detail="not signed in")
    body = await _read_json(request)
    platform = body.get("platform")
    if not platform:
        raise HTTPException(status_code=400, detail="missing 'platform'")
    try:
        uid = _resolve_user_id(request, body.get("user_id"))
    except HTTPException:
        raise
    try:
        set_credential(
            uid, platform,
            cookies=body.get("cookies"),
            username=body.get("username"),
            password=body.get("password"),
            method=body.get("method"),
            notes=body.get("notes", ""),
        )
    except PlatformNotSupported as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except UnknownPlatform as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except CredentialsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return credentials_status(uid)


# ---------------------------------------------------------------------------
# DELETE /api/v1/auth/credentials/{platform}
# ---------------------------------------------------------------------------

@router.delete("/api/v1/auth/credentials/{platform}")
def delete_credential(request: Request, platform: str):
    if not _app_check_auth(request):
        raise HTTPException(status_code=401, detail="not signed in")
    uid = _resolve_user_id(request)
    try:
        normalize_platform(platform)
    except UnknownPlatform as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        store = CredentialStore()
        store.delete(uid, platform)
    except (NoSuchUserError, CredentialNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except CredentialsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "user_id": uid, "deleted": platform}


# ---------------------------------------------------------------------------
# POST /api/v1/auth/credentials/{platform}/login — interactive browser capture
# ---------------------------------------------------------------------------

@router.post("/api/v1/auth/credentials/{platform}/login")
async def interactive_login(
    request: Request, platform: str, body: Optional[Dict[str, Any]] = None
):
    """Open a browser window, wait for the user to sign in, capture cookies.

    Body (all optional): ``{"timeout": 300, "force": false, "headless": false}``.
    On a real desktop session, the browser is the same one the user is
    working in (or visible alongside it). On a headless server, ``headless``
    is allowed and the cookies are still captured — the human just has to
    be in front of a VNC.
    """
    if not _app_check_auth(request):
        raise HTTPException(status_code=401, detail="not signed in")
    uid = _resolve_user_id(request)
    body = body or {}
    timeout = int(body.get("timeout", 300))
    headless = bool(body.get("headless", False))
    force = bool(body.get("force", False))
    browser_path = body.get("browser_path") or os.getenv("CAPTAIN_CHROMIUM_PATH")

    try:
        result = capture_session(
            platform, uid, timeout=timeout, headless=headless,
            force=force, browser_path=browser_path,
        )
    except PlatformNotSupported as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except UnknownPlatform as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except PlaywrightMissing as exc:
        raise HTTPException(status_code=501, detail=str(exc))
    except LoginTimeout as exc:
        raise HTTPException(status_code=408, detail=str(exc))
    except CaptureError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return result.summary()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _read_json(request: Request) -> Dict[str, Any]:
    """Read and validate a JSON body. Returns {} for an empty body."""
    if not request.headers.get("content-type", "").startswith("application/json"):
        return {}
    body = await request.body()
    if not body:
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400, detail=f"body is not valid JSON (line {exc.lineno})"
        )