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


def connect_user_db(user_id: str = ADMIN_USER_ID) -> sqlite3.Connection:
    """Open a SQLite connection to the user's jobs DB (creates file if needed)."""
    p = get_user_db_path(user_id)
    if not p.exists() and user_id != ADMIN_USER_ID:
        # First-time user — bootstrap from template
        p.parent.mkdir(parents=True, exist_ok=True)
        if USER_TEMPLATE_DIR.exists():
            tpl = USER_TEMPLATE_DIR / "jobs.db"
            if tpl.exists():
                import shutil
                shutil.copy(tpl, p)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    return conn
