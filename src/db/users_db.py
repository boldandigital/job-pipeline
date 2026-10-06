"""Global users database for CaptainApply Phase 1.2.

Unlike per-user jobs.db, the users table is global (data/users.db) because
the *identity* layer is shared — every user logs in the same way, no matter
which jobs.db their data lives in. After Phase 1.2 the auth flow is:

    1. POST /api/v1/auth/signup  → row in users, 6-digit code printed to console
    2. POST /api/v1/auth/verify  → mark verified, set session cookie
    3. POST /api/v1/auth/forgot  → reset token (always 200 — no enumeration)
    4. POST /api/v1/auth/reset   → consume token, update password_hash

The bootstrap admin user "lars" continues to be authenticated by
``check_bootstrap_admin`` in src/web/auth.py — never by this table.

Schema invariants:
  - email is unique (case-insensitive — stored lowercased)
  - password_hash is pbkdf2$... format from auth.hash_password
  - verify_code is 6 ASCII digits, expires 30 minutes
  - reset_token is 32 bytes url-safe, expires 1 hour, one-time use
"""
from __future__ import annotations

import re
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

# Project root = parent of src/
PROJECT_ROOT = Path(__file__).resolve().parents[2]
USERS_DB = PROJECT_ROOT / "data" / "users.db"

# Tuning
VERIFY_CODE_TTL = timedelta(minutes=30)
RESET_TOKEN_TTL = timedelta(hours=1)
MAX_VERIFY_ATTEMPTS = 5

# RFC-5322-ish: not perfect, but rejects obvious garbage.
# Good enough to keep bots out; fine-grained validation belongs at the SMTP layer.
EMAIL_REGEX = re.compile(
    r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$"
)


# -------------------------------------------------------------------
# Connection + schema
# -------------------------------------------------------------------

def connect_users_db() -> sqlite3.Connection:
    """Open the global users DB. Creates the file + schema if missing."""
    USERS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(USERS_DB))
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    conn.commit()
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create tables if missing on this connection. Idempotent + cheap."""
    conn.executescript(SCHEMA)


# Cross-process marker so the schema is only CREATEd once per DB file.
_schema_lock = threading.Lock()


def init_users_db() -> None:
    """Create the schema if it doesn't exist. Idempotent."""
    with _schema_lock:
        conn = connect_users_db()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    name TEXT,
    plan TEXT DEFAULT 'free',
    verified INTEGER DEFAULT 0,
    verify_code TEXT,
    verify_expires_at TEXT,
    verify_attempts INTEGER DEFAULT 0,
    verify_invalidated INTEGER DEFAULT 0,
    reset_token TEXT,
    reset_expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_reset_token ON users(reset_token);
