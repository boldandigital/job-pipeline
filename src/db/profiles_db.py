"""Phase 2.7 — multi-profile storage layer.

A *profile* is a CV/headline/summary/experience variant the user can switch
between per job application. There is exactly one *default* profile per
user, surfaced in the legacy /api/profile and /app/profile surfaces for
backward compatibility.

Two tables live in the global users DB (data/users.db):

  profiles      — one row per profile, with name, kind, headline, summary,
                  experience, skills, links (all JSON-encoded lists).
                  `is_default=1` is the row the legacy /api/profile serves.
  profile_cvs   — one row per uploaded CV per profile. `is_default=1` flags
                  the CV that gets attached when this profile is selected.

The bootstrap admin user (``lars``) is allowed to own profiles too, and
gets a single "Default" profile auto-created on first access, seeded from
``config/lars-cv-data.json`` so the dashboard is never empty for him.

Edge cases (each enforced here so callers don't have to):

  - Deleting the only profile → ValueError("create another profile first")
  - Deleting the default profile → another row becomes default
    (newest by created_at), so the exactly-one-default invariant holds
  - Setting a CV default → previous default for that profile is cleared
  - ensure_default_profile_for_user is idempotent (a no-op when rows exist)
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("db.profiles_db")

# Re-use the global users DB so the auth + profile + profiles tables
# live in one file (data/users.db) and one connection. We import the
# helper lazily to avoid a circular import on app startup.
from . import users_db as _users_db

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ADMIN_USER_ID = _users_db.ADMIN_USER_ID

# ---- Validation constants ----------------------------------------------
NAME_MAX = 80
HEADLINE_MAX = 200
SUMMARY_MAX = 2000
LABEL_MAX = 120

#: Content types we can infer from an allowed extension. Used only when the
#: upload does not declare one itself.
_MIME_BY_EXT = {
    "pdf": "application/pdf",
    "docx": ("application/vnd.openxmlformats-officedocument"
             ".wordprocessingml.document"),
    "doc": "application/msword",
}

#: Profile kinds. ``default`` is the special "owns all legacy fields" row
#: that is auto-created for the admin; ``role`` is the typical application-
#: specific variant (e.g. "Head of Hosting" or "BD Lead DACH").
KINDS = ("default", "role")
#: Link kinds are stored lowercase so the multi-profile surface is wire-
#: compatible with the legacy single-profile /api/profile payload (which has
#: always lowercased these via ``users_db.normalise_links``). The UI renders
#: the label from a separate catalog; the DB row keeps the canonical key.
ALLOWED_LINK_KINDS = ("linkedin", "github", "portfolio", "other")

#: CV upload — kept aligned with the legacy single-profile path
#: (src/db/users_db.CV_ALLOWED_EXTS / CV_MAX_BYTES) so behaviour is
#: identical for both endpoints.
CV_ALLOWED_EXTS = ("pdf", "docx", "doc")
CV_MAX_BYTES = 5 * 1024 * 1024

#: Disk location for per-profile CVs:
#: data/users/<user_id>/profiles/<profile_id>/cvs/<cv_id>.<ext>
#: (anchored under data/ alongside jobs.db / users.db so a backup that
#: ships data/ brings the CVs too).
PROFILE_CV_ROOT = PROJECT_ROOT / "data" / "users"


# ---------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'role',
    headline TEXT,
    summary TEXT,
    experience_json TEXT,
    skills_json TEXT,
    links_json TEXT,
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_profiles_user ON profiles(user_id);
CREATE INDEX IF NOT EXISTS idx_profiles_default ON profiles(user_id, is_default);

CREATE TABLE IF NOT EXISTS profile_cvs (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    label TEXT,
    filename TEXT NOT NULL,
    file_path TEXT NOT NULL,
    mime_type TEXT,
    size_bytes INTEGER NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0,
    uploaded_at TEXT NOT NULL,
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_profile_cvs_profile ON profile_cvs(profile_id);
CREATE INDEX IF NOT EXISTS idx_profile_cvs_default ON profile_cvs(profile_id, is_default);
"""

