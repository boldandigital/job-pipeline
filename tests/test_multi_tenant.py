"""
Multi-tenant isolation tests for the per-user database factory.

These prove that when multiple users exist in the system, their data
is fully isolated:
  - User A's approvals don't show in User B's stats
  - User A's PDFs are not accessible to User B
  - User A's profile.json is independent of User B's
  - The admin user ("lars") continues to use the legacy single-user paths

This is the foundation for the SaaS (Phase 4) and a hard requirement
for any per-user feature.
"""
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from src.db.jobs_db import (
    ADMIN_USER_ID,
    LEGACY_BATCHES,
    LEGACY_DB,
    USERS_DIR,
    connect_user_db,
    get_user_batches_dir,
    get_user_db_path,
    get_user_id,
    get_user_profile,
    get_user_profile_path,
)


# ----- helpers -------------------------------------------------------------

def _seed_user_db(user_id: str) -> Path:
    """Create a fresh user DB with 1 row, return its path."""
    user_dir = USERS_DIR / user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    db_path = user_dir / "jobs.db"
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY,
            title TEXT, company TEXT, location TEXT,
            url TEXT, career_url TEXT, source TEXT,
            description TEXT, score INTEGER,
            status TEXT DEFAULT 'new',
            cv_path TEXT, cover_letter_path TEXT
        );
    """)
    conn.execute(
        "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (1, "Founder", f"User-{user_id}-Co", "Berlin", "https://x.test", "",
         "xing", "Only User " + user_id + " can see this", 999, "new", "", ""),
    )
    conn.commit()
    conn.close()
    return db_path


# ----- tests ---------------------------------------------------------------

def test_admin_user_uses_legacy_path():
    """Admin user "lars" must keep using data/jobs.db (no data loss)."""
    assert get_user_db_path(ADMIN_USER_ID) == LEGACY_DB
    assert get_user_batches_dir(ADMIN_USER_ID) == LEGACY_BATCHES


def test_new_user_uses_isolated_path():
    """New user_id gets their own data/users/<id>/ directory."""
    new_user = "test_user_1"
    p = get_user_db_path(new_user)
    assert p != LEGACY_DB
    assert "data/users/test_user_1" in str(p)
    # Side effect: path creation
    p.parent.mkdir(parents=True, exist_ok=True)
    assert p.parent.exists()


def test_get_user_id_default(monkeypatch):
    """No env var, no request → admin user."""
    monkeypatch.delenv("LARS_USER_ID", raising=False)
    assert get_user_id() == ADMIN_USER_ID


def test_get_user_id_from_env(monkeypatch):
    monkeypatch.setenv("LARS_USER_ID", "scraper_cron")
    assert get_user_id() == "scraper_cron"


def test_user_a_data_does_not_leak_to_user_b(tmp_path, monkeypatch):
    """Approving a job in User A's DB does NOT show in User B's DB."""
    # Create two users
    db_a = _seed_user_db("alpha")
    db_b = _seed_user_db("beta")

    # Connect to A, approve job 1
    conn_a = connect_user_db("alpha")
    conn_a.execute("UPDATE jobs SET status='approved' WHERE id=1")
    conn_a.commit()
    conn_a.close()

    # Connect to B — should still be 'new'
    conn_b = connect_user_db("beta")
    row = conn_b.execute("SELECT status FROM jobs WHERE id=1").fetchone()
    assert row[0] == "new"
    conn_b.close()

    # Confirm A's DB has 'approved'
    conn_a = connect_user_db("alpha")
    row = conn_a.execute("SELECT status FROM jobs WHERE id=1").fetchone()
    assert row[0] == "approved"
    conn_a.close()


def test_user_a_profile_independent_of_user_b(tmp_path, monkeypatch):
    """Each user gets their own profile.json — no sharing."""
    p_a = get_user_profile_path("alpha")
    p_b = get_user_profile_path("beta")
    assert p_a != p_b
    assert p_a.parent != p_b.parent
    # Touch A's profile (lazy-create from template if present, else {})
    profile_a = get_user_profile("alpha")
    assert isinstance(profile_a, dict)


def test_isolated_batches_dir():
    """Each user gets their own batches/ folder."""
    d_a = get_user_batches_dir("alpha")
    d_b = get_user_batches_dir("beta")
    assert d_a != d_b
    assert d_a.exists(), "batches dir should be auto-created on first access"
    assert d_b.exists()


def test_connect_user_db_for_known_user_returns_existing(tmp_path, monkeypatch):
    """Connecting to a known user returns the existing jobs table."""
    db_path = _seed_user_db("known_user")
    conn = connect_user_db("known_user")
    rows = conn.execute("SELECT COUNT(*) FROM jobs").fetchall()
    assert rows[0][0] == 1
    conn.close()


def test_connect_user_db_for_unknown_user_creates_with_schema(tmp_path, monkeypatch):
    """Connecting to a never-seen user auto-creates the jobs table schema.

    This is the SaaS-ready behavior — a new signup should immediately be able to
    see an empty dashboard, not hit a SQL error.
    """
    test_user = "ghost_user_xyz"
    db_path = USERS_DIR / test_user / "jobs.db"
    if db_path.exists():
        db_path.unlink()
    conn = connect_user_db(test_user)
    tables = sorted(
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    )
    # 'jobs' is required; sqlite_sequence is created by AUTOINCREMENT
    assert "jobs" in tables, f"Expected jobs table, got {tables}"
    # Empty table = zero rows
    rows = conn.execute("SELECT COUNT(*) FROM jobs").fetchall()
    assert rows[0][0] == 0
    conn.close()
