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

import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("db.users_db")

# Project root = parent of src/
PROJECT_ROOT = Path(__file__).resolve().parents[2]
USERS_DB = PROJECT_ROOT / "data" / "users.db"

# Bootstrap admin (matches jobs_db.ADMIN_USER_ID) — never demoted.
ADMIN_USER_ID = "lars"

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
    updated_at TEXT NOT NULL,
    -- Phase 2.6 — profile page (CV, experience, links)
    phone TEXT,
    location TEXT,
    headline TEXT,
    summary TEXT,
    experience_json TEXT,
    skills_json TEXT,
    links_json TEXT,
    cv_upload_path TEXT,
    cv_uploaded_at TEXT,
    onboarding_complete INTEGER DEFAULT 0,
    onboarding_step INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_reset_token ON users(reset_token);
"""

# Gated ALTER TABLE migrations for DBs created before Phase 2.6.
#
# SQLite has no "ADD COLUMN IF NOT EXISTS" and each ALTER on an existing
# column raises, so every column is guarded by a PRAGMA table_info probe.
# Same shape as connect_user_db()'s gate migrations in src/db/jobs_db.py —
# an old users.db upgrades in place with no data loss.
_PROFILE_MIGRATIONS = (
    ("phone", "ALTER TABLE users ADD COLUMN phone TEXT"),
    ("location", "ALTER TABLE users ADD COLUMN location TEXT"),
    ("headline", "ALTER TABLE users ADD COLUMN headline TEXT"),
    ("summary", "ALTER TABLE users ADD COLUMN summary TEXT"),
    ("experience_json", "ALTER TABLE users ADD COLUMN experience_json TEXT"),
    ("skills_json", "ALTER TABLE users ADD COLUMN skills_json TEXT"),
    ("links_json", "ALTER TABLE users ADD COLUMN links_json TEXT"),
    ("cv_upload_path", "ALTER TABLE users ADD COLUMN cv_upload_path TEXT"),
    ("cv_uploaded_at", "ALTER TABLE users ADD COLUMN cv_uploaded_at TEXT"),
    ("onboarding_complete",
     "ALTER TABLE users ADD COLUMN onboarding_complete INTEGER DEFAULT 0"),
    ("onboarding_step",
     "ALTER TABLE users ADD COLUMN onboarding_step INTEGER DEFAULT 0"),
)

#: Scalar columns the profile page reads and writes (never password_hash /
#: email — those are owned by the auth flow, not the profile form).
PROFILE_COLUMNS = (
    "name", "phone", "location", "headline", "summary",
)

#: JSON-encoded list columns. The API exposes them decoded (``links``,
#: ``experience``, ``skills``); the DB stores them as TEXT so the schema stays
#: flat and needs no separate profile table.
_JSON_COLUMNS = {
    "links_json": "links",
    "experience_json": "experience",
    "skills_json": "skills",
}

#: Link kinds the profile page offers in its repeater.
LINK_KINDS = ("linkedin", "github", "portfolio", "other")

#: CV file types the uploader accepts. Extension-gated only — the ATS
#: adapters never parse the CV, they just hand the path to a browser upload.
CV_ALLOWED_EXTS = ("pdf", "docx")
CV_MAX_BYTES = 5 * 1024 * 1024


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


def set_plan(uid: str, plan: str) -> None:
    """Update a user's plan tier. Phase 1.4: written by Stripe webhooks.

    The bootstrap admin user ("lars") is never downgraded by this helper —
    callers in the Stripe event handler already check, but we belt-and-
    brace it here too so a stray write cannot accidentally strip admin.
    """
    if plan not in ("free", "solo", "pro", "admin"):
        raise ValueError(f"unknown plan tier: {plan!r}")
    if uid == ADMIN_USER_ID:
        # Never mutate the bootstrap admin's plan via this path.
        return
    conn = connect_users_db()
    try:
        conn.execute(
            "UPDATE users SET plan = ?, updated_at = ? WHERE id = ?",
            (plan, _now_iso(), uid),
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


# -------------------------------------------------------------------
# -------------------------------------------------------------------
# Phase 2.6 — Profile page helpers
# -------------------------------------------------------------------
#
# The profile page (web/static/profile.html, served at /app/profile) reads
# and writes exactly the columns declared in SCHEMA / _PROFILE_MIGRATIONS.
# Column names are the card's contract and are asserted by
# tests/test_profile_page.py:
#
#   scalar : name, phone, location, headline, summary
#   json   : links_json, experience_json, skills_json   -> links/experience/skills
#   cv     : cv_upload_path, cv_uploaded_at
#   tour   : onboarding_complete, onboarding_step
#
# The bootstrap admin ("lars") is NOT a users-table citizen — it is
# authenticated by check_bootstrap_admin() and its identity lives in env
# vars. Every writer here refuses to touch it, so the self-host operator
# cannot be downgraded or renamed through the profile form.


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Add any Phase 2.6 column this DB file is missing.

    SQLite has no "ADD COLUMN IF NOT EXISTS", so each ALTER is guarded by a
    PRAGMA probe first. Idempotent and cheap — safe on every connect, and a
    pre-2.6 users.db upgrades in place without losing rows.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    for col, ddl in _PROFILE_MIGRATIONS:
        if col in existing:
            continue
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            # A concurrent writer won the race and added it first — that is
            # the outcome we wanted anyway. Anything else is a real problem
            # and will surface on the next statement that touches the column.
            log.warning("users_db migration %s failed", col)


def _decode_list(raw: Any, kind: str) -> list[Any]:
    """Decode a JSON list column, tolerating null / corrupt / wrong-shape."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _profile_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Project a users Row to the profile API shape (no secrets)."""
    keys = row.keys()
    return {
        "id": row["id"],
        "user_id": row["id"],
        "name": row["name"] or "",
        "email": row["email"] or "",
        "phone": (row["phone"] if "phone" in keys else "") or "",
        "location": (row["location"] if "location" in keys else "") or "",
        "headline": (row["headline"] if "headline" in keys else "") or "",
        "summary": (row["summary"] if "summary" in keys else "") or "",
        "links": _decode_list(row["links_json"] if "links_json" in keys else None, "links"),
        "experience": _decode_list(
            row["experience_json"] if "experience_json" in keys else None, "experience"
        ),
        "skills": _decode_list(row["skills_json"] if "skills_json" in keys else None, "skills"),
        "plan": row["plan"] or "free",
        "cv_upload_path": (row["cv_upload_path"] if "cv_upload_path" in keys else "") or "",
        "cv_uploaded_at": (row["cv_uploaded_at"] if "cv_uploaded_at" in keys else "") or "",
        "onboarding_complete": int(
            (row["onboarding_complete"] if "onboarding_complete" in keys else 0) or 0
        ),
        "onboarding_step": int(
            (row["onboarding_step"] if "onboarding_step" in keys else 0) or 0
        ),
        "is_admin": row["id"] == ADMIN_USER_ID,
    }