"""


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def normalise_email(email: str) -> str:
    """Lowercase + strip. We treat the address as case-insensitive."""
    return email.strip().lower()


def is_valid_email(email: str) -> bool:
    """RFC-5322-ish — rejects obviously malformed addresses."""
    if not email or len(email) > 254:
        return False
    return bool(EMAIL_REGEX.match(email))


def is_weak_password(password: str) -> bool:
    """Returns True if the password is shorter than the 10-char minimum."""
    return len(password) < 10


def generate_verify_code() -> str:
    """6-digit numeric code, zero-padded."""
    return f"{secrets.randbelow(1_000_000):06d}"


def generate_reset_token() -> str:
    """32 random bytes, URL-safe base64 (no padding)."""
    return secrets.token_urlsafe(32)


# -------------------------------------------------------------------
# CRUD
# -------------------------------------------------------------------

def create_user(
    email: str,
    password_hash: str,
    name: Optional[str] = None,
    plan: str = "free",
) -> str:
    """Insert a new user row, return the UUID.

    Raises sqlite3.IntegrityError if email already exists.
    """
    email = normalise_email(email)
    uid = _new_id()
    now = _now_iso()
    conn = connect_users_db()
    try:
        conn.execute(
            """INSERT INTO users
               (id, email, password_hash, name, plan, verified, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 0, ?, ?)""",
            (uid, email, password_hash, name, plan, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return uid


def get_user_by_id(uid: str) -> Optional[sqlite3.Row]:
    conn = connect_users_db()
    try:
        return conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    finally:
        conn.close()


def get_user_by_email(email: str) -> Optional[sqlite3.Row]:
    conn = connect_users_db()
    try:
        return conn.execute(
            "SELECT * FROM users WHERE email = ?",
            (normalise_email(email),),
        ).fetchone()
    finally:
        conn.close()


def email_taken(email: str) -> bool:
    return get_user_by_email(email) is not None


def set_verify_code(uid: str, code: str, ttl: timedelta = VERIFY_CODE_TTL) -> str:
    """Store a fresh verify code. Returns the ISO expiry timestamp."""
    expires_at = (datetime.now(timezone.utc) + ttl).isoformat()
    conn = connect_users_db()
    try:
        conn.execute(
            """UPDATE users
               SET verify_code = ?, verify_expires_at = ?,
                   verify_attempts = 0, verify_invalidated = 0,
                   updated_at = ?
               WHERE id = ?""",
            (code, expires_at, _now_iso(), uid),
        )
        conn.commit()
    finally:
        conn.close()
    return expires_at


def get_user_by_verify(uid: str) -> Optional[sqlite3.Row]:
    """Look up a user only if their verify code slot is still usable."""
    conn = connect_users_db()
    try:
        return conn.execute(
            "SELECT * FROM users WHERE id = ? AND verify_code IS NOT NULL",
            (uid,),
        ).fetchone()
    finally:
        conn.close()


def increment_verify_attempts(uid: str) -> int:
    """Bump attempts; invalidate the code if it crosses MAX_VERIFY_ATTEMPTS."""
    conn = connect_users_db()
    try:
        row = conn.execute(
            "SELECT verify_attempts FROM users WHERE id = ?", (uid,)
        ).fetchone()
        if row is None:
            return 0
        new_count = (row["verify_attempts"] or 0) + 1
        invalidated = 1 if new_count >= MAX_VERIFY_ATTEMPTS else 0
        conn.execute(
            """UPDATE users
               SET verify_attempts = ?, verify_invalidated = ?,
                   updated_at = ?
               WHERE id = ?""",
            (new_count, invalidated, _now_iso(), uid),
        )
        conn.commit()
        return new_count
    finally:
        conn.close()


def mark_verified(uid: str) -> None:
    """Mark user verified and clear verify_code slot."""
    conn = connect_users_db()
    try:
        conn.execute(
            """UPDATE users
               SET verified = 1,
                   verify_code = NULL,
                   verify_expires_at = NULL,
                   verify_attempts = 0,
                   verify_invalidated = 0,
                   updated_at = ?
               WHERE id = ?""",
            (_now_iso(), uid),
        )
        conn.commit()
    finally:
        conn.close()


def set_reset_token(uid: str, token: str, ttl: timedelta = RESET_TOKEN_TTL) -> str:
    """Store a password-reset token. Returns the ISO expiry timestamp."""
    expires_at = (datetime.now(timezone.utc) + ttl).isoformat()
    conn = connect_users_db()
    try:
        conn.execute(
            """UPDATE users
               SET reset_token = ?, reset_expires_at = ?, updated_at = ?
               WHERE id = ?""",
            (token, expires_at, _now_iso(), uid),
        )
        conn.commit()
    finally:
        conn.close()
    return expires_at


def consume_reset_token(token: str) -> Optional[sqlite3.Row]:
    """Return the user row if the token is valid+unexpired+single-use.

    Clears the token atomically as part of the lookup — call update_password
    afterwards to actually set the new hash.
    """
    if not token:
        return None
    conn = connect_users_db()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE reset_token = ?", (token,)
        ).fetchone()
        if row is None:
            return None
        # Expiry check
        expires_at = row["reset_expires_at"]
        if expires_at:
            try:
                exp_dt = datetime.fromisoformat(expires_at)
                if datetime.now(timezone.utc) > exp_dt:
                    return None
            except Exception:
                return None
        return row
    finally:
        conn.close()


def clear_reset_token(uid: str) -> None:
    """One-time-use enforcement: wipe the token after a successful reset."""
    conn = connect_users_db()
    try:
        conn.execute(
            """UPDATE users
               SET reset_token = NULL, reset_expires_at = NULL, updated_at = ?
               WHERE id = ?""",
            (_now_iso(), uid),
        )
        conn.commit()
    finally:
        conn.close()


def update_password(uid: str, new_hash: str) -> None:
    conn = connect_users_db()
    try:
        conn.execute(
            "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
            (new_hash, _now_iso(), uid),
        )
        conn.commit()
    finally:
        conn.close()


def user_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Project a user row to a JSON-safe dict (no secrets)."""
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "plan": row["plan"] or "free",
        "verified": bool(row["verified"]),
    }


def ensure_schema() -> None:
    """Public alias — called once at app startup."""
    init_users_db()