#: Gated ALTERs for profile_cvs rows created before ``label`` /
#: ``mime_type`` existed. CREATE TABLE IF NOT EXISTS never adds columns to
#: an existing table, so an already-migrated DB needs these. Each ALTER is
#: applied only when the column is genuinely absent, making this idempotent
#: and safe to run on every connection.
_PROFILE_CV_MIGRATIONS = [
    ("label", "ALTER TABLE profile_cvs ADD COLUMN label TEXT"),
    ("mime_type", "ALTER TABLE profile_cvs ADD COLUMN mime_type TEXT"),
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------
# Connection + migrations
# ---------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    """Open the global users DB with the profiles schema applied.

    We piggy-back on users_db.connect_users_db() so the file + users
    schema is guaranteed to exist first; then we apply the profiles
    schema (CREATE TABLE IF NOT EXISTS — idempotent) followed by the
    gated column migrations.
    """
    conn = _users_db.connect_users_db()
    conn.executescript(SCHEMA)
    _apply_profile_cv_migrations(conn)
    conn.commit()
    return conn


def _apply_profile_cv_migrations(conn: sqlite3.Connection) -> None:
    """Add profile_cvs columns that predate this revision, if missing."""
    try:
        cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(profile_cvs)").fetchall()
        }
    except sqlite3.DatabaseError as exc:
        log.warning("could not inspect profile_cvs columns: %s", exc)
        return
    for column, ddl in _PROFILE_CV_MIGRATIONS:
        if column not in cols:
            try:
                conn.execute(ddl)
            except sqlite3.DatabaseError as exc:
                # A concurrent writer may have added it between the PRAGMA
                # and here; "duplicate column" is success, anything else is
                # worth knowing about.
                if "duplicate column" not in str(exc).lower():
                    log.warning("profile_cvs migration %s failed: %s",
                                column, exc)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _decode_list(raw: Any) -> list[Any]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _clamp(text: Optional[str], limit: int) -> str:
    if text is None:
        return ""
    s = str(text).strip()
    return s[:limit]


def profile_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Project a profiles row to a JSON-safe dict for the API."""
    keys = row.keys()
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "name": row["name"] or "",
        "kind": (row["kind"] or "role"),
        "headline": (row["headline"] if "headline" in keys else "") or "",
        "summary": (row["summary"] if "summary" in keys else "") or "",
        "experience": _decode_list(
            row["experience_json"] if "experience_json" in keys else None
        ),
        "skills": _decode_list(
            row["skills_json"] if "skills_json" in keys else None
        ),
        "links": _decode_list(
            row["links_json"] if "links_json" in keys else None
        ),
        "is_default": int(row["is_default"] or 0) == 1,
        "created_at": row["created_at"] or "",
        "updated_at": row["updated_at"] or "",
    }


def cv_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    keys = row.keys()
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "profile_id": row["profile_id"],
        # A CV uploaded before the label column existed has no label; fall
        # back to the filename so the UI never renders an empty row.
        "label": (row["label"] if "label" in keys else None)
        or row["filename"] or "",
        "filename": row["filename"] or "",
        "file_path": row["file_path"] or "",
        "mime_type": (row["mime_type"] if "mime_type" in keys else None) or "",
        "size_bytes": int(row["size_bytes"] or 0),
        "is_default": int(row["is_default"] or 0) == 1,
        "uploaded_at": row["uploaded_at"] or "",
    }


# ---------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------

def normalise_kind(kind: Any) -> str:
    """Return a valid kind, defaulting to ``role`` on anything unknown."""
    if not kind:
        return "role"
    s = str(kind).strip().lower()
    return s if s in KINDS else "role"