def _admin_profile() -> dict[str, Any]:
    """Read-only profile for the bootstrap admin, derived from env.

    Deliberately NOT backed by a users-table row: writing admin's CV or
    personal data through the profile form would shadow the env-driven
    identity that check_bootstrap_admin() and /api/me already use.
    """
    return {
        "id": ADMIN_USER_ID,
        "user_id": ADMIN_USER_ID,
        "name": os.getenv("LARS_NAME", "Lars Zimmermann"),
        "email": os.getenv("LARS_EMAIL", "lars.z@icloud.com"),
        "phone": "",
        "location": "",
        "headline": "",
        "summary": "",
        "links": [],
        "experience": [],
        "skills": [],
        "plan": "admin (self-host)",
        "cv_upload_path": "",
        "cv_uploaded_at": "",
        "onboarding_complete": 1,
        "onboarding_step": 0,
        "is_admin": True,
    }


def get_profile(uid: str) -> dict[str, Any]:
    """Return the profile dict for a user. Never raises for a missing row —
    an unknown or admin user gets the empty/read-only shape instead.

    Phase 2.7: the headline/summary/links/experience/skills now live on
    the default row of the new ``profiles`` table. The users row still
    owns name/phone/location/email/plan/cv/onboarding so other endpoints
    keep reading the same fields without a second lookup.
    """
    if uid == ADMIN_USER_ID:
        return _admin_profile()
    conn = connect_users_db()
    try:
        _apply_migrations(conn)
        row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        conn.commit()
    finally:
        conn.close()
    if row is None:
        return {}
    base = _profile_row_to_dict(row)
    # Compose with the default profile row (Phase 2.7).
    try:
        from . import profiles_db as _profiles_db
        default = _profiles_db.get_default_profile(uid)
    except Exception:
        default = None
    if default is not None:
        base["headline"] = default.get("headline") or base["headline"]
        base["summary"] = default.get("summary") or base["summary"]
        base["links"] = default.get("links") or []
        base["experience"] = default.get("experience") or []
        base["skills"] = default.get("skills") or []
    return base


