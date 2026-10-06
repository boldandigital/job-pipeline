"""Per-user database + profile factory.

Single source of truth for "where is this user's data".

Before Phase 1: every script hardcoded `data/jobs.db` and `config/lars-cv-data.json`.
After Phase 1: every script calls `get_user_db(user_id)`, `get_user_profile(user_id)`,
`get_user_batches_dir(user_id)`.

For the bootstrap admin user (Lars, user_id="lars"), we keep the legacy paths
(data/jobs.db, config/lars-cv-data.json) so Ye don't have to migrate Ye.r
existing 43 jobs, 10 batch packs, and 3 months of CV data. New users get
isolated `data/users/<id>/jobs.db` and `data/users/<id>/profile.json`.

This is a hard requirement for the SaaS (Phase 4) but it's also the foundation
for any future per-user feature (different CV per user, different scoring config).
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

# Project root = parent of src/
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Legacy single-user paths (kept as-is so Ye don't lose data)
LEGACY_DB = PROJECT_ROOT / "data" / "jobs.db"
LEGACY_PROFILE = PROJECT_ROOT / "config" / "lars-cv-data.json"
LEGACY_BATCHES = PROJECT_ROOT / "data" / "batches"

# Per-user paths
USERS_DIR = PROJECT_ROOT / "data" / "users"
USER_TEMPLATE_DIR = USERS_DIR / "_template"

ADMIN_USER_ID = "lars"


def get_user_id(request=None, env_var: str = "LARS_USER_ID") -> str:
    """Return the user_id for the current request.

    MVP: read from LARS_USER_ID env var (set by the launcher).
    Phase 4: read from session cookie (after auth lands).
    """
    # 1. env var (for CLI scripts + cron)
    eid = os.getenv(env_var)
    if eid:
        return eid
    # 2. request state (after auth lands)
    if request is not None and hasattr(request, "state") and hasattr(request.state, "user_id"):
        return request.state.user_id
    # 3. default to admin
    return ADMIN_USER_ID


def get_user_db_path(user_id: str = ADMIN_USER_ID) -> Path:
    """Per-user SQLite path. Falls back to legacy for admin user."""
    if user_id == ADMIN_USER_ID:
        return LEGACY_DB
    p = USERS_DIR / user_id / "jobs.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def get_user_batches_dir(user_id: str = ADMIN_USER_ID) -> Path:
    """Per-user batches directory."""
    if user_id == ADMIN_USER_ID:
        return LEGACY_BATCHES
    p = USERS_DIR / user_id / "batches"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_user_profile_path(user_id: str = ADMIN_USER_ID) -> Path:
    """Per-user profile (CV data) path.

    For the admin user, returns the legacy file (do not migrate).
    For new users, returns their isolated profile.json.
    """
    if user_id == ADMIN_USER_ID:
        return LEGACY_PROFILE
    p = USERS_DIR / user_id / "profile.json"
    if not p.exists():
        # Copy from template
        p.parent.mkdir(parents=True, exist_ok=True)
        if USER_TEMPLATE_DIR.exists():
            template = USER_TEMPLATE_DIR / "profile.json"
            if template.exists():
                p.write_text(template.read_text(encoding="utf-8"))
    return p


def get_user_profile(user_id: str = ADMIN_USER_ID) -> dict[str, Any]:
    """Read the user profile JSON. Returns {} on parse error."""
    path = get_user_profile_path(user_id)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


# Schema applied to new users' jobs DB on first connect.
# Mirrors the admin user's data/jobs.db schema so the dashboard works out of the box.
#
# Phase 1.7 — 4-gate progressive approval columns:
#   cv_ok, Anschreiben_ok, motivation_ok — booleans, default 0 (false)
#   approved_at — ISO timestamp, set when the final Approve & Submit fires
_USER_JOBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    company TEXT,
    location TEXT,
    url TEXT,
    career_url TEXT,
    source TEXT,
    description TEXT,
    score INTEGER DEFAULT 0,
    status TEXT DEFAULT 'new',
    cv_path TEXT,
    cover_letter_path TEXT,
    cv_ok INTEGER DEFAULT 0,
    anschreiben_ok INTEGER DEFAULT 0,
    motivation_ok INTEGER DEFAULT 0,
    approved_at TEXT,
    created_at TEXT,
    updated_at TEXT
);
"""

# Migration for existing DBs (admin's legacy data/jobs.db) that don't have
# the gate columns yet. Each ALTER is wrapped in a guard so it's a no-op on
# already-migrated DBs.
_GATE_MIGRATIONS = [
    "ALTER TABLE jobs ADD COLUMN cv_ok INTEGER DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN anschreiben_ok INTEGER DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN motivation_ok INTEGER DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN approved_at TEXT",
]


def connect_user_db(user_id: str = ADMIN_USER_ID) -> sqlite3.Connection:
    """Open a SQLite connection to the user's jobs DB (creates file + schema if needed).

    For new users (user_id != 'lars'), the file at data/users/<id>/jobs.db is created
    on first connect, and the minimal jobs schema is applied via CREATE TABLE IF NOT EXISTS.
    For the admin user, the same CREATE TABLE IF NOT EXISTS is also applied — this is a
    no-op on the legacy data/jobs.db (schema already exists from earlier migrations) but
    is essential when the legacy file is fresh (e.g. first Docker volume mount with an
    empty data/jobs.db), which previously crashed /api/jobs with "no such table: jobs".
    """
    p = get_user_db_path(user_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    is_new_user = user_id != ADMIN_USER_ID
    if is_new_user and not p.exists():
        # First-time user — bootstrap from template if present
        if USER_TEMPLATE_DIR.exists():
            tpl = USER_TEMPLATE_DIR / "jobs.db"
            if tpl.exists():
                import shutil
                shutil.copy(tpl, p)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    # Ensure schema exists for any user (cheap; CREATE TABLE IF NOT EXISTS is idempotent)
    conn.executescript(_USER_JOBS_SCHEMA)
    # Run gated migrations on existing DBs that lack the new fields.
    # Each ALTER would error on duplicate column, so we look at PRAGMA first.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    if "cv_ok" not in existing_cols:
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN cv_ok INTEGER DEFAULT 0")
        except Exception:
            pass
    if "anschreiben_ok" not in existing_cols:
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN anschreiben_ok INTEGER DEFAULT 0")
        except Exception:
            pass
    if "motivation_ok" not in existing_cols:
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN motivation_ok INTEGER DEFAULT 0")
        except Exception:
            pass
    if "approved_at" not in existing_cols:
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN approved_at TEXT")
        except Exception:
            pass
    conn.commit()
    return conn
