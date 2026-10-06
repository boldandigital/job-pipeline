"""
Session-cookie authentication for CaptainApply.

Phase 1.1 replaces HTTP Basic with a proper session system:
  - POST /api/v1/auth/login     → { email, password } → set cookie + user
  - POST /api/v1/auth/logout    → clear cookie
  - GET  /api/v1/auth/me        → current user info (used by the UI)
  - POST /api/v1/auth/signup    → { email, password, name } → new user
  - GET  /api/v1/auth/verify/{token} → email verification (Phase 1.2)

Cookie shape: signed via itsdangerous. JWT-style payload:
  {
    "uid": "<user_id>",          # For Phase 1 = "lars" (admin bootstrap)
                                   # Phase 1.2 = UUID
    "exp": 1234567890             # Unix timestamp, 7-day expiry
  }
Cookie name: captain_session (httponly, samesite=lax, secure in prod)
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException, Request, status

# Sign cookies with this secret. In production, MUST be set via env (44+ char).
SESSION_SECRET = os.getenv("CAPTAIN_SESSION_SECRET", "dev-only-do-not-use-in-prod-" * 2)
COOKIE_NAME = "captain_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 7  # 7 days


def _sign(payload: dict[str, Any]) -> str:
    """Sign a payload dict → base64.token string."""
    import json as _json
    import base64
    raw = _json.dumps(payload, separators=(",", ":")).encode()
    sig = hmac.new(SESSION_SECRET.encode(), raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=") + "." + base64.urlsafe_b64encode(sig).decode().rstrip("=")


def _unsign(token: str) -> Optional[dict[str, Any]]:
    """Verify + decode a signed token. Returns None on tamper/expiry."""
    import base64
    import json as _json
    try:
        raw_b64, sig_b64 = token.split(".", 1)
        # Re-pad
        raw_b64 += "=" * ((4 - len(raw_b64) % 4) % 4)
        sig_b64 += "=" * ((4 - len(sig_b64) % 4) % 4)
        raw = base64.urlsafe_b64decode(raw_b64)
        sig = base64.urlsafe_b64decode(sig_b64)
        expected = hmac.new(SESSION_SECRET.encode(), raw, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = _json.loads(raw.decode())
        if payload.get("exp", 0) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


def make_session_cookie(user_id: str) -> str:
    """Build a session cookie value (token string) for the given user."""
    return _sign({"uid": user_id, "exp": int(time.time()) + COOKIE_MAX_AGE})


def set_session_cookie(response, user_id: str) -> None:
    """Attach the session cookie to a Response object."""
    is_prod = os.getenv("CAPTAIN_PROD", "0") == "1"
    response.set_cookie(
        key=COOKIE_NAME,
        value=make_session_cookie(user_id),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        secure=is_prod,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def current_user_id(request: Request) -> Optional[str]:
    """Resolve the current user_id from the session cookie.

    Returns None if no/invalid cookie. Use this in endpoints that should
    be auth-required but respond 401 themselves.
    """
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    payload = _unsign(token)
    if not payload:
        return None
    return payload.get("uid")


def require_user(request: Request) -> str:
    """FastAPI dependency: extract user_id from session, 401 if missing."""
    uid = current_user_id(request)
    if not uid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not signed in",
        )
    return uid


# ---------------------------------------------------------------------------
# Bootstrap admin auth (Phase 1 — single user, env-var password)
# ---------------------------------------------------------------------------

def check_bootstrap_admin(user_id: str, password: str) -> bool:
    """Verify the bootstrap admin (Lars) credentials.

    Phase 1.1: a single user_id (== "lars") with env-var password.
    Phase 1.2: replaced by a real users table + bcrypt hashes.
    """
    if user_id != "lars":
        return False
    expected = os.getenv("LARS_PASS", "captain")
    return hmac.compare_digest(password.encode(), expected.encode())


# ---------------------------------------------------------------------------
# Password hashing (Phase 1.2 will use this for real users)
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """Hash a password with a random salt (PBKDF2-HMAC-SHA256, 100k iterations).

    Format: pbkdf2$100000$<salt_b64>$<hash_b64>
    """
    import base64
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)
    return f"pbkdf2$100000${base64.b64encode(salt).decode()}${base64.b64encode(derived).decode()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a password against a stored hash."""
    import base64
    try:
        algo, iters, salt_b64, hash_b64 = stored_hash.split("$", 3)
        if algo != "pbkdf2":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iters))
        return hmac.compare_digest(derived, expected)
    except Exception:
        return False