def normalise_links(links: Any) -> list[dict[str, str]]:
    """Coerce the links repeater into [{kind, url}]. Drops invalid rows."""
    out: list[dict[str, str]] = []
    if not isinstance(links, list):
        return out
    for item in links:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "Other").strip()
        # Case-insensitive match against the allowed set, fall back to Other
        if kind not in ALLOWED_LINK_KINDS:
            kind_lower = kind.lower()
            kind = next(
                (k for k in ALLOWED_LINK_KINDS if k.lower() == kind_lower),
                "Other",
            )
        url = str(item.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        out.append({"kind": kind, "url": url})
    return out


def normalise_experience(items: Any) -> list[dict[str, Any]]:
    """Coerce the experience repeater into [{company, role, start, end, bullets}]."""
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


def validate_name(name: Any) -> str:
    """Reject empty / oversize names. Returns the cleaned string."""
    if name is None:
        raise ValueError("name is required")
    s = str(name).strip()
    if not s:
        raise ValueError("name is required")
    if len(s) > NAME_MAX:
        raise ValueError(f"name must be {NAME_MAX} characters or fewer")
    return s


# ---------------------------------------------------------------------
# CRUD — profiles
# ---------------------------------------------------------------------

def list_profiles(user_id: str) -> list[dict[str, Any]]:
    """All profiles for a user, default first, then newest first."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM profiles WHERE user_id = ? "
            "ORDER BY is_default DESC, created_at ASC",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    return [profile_to_dict(r) for r in rows]


def get_profile(user_id: str, profile_id: str) -> Optional[dict[str, Any]]:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM profiles WHERE user_id = ? AND id = ?",
            (user_id, profile_id),
        ).fetchone()
    finally:
        conn.close()
    return profile_to_dict(row) if row else None


def get_default_profile(user_id: str) -> Optional[dict[str, Any]]:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM profiles WHERE user_id = ? AND is_default = 1",
            (user_id,),
        ).fetchone()
        if row is None:
            # No row is flagged default — fall back to the oldest one so
            # legacy /api/profile never returns 404 mid-migration. This
            # query must stay INSIDE the try: the connection is closed
            # by the finally block.
            row = conn.execute(
                "SELECT * FROM profiles WHERE user_id = ? "
                "ORDER BY created_at ASC LIMIT 1",
                (user_id,),
            ).fetchone()
    finally:
        conn.close()
    return profile_to_dict(row) if row else None


def _promote_default(conn: sqlite3.Connection, user_id: str,
                     target_id: str) -> None:
    """Set exactly one default. Used by create + set_default_profile.

    The demote is scoped to ``AND is_default = 1`` on purpose: a blanket
    ``WHERE user_id = ?`` would rewrite ``updated_at`` on every profile
    the user owns, including rows they never touched, which makes
    updated_at useless as a change signal.
    """
    conn.execute(
        "UPDATE profiles SET is_default = 0, updated_at = ? "
        "WHERE user_id = ? AND is_default = 1",
        (_now_iso(), user_id),
    )
    conn.execute(
        "UPDATE profiles SET is_default = 1, updated_at = ? "
        "WHERE user_id = ? AND id = ?",
        (_now_iso(), user_id, target_id),
    )


def create_profile(
    user_id: str,
    name: str,
    kind: str = "role",
    headline: str = "",
    summary: str = "",
    experience: Optional[list[dict[str, Any]]] = None,
    skills: Optional[list[str]] = None,
    links: Optional[list[dict[str, str]]] = None,
    make_default: bool = False,
) -> dict[str, Any]:
    """Insert a new profile. Returns the persisted row as a dict."""
    name = validate_name(name)
    kind = normalise_kind(kind)
    pid = _new_id()
    now = _now_iso()
    conn = _conn()
    try:
        existing = conn.execute(
            "SELECT COUNT(*) AS c FROM profiles WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        # First profile for a user is automatically default unless caller
        # explicitly asks not to (typical only in tests).
        is_default = 1 if (existing["c"] == 0 or make_default) else 0

        if is_default:
            _promote_default(conn, user_id, pid)
        # NB: when is_default=0, do NOT demote an existing default. A non-
        # default insert must leave the current default row untouched; only
        # make_default=True or the very first row should change the default.

        conn.execute(
            """INSERT INTO profiles
               (id, user_id, name, kind, headline, summary,
                experience_json, skills_json, links_json,
                is_default, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pid, user_id, name, kind,
                _clamp(headline, HEADLINE_MAX),
                _clamp(summary, SUMMARY_MAX),
                json.dumps(normalise_experience(experience), ensure_ascii=False),
                json.dumps(normalise_skills(skills), ensure_ascii=False),
                json.dumps(normalise_links(links), ensure_ascii=False),
                is_default, now, now,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM profiles WHERE id = ?", (pid,)
        ).fetchone()
    finally:
        conn.close()
    return profile_to_dict(row)


def update_profile(
    user_id: str,
    profile_id: str,
    fields: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Partial update. Returns the new row, or None if not found."""
    if not fields:
        return get_profile(user_id, profile_id)

    assignments: list[str] = []
    values: list[Any] = []

    if "name" in fields:
        assignments.append("name = ?")
        values.append(validate_name(fields["name"]))
    if "kind" in fields:
        assignments.append("kind = ?")
        values.append(normalise_kind(fields["kind"]))
    if "headline" in fields:
        assignments.append("headline = ?")
        values.append(_clamp(fields.get("headline"), HEADLINE_MAX))
    if "summary" in fields:
        assignments.append("summary = ?")
        values.append(_clamp(fields.get("summary"), SUMMARY_MAX))
    if "experience" in fields:
        assignments.append("experience_json = ?")
        values.append(json.dumps(
            normalise_experience(fields.get("experience")), ensure_ascii=False
        ))
    if "skills" in fields:
        assignments.append("skills_json = ?")
        values.append(json.dumps(
            normalise_skills(fields.get("skills")), ensure_ascii=False
        ))
    if "links" in fields:
        assignments.append("links_json = ?")
        values.append(json.dumps(
            normalise_links(fields.get("links")), ensure_ascii=False
        ))

    if not assignments:
        return get_profile(user_id, profile_id)

    assignments.append("updated_at = ?")
    values.append(_now_iso())
    values.extend([user_id, profile_id])

    conn = _conn()
    try:
        cur = conn.execute(
            f"UPDATE profiles SET {', '.join(assignments)} "
            f"WHERE user_id = ? AND id = ?",
            values,
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT * FROM profiles WHERE user_id = ? AND id = ?",
            (user_id, profile_id),
        ).fetchone()
    finally:
        conn.close()
    return profile_to_dict(row)


def delete_profile(user_id: str, profile_id: str) -> bool:
    """Delete a profile and its CV files. Refuses to delete the only one.

    Returns True on success. Raises ValueError when the user has exactly
    one profile left (so callers can turn that into a 400 with a useful
    message).
    """
    conn = _conn()
    try:
        # Existence check first — returning 404 for an unknown id must come
        # BEFORE the "only profile left" guard, otherwise an unknown id would
        # look like a 400 "create another profile first".
        row = conn.execute(
            "SELECT * FROM profiles WHERE user_id = ? AND id = ?",
            (user_id, profile_id),
        ).fetchone()
        if row is None:
            return False

        count_row = conn.execute(
            "SELECT COUNT(*) AS c FROM profiles WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if count_row["c"] <= 1:
            raise ValueError("create another profile first")

        was_default = int(row["is_default"] or 0) == 1

        # Detach CV files from this profile so we can delete them safely
        cv_rows = conn.execute(
            "SELECT * FROM profile_cvs WHERE user_id = ? AND profile_id = ?",
            (user_id, profile_id),
        ).fetchall()
        conn.execute(
            "DELETE FROM profile_cvs WHERE user_id = ? AND profile_id = ?",
            (user_id, profile_id),
        )
        conn.execute(
            "DELETE FROM profiles WHERE user_id = ? AND id = ?",
            (user_id, profile_id),
        )

        if was_default:
            # Promote the next-oldest remaining row to default so the
            # exactly-one-default invariant holds.
            nxt = conn.execute(
                "SELECT id FROM profiles WHERE user_id = ? "
                "ORDER BY created_at ASC LIMIT 1",
                (user_id,),
            ).fetchone()
            if nxt:
                _promote_default(conn, user_id, nxt["id"])
        conn.commit()
    finally:
        conn.close()

    # Best-effort CV file removal (outside the DB txn).
    for cv in cv_rows:
        try:
            abs_path = (PROFILE_CV_ROOT / cv["file_path"]).resolve()
            # Defensive: never delete anything outside the CV root.
            if str(abs_path).startswith(str(PROFILE_CV_ROOT.resolve())):
                abs_path.unlink(missing_ok=True)
        except Exception as exc:
            log.warning("could not remove CV file %s: %s", cv["file_path"], exc)
    return True


def set_default_profile(user_id: str, profile_id: str) -> Optional[dict[str, Any]]:
    """Make ``profile_id`` the default. Idempotent."""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM profiles WHERE user_id = ? AND id = ?",
            (user_id, profile_id),
        ).fetchone()
        if row is None:
            return None
        _promote_default(conn, user_id, profile_id)
        conn.commit()
        row = conn.execute(
            "SELECT * FROM profiles WHERE user_id = ? AND id = ?",
            (user_id, profile_id),
        ).fetchone()
    finally:
        conn.close()
    return profile_to_dict(row)


# ---------------------------------------------------------------------
# Default profile migration (idempotent)
# ---------------------------------------------------------------------

def ensure_default_profile_for_user(
    user_id: str,
    fallback_data: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Make sure the user has at least one profile.

    - Returns the existing default row if one exists.
    - Otherwise creates a "Default" profile and seeds it from
      ``fallback_data`` (typically ``config/lars-cv-data.json`` for
      the bootstrap admin, or an empty dict for new signups).
    - Idempotent: calling it twice is a no-op on the second call.
    """
    existing = get_default_profile(user_id)
    if existing is not None:
        return existing

    data = fallback_data or {}
    return create_profile(
        user_id=user_id,
        name=data.get("name") or "Default",
        kind="default",
        headline=data.get("headline") or "",
        summary=data.get("summary") or "",
        experience=data.get("experience") or [],
        skills=data.get("skills") or [],
        links=data.get("links") or [],
        make_default=True,
    )


# ---------------------------------------------------------------------
# CV CRUD
# ---------------------------------------------------------------------

def list_cvs(user_id: str, profile_id: str) -> list[dict[str, Any]]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM profile_cvs WHERE user_id = ? AND profile_id = ? "
            "ORDER BY is_default DESC, uploaded_at ASC",
            (user_id, profile_id),
        ).fetchall()
    finally:
        conn.close()
    return [cv_to_dict(r) for r in rows]


def get_cv(user_id: str, cv_id: str) -> Optional[dict[str, Any]]:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM profile_cvs WHERE user_id = ? AND id = ?",
            (user_id, cv_id),
        ).fetchone()
    finally:
        conn.close()
    return cv_to_dict(row) if row else None


