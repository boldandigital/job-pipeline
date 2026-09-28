#!/usr/bin/env python3
"""
MAIL-2 — migration idempotency tests.

Covers the four contracts of ``scripts/migrate_schema_add_lifecycle.py``:

  1. Fresh DB (jobs table missing) → no-op (init_db() handles it).
  2. Pre-MAIL-2 DB (jobs exists, lifecycle cols missing) → ALTER TABLE adds
     ``salary_range`` + ``cv_version``, creates ``idx_jobs_outcome``, and
     backfills ``outcome='pending'`` on legacy NULL rows.
  3. Re-run on the same DB → summary has ``already_current=True`` and
     nothing changed.
  4. Hybrid DB (some cols present, some missing) → only the missing
     columns are added; existing data is preserved.

Run:
    .venv/bin/python -m pytest tests/test_schema_migration.py -v
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

# Make ``scripts.*`` importable from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.migrate_schema_add_lifecycle import (  # noqa: E402
    LEGACY_OUTCOME_DEFAULT,
    NEW_COLUMNS,
    NEW_INDEXES,
    run,
)
from src.sheet.google_writer import LIFECYCLE_DB_COLUMNS  # noqa: E402


def _make_pre_mail2_db(path: Path) -> None:
    """A SQLite DB that has the pre-MAIL-2 schema (ADOPT-11 + MAIL-1, no MAIL-2 cols)."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            location TEXT,
            url TEXT,
            score INTEGER DEFAULT 0,
            status TEXT DEFAULT 'new',
            outcome TEXT,                  -- nullable on purpose
            mail_received_at TIMESTAMP,     -- MAIL-1 cols
            last_email_subject TEXT,
            last_email_at TIMESTAMP,
            interview_at TIMESTAMP,
            applied_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(title, company)
        );
        INSERT INTO jobs (title, company, score, outcome, mail_received_at) VALUES
            ('CTO',          'Acme AG',     180, NULL,        NULL),
            ('Founder',      'Beta GmbH',   220, 'received',  '2026-09-26 10:00:00'),
            ('DevOps Eng',   'Gamma Inc',    50, NULL,        NULL);
    """)
    conn.commit()
    conn.close()


def _columns(path: Path) -> set[str]:
    conn = sqlite3.connect(str(path))
    rows = conn.execute("PRAGMA table_info(jobs)").fetchall()
    conn.close()
    return {r[1] for r in rows}


def _index_names(path: Path) -> set[str]:
    conn = sqlite3.connect(str(path))
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_jobs_%'"
    ).fetchall()
    conn.close()
    return {r[0] for r in rows}


def _outcomes(path: Path) -> list[str]:
    conn = sqlite3.connect(str(path))
    rows = conn.execute("SELECT outcome FROM jobs ORDER BY id").fetchall()
    conn.close()
    return [r[0] for r in rows]


# ----------------------------------------------------------------------
# 1. Fresh DB → no-op (init_db creates the schema; we have nothing to do)
# ----------------------------------------------------------------------

def test_fresh_db_is_noop(tmp_path: Path) -> None:
    """A DB that doesn't exist yet should not raise. init_db() handles it."""
    db = tmp_path / "jobs.db"
    assert not db.exists()
    summary = run(str(db))
    assert summary["already_current"] is True
    assert summary["added"] == []
    assert summary["indexes"] == []
    assert summary["backfilled"] == 0


def test_db_without_jobs_table_is_noop(tmp_path: Path) -> None:
    """Empty DB (no jobs table) — we shouldn't crash trying to ALTER it."""
    db = tmp_path / "empty.db"
    sqlite3.connect(str(db)).close()  # create file, no tables
    summary = run(str(db))
    assert summary["already_current"] is True


# ----------------------------------------------------------------------
# 2. Pre-MAIL-2 DB → ALTER TABLE adds the two new cols + index + backfill
# ----------------------------------------------------------------------