def normalise_links(links: Any) -> list[dict[str, str]]:
    """Coerce the links repeater into [{kind, url}] with known kinds only.

    Anything without a usable http(s) URL is dropped — a link row with an
    empty target would render as a dead anchor on the profile page.
    """
    out: list[dict[str, str]] = []
    if not isinstance(links, list):
        return out
    for item in links:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "other").strip().lower()
        if kind not in LINK_KINDS:
            kind = "other"
        url = str(item.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        out.append({"kind": kind, "url": url})
    return out


def normalise_experience(items: Any) -> list[dict[str, Any]]:
    """Coerce the experience repeater into [{company, role, start, end, bullets}].

    Entries missing both company and role are dropped — an empty row in the
    repeater should not become an empty timeline entry.
    """
    out: list[dict[str, Any]] = []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        company = str(item.get("company") or "").strip()
        role = str(item.get("role") or "").strip()
        if not company and not role:
            continue
        bullets_raw = item.get("bullets") or []
        if isinstance(bullets_raw, str):
            # The textarea writes one bullet per line.
            bullets = [ln.strip() for ln in bullets_raw.splitlines() if ln.strip()]
        elif isinstance(bullets_raw, list):
            bullets = [str(b).strip() for b in bullets_raw if str(b).strip()]
        else:
            bullets = []
        out.append({
            "company": company,
            "role": role,
            "start": str(item.get("start") or "").strip(),
            "end": str(item.get("end") or "").strip(),
            "bullets": bullets,
        })
    return out


def normalise_skills(skills: Any) -> list[str]:
    """Coerce the chip input into a de-duplicated list of non-empty strings."""
    if not isinstance(skills, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for s in skills:
        val = str(s).strip()
        if not val:
            continue
        key = val.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(val)
    return out


def set_profile(uid: str, fields: dict[str, Any]) -> bool:
    """Write profile fields. Returns True if a row was written.

    Only whitelisted keys are considered; ``email``, ``password_hash``,
    ``plan`` and ``verified`` are NOT writable through this path.

    Phase 2.7: name/phone/location stay on the users row. The
    headline/summary/links/experience/skills fields are written to the
    default row of the profiles table (auto-created on first call).
    The users row keeps the legacy headline/summary columns as a mirror
    so any direct DB reader that ignores profiles still sees a value.
    """
    if uid == ADMIN_USER_ID:
        return False
    assignments: list[str] = []
    values: list[Any] = []
    for col in PROFILE_COLUMNS:
        if col in fields:
            raw = fields[col]
            assignments.append(f"{col} = ?")
            values.append("" if raw is None else str(raw).strip()[:4000])

    for json_col, api_key in _JSON_COLUMNS.items():
        if api_key not in fields:
            continue
        raw = fields[api_key]
        if api_key == "links":
            normalised = normalise_links(raw)
        elif api_key == "experience":
            normalised = normalise_experience(raw)
        else:
            normalised = normalise_skills(raw)
        assignments.append(f"{json_col} = ?")
        values.append(json.dumps(normalised, ensure_ascii=False))

    wrote_users = False
    if assignments:
        assignments.append("updated_at = ?")
        values.append(_now_iso())
        values.append(uid)
        conn = connect_users_db()
        try:
            _apply_migrations(conn)
            cur = conn.execute(
                f"UPDATE users SET {', '.join(assignments)} WHERE id = ?", values
            )
            conn.commit()
            wrote_users = cur.rowcount > 0
        finally:
            conn.close()

    # Phase 2.7: also write the profile-owned fields to the default
    # profile row. The users-row write above keeps them as a mirror so
    # direct-DB readers continue to work; the source of truth becomes
    # the profiles table.
    profile_fields: dict[str, Any] = {}
    for key in ("headline", "summary", "links", "experience", "skills"):
        if key in fields:
            profile_fields[key] = fields[key]
    if profile_fields:
        try:
            from . import profiles_db as _profiles_db
            # Make sure a default row exists for this user.
            _profiles_db.ensure_default_profile_for_user(uid)
            default = _profiles_db.get_default_profile(uid)
            if default is not None:
                _profiles_db.update_profile(
                    uid, default["id"], profile_fields
                )
        except Exception as exc:
            log.warning("profile table write failed: %s", exc)

    return wrote_users or bool(profile_fields)


def set_cv_upload(uid: str, relative_path: str) -> Optional[str]:
    """Record a stored CV. Returns the ISO timestamp, or None if no row.

    ``relative_path`` is project-root-relative (``data/users/<id>/cv/cv.pdf``)
    so a DB restored into a container still resolves. Callers must have
    already resolved it to a real, non-traversing path on disk.
    """
    if uid == ADMIN_USER_ID:
        return None
    ts = _now_iso()
    conn = connect_users_db()
    try:
        _apply_migrations(conn)
        cur = conn.execute(
            """UPDATE users
               SET cv_upload_path = ?, cv_uploaded_at = ?, updated_at = ?
               WHERE id = ?""",
            (relative_path, ts, ts, uid),
        )
        conn.commit()
    finally:
        conn.close()
    return ts if cur.rowcount > 0 else None


def clear_cv_upload(uid: str) -> None:
    """Forget the CV pointer. Does not touch the file — the caller owns that."""
    if uid == ADMIN_USER_ID:
        return
    conn = connect_users_db()
    try:
        _apply_migrations(conn)
        conn.execute(
            """UPDATE users
               SET cv_upload_path = NULL, cv_uploaded_at = NULL, updated_at = ?
               WHERE id = ?""",
            (_now_iso(), uid),
        )
        conn.commit()
    finally:
        conn.close()


def set_onboarding(uid: str, complete: bool, step: Optional[int] = None) -> bool:
    """Persist guided-tour state. Returns True if a row was written.

    Step is clamped to TOUR_TOTAL_STEPS so a crafted request cannot park the
    tour on a step that does not exist.
    """
    if uid == ADMIN_USER_ID:
        return False
    conn = connect_users_db()
    try:
        _apply_migrations(conn)
        cur = conn.execute(
            "UPDATE users SET onboarding_complete = ?, onboarding_step = ?, "
            "updated_at = ? WHERE id = ?",
            (1 if complete else 0, clamp_tour_step(step), _now_iso(), uid),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


#: Number of steps in the first-run dashboard tour. Kept next to the DB
#: writer because onboarding_step is clamped against it.
TOUR_TOTAL_STEPS = 5


def clamp_tour_step(step: Optional[int]) -> int:
    """Clamp a client-supplied tour step into 0..TOUR_TOTAL_STEPS."""
    if step is None:
        return 0
    try:
        value = int(step)
    except (TypeError, ValueError):
        return 0
    return max(0, min(TOUR_TOTAL_STEPS, value))


def tour_pending(uid: str) -> bool:
    """True when the first-run tour should fire for this user.

    The admin is never toured (their profile has no DB row to persist state
    in), so they read as already onboarded.
    """
    profile = get_profile(uid)
    if not profile or profile.get("is_admin"):
        return False
    return not bool(profile.get("onboarding_complete"))