def get_default_cv(user_id: str, profile_id: str) -> Optional[dict[str, Any]]:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM profile_cvs WHERE user_id = ? AND profile_id = ? "
            "AND is_default = 1",
            (user_id, profile_id),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT * FROM profile_cvs WHERE user_id = ? AND profile_id = ? "
                "ORDER BY uploaded_at ASC LIMIT 1",
                (user_id, profile_id),
            ).fetchone()
    finally:
        conn.close()
    return cv_to_dict(row) if row else None


def cv_disk_dir(user_id: str, profile_id: str) -> Path:
    """Return (and create) the directory for a profile's CV files."""
    d = PROFILE_CV_ROOT / user_id / "profiles" / profile_id / "cvs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def add_cv(
    user_id: str,
    profile_id: str,
    filename: str,
    file_bytes: bytes,
    label: str = "",
    mime_type: str = "",
) -> dict[str, Any]:
    """Persist a CV upload. The first CV for a profile becomes default.

    ``label`` is the user-facing name for this base CV ("Base CTO",
    "EU Format"); it falls back to the filename when omitted.
    ``mime_type`` is stored verbatim when supplied and inferred from the
    extension otherwise.

    Caller is responsible for ext/size validation against
    ``CV_ALLOWED_EXTS`` / ``CV_MAX_BYTES`` — this helper trusts them but
    also re-validates defensively so a misuse here still errors out.
    """
    if not filename:
        raise ValueError("filename is required")
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in CV_ALLOWED_EXTS:
        raise ValueError(f"unsupported file type '{ext}'")
    if not file_bytes:
        raise ValueError("uploaded file is empty")
    if len(file_bytes) > CV_MAX_BYTES:
        raise ValueError(
            f"file is larger than the {CV_MAX_BYTES // (1024*1024)} MB limit"
        )

    label = str(label or "").strip() or filename
    if len(label) > LABEL_MAX:
        label = label[:LABEL_MAX]
    mime_type = str(mime_type or "").strip() or _MIME_BY_EXT.get(ext, "")

    cv_id = _new_id()
    disk_dir = cv_disk_dir(user_id, profile_id)
    target = disk_dir / f"{cv_id}.{ext}"
    target.write_bytes(file_bytes)
    # Store the path relative to PROFILE_CV_ROOT so a redirected CV root
    # (e.g. in tests pointing at a tmp dir) does not break lookups.
    rel_path = str(target.relative_to(PROFILE_CV_ROOT))

    conn = _conn()
    try:
        # Was there already a default? If not, this new one is default.
        existing_default = conn.execute(
            "SELECT COUNT(*) AS c FROM profile_cvs "
            "WHERE user_id = ? AND profile_id = ? AND is_default = 1",
            (user_id, profile_id),
        ).fetchone()
        is_default = 1 if existing_default["c"] == 0 else 0
        # If we are the default, demote any previous default first.
        if is_default:
            conn.execute(
                "UPDATE profile_cvs SET is_default = 0 "
                "WHERE user_id = ? AND profile_id = ?",
                (user_id, profile_id),
            )
        conn.execute(
            """INSERT INTO profile_cvs
               (id, user_id, profile_id, label, filename, file_path,
                mime_type, size_bytes, is_default, uploaded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                cv_id, user_id, profile_id, label, filename, rel_path,
                mime_type, len(file_bytes), is_default, _now_iso(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return get_cv(user_id, cv_id) or {}


def set_default_cv(
    user_id: str, profile_id: str, cv_id: str
) -> Optional[dict[str, Any]]:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM profile_cvs "
            "WHERE user_id = ? AND profile_id = ? AND id = ?",
            (user_id, profile_id, cv_id),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE profile_cvs SET is_default = 0 "
            "WHERE user_id = ? AND profile_id = ?",
            (user_id, profile_id),
        )
        conn.execute(
            "UPDATE profile_cvs SET is_default = 1 "
            "WHERE user_id = ? AND profile_id = ? AND id = ?",
            (user_id, profile_id, cv_id),
        )
        conn.commit()
    finally:
        conn.close()
    return get_cv(user_id, cv_id)


def delete_cv(user_id: str, cv_id: str) -> bool:
    """Delete a CV row + best-effort file removal.

    If the deleted CV was the default, the oldest remaining CV (by
    uploaded_at) becomes the new default — same invariant as for profiles.
    """
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM profile_cvs WHERE user_id = ? AND id = ?",
            (user_id, cv_id),
        ).fetchone()
        if row is None:
            return False
        was_default = int(row["is_default"] or 0) == 1
        profile_id = row["profile_id"]
        file_path = row["file_path"]

        conn.execute(
            "DELETE FROM profile_cvs WHERE user_id = ? AND id = ?",
            (user_id, cv_id),
        )
        if was_default:
            nxt = conn.execute(
                "SELECT id FROM profile_cvs "
                "WHERE user_id = ? AND profile_id = ? "
                "ORDER BY uploaded_at ASC LIMIT 1",
                (user_id, profile_id),
            ).fetchone()
            if nxt:
                conn.execute(
                    "UPDATE profile_cvs SET is_default = 1 "
                    "WHERE user_id = ? AND profile_id = ? AND id = ?",
                    (user_id, profile_id, nxt["id"]),
                )
        conn.commit()
    finally:
        conn.close()

    try:
        # Mirror add_cv() — resolve from PROFILE_CV_ROOT so a redirected
        # CV root (tests) still finds the right file.
        abs_path = (PROFILE_CV_ROOT / file_path).resolve()
        if str(abs_path).startswith(str(PROFILE_CV_ROOT.resolve())):
            abs_path.unlink(missing_ok=True)
    except Exception as exc:
        log.warning("could not remove CV file %s: %s", file_path, exc)
    return True


# ---------------------------------------------------------------------
# Admin seeding from config/lars-cv-data.json (or the markdown profile)
# ---------------------------------------------------------------------

def _profile_from_lars_legacy_json() -> dict[str, Any]:
    """Build a profile dict from config/lars-cv-data.json (admin legacy)."""
    path = PROJECT_ROOT / "config" / "lars-cv-data.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    personal = raw.get("personal", {}) or {}
    summary = (
        raw.get("summary_en")
        or raw.get("summary_de")
        or personal.get("title_en", "")
    )
    headline = personal.get("title_en", "") or personal.get("title_de", "")

    # Convert lars-cv-data.json's experience shape (period / role_en / bullets_en)
    # into the schema expected by the profiles table (company / role / bullets).
    experience: list[dict[str, Any]] = []
    for entry in (raw.get("experience") or []):
        experience.append({
            "company": entry.get("company", ""),
            "role": entry.get("role_en") or entry.get("role_de") or "",
            "start": (entry.get("period") or "").split("→", 1)[0].strip(),
            "end": (
                (entry.get("period") or "").split("→", 1)[1].strip()
                if "→" in (entry.get("period") or "") else ""
            ),
            "bullets": (
                entry.get("bullets_en")
                or entry.get("bullets_de")
                or []
            ),
        })

    skills: list[str] = []
    s = raw.get("skills")
    if isinstance(s, dict):
        skills = s.get("en") or s.get("de") or []
    elif isinstance(s, list):
        skills = s

    # Convert flat linkedin / github / websites into the repeater shape.
    links: list[dict[str, str]] = []
    if personal.get("linkedin"):
        links.append({"kind": "LinkedIn", "url": personal["linkedin"]})
    if personal.get("github"):
        links.append({"kind": "GitHub", "url": personal["github"]})
    for site in personal.get("websites") or []:
        links.append({"kind": "Portfolio", "url": site})

    return {
        "name": personal.get("name") or "Lars Zimmermann",
        "headline": headline,
        "summary": summary,
        "experience": experience,
        "skills": skills,
        "links": links,
    }


def ensure_admin_default() -> dict[str, Any]:
    """Idempotently ensure the bootstrap admin has a Default profile."""
    fallback = _profile_from_lars_legacy_json()
    return ensure_default_profile_for_user(ADMIN_USER_ID, fallback)