def test_adds_lifecycle_columns_to_existing_db(tmp_path: Path) -> None:
    db = tmp_path / "jobs.db"
    _make_pre_mail2_db(db)

    summary = run(str(db))

    # Both new columns added
    expected_new = {name for name, _ in NEW_COLUMNS}
    assert set(summary["added"]) == expected_new
    # idx_jobs_outcome created
    assert "idx_jobs_outcome" in summary["indexes"]
    # Legacy NULL outcomes backfilled
    assert summary["backfilled"] == 2  # the two NULL rows
    # The 'received' row was not touched
    outcomes = _outcomes(db)
    assert outcomes[0] == "pending"           # was NULL
    assert outcomes[1] == "received"          # was already set
    assert outcomes[2] == "pending"           # was NULL
    # Schema reflects the additions
    cols = _columns(db)
    assert "salary_range" in cols
    assert "cv_version" in cols
    # Index exists
    assert "idx_jobs_outcome" in _index_names(db)


# ----------------------------------------------------------------------
# 3. Re-run on the same DB → idempotent no-op
# ----------------------------------------------------------------------

def test_second_run_is_idempotent_noop(tmp_path: Path) -> None:
    db = tmp_path / "jobs.db"
    _make_pre_mail2_db(db)

    run(str(db))   # 1st run — does the work
    summary2 = run(str(db))  # 2nd run — must be a no-op

    assert summary2["already_current"] is True
    assert summary2["added"] == []
    assert summary2["indexes"] == []
    assert summary2["backfilled"] == 0


def test_second_run_preserves_existing_outcomes(tmp_path: Path) -> None:
    """A re-run must not clobber an already-set outcome."""
    db = tmp_path / "jobs.db"
    _make_pre_mail2_db(db)

    run(str(db))
    # Now manually set a row to 'offer' — second run must leave it.
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE jobs SET outcome = 'offer' WHERE company = 'Beta GmbH'")
    conn.commit()
    conn.close()

    run(str(db))
    outcomes = _outcomes(db)
    assert outcomes[1] == "offer"   # not 'pending'


# ----------------------------------------------------------------------
# 4. Hybrid DB → only the missing columns are added
# ----------------------------------------------------------------------

def test_partial_db_only_adds_missing_columns(tmp_path: Path) -> None:
    """If salary_range already exists (e.g. partial manual migration),
    only cv_version should be added. The pre-existing column is untouched."""
    db = tmp_path / "hybrid.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            score INTEGER DEFAULT 0,
            outcome TEXT,
            salary_range TEXT,                 -- already present
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(title, company)
        );
        INSERT INTO jobs (title, company, salary_range) VALUES
            ('Founder', 'Acme', '€80-100k'),
            ('CTO',     'Beta', NULL);
    """)
    conn.commit()
    conn.close()

    summary = run(str(db))

    # Only cv_version was added
    assert summary["added"] == ["cv_version"]
    # salary_range data is preserved
    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT title, salary_range FROM jobs ORDER BY id"
    ).fetchall()
    conn.close()
    assert rows[0][1] == "€80-100k"
    assert rows[1][1] is None
    # Both NULLs got backfilled
    assert summary["backfilled"] == 2


# ----------------------------------------------------------------------
# Smoke: module-level constants are sane
# ----------------------------------------------------------------------

def test_module_constants_align_with_spec() -> None:
    """The migration's NEW_COLUMNS list must match the MAIL-2 spec."""
    cols = {name for name, _ in NEW_COLUMNS}
    assert cols == {"salary_range", "cv_version"}

    # The backfill default is 'pending' (matches OUTCOMES[0]).
    assert LEGACY_OUTCOME_DEFAULT == "pending"

    # The new index is on the outcome column.
    assert any("idx_jobs_outcome" in ddl for ddl in NEW_INDEXES)


def test_lifecycle_db_columns_order_matches_default_headers_tail() -> None:
    """LIFECYCLE_DB_COLUMNS is the spec-level order; sync_lifecycle relies
    on it for the SELECT projection."""
    # Skip id/title/company at the front (used for the join key).
    lifecycle_only = LIFECYCLE_DB_COLUMNS[3:]
    assert lifecycle_only == (
        "mail_received_at",
        "last_email_subject",
        "last_email_at",
        "interview_at",
        "outcome",
        "salary_range",
        "cv_version",
    )