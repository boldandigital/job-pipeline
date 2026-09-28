#!/usr/bin/env python3
"""Idempotent migration: add MAIL-2 lifecycle columns + outcome backfill.

Background
----------
MAIL-2 extends the Sheet→DB lifecycle tracking that MAIL-1 started. We add
two more columns that the daily Google Sheet needs to surface the full
pipeline state (salary_range + cv_version) plus a single-column index on
``outcome`` for ad-hoc ``WHERE outcome = ?`` queries (the existing
``idx_jobs_company_outcome`` composite from MAIL-1 also satisfies those).

We also backfill ``outcome = 'pending'`` for any legacy row whose outcome
is NULL — this gives the Sheet a clean default to compare against in the
data-validation dropdown, and matches the enum documented in
:data:`src.sheet.google_writer.OUTCOMES`.

Idempotency
-----------
- Column adds use the same ``PRAGMA table_info`` probe pattern as the
  MAIL-1 migration (``scripts/migrate_schema_add_mail_columns.py``).
- The index uses ``CREATE INDEX IF NOT EXISTS`` — SQLite native.
- The outcome backfill is ``WHERE outcome IS NULL``, so re-runs are a no-op.

Usage
-----
    python3 scripts/migrate_schema_add_lifecycle.py
    python3 scripts/migrate_schema_add_lifecycle.py --db /opt/jp/data/jobs.db
    # or via wrapper:
    bash scripts/lars-daily-run.sh   # calls this on startup if DB_PATH set

Returns
-------
A summary dict (also printed to stdout) with keys:
    added         — column names that were ALTER-ADDed this run
    indexes       — DDL that created an index this run
    backfilled    — number of rows whose outcome was set to 'pending'
    already_current — True when nothing changed (idempotent re-run)
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

logging = __import__("logging")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("migrate_lifecycle")

# Resolve project root relative to this script so we can run it from anywhere.
_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent  # scripts/ -> job-pipeline/
_DB_PATH = os.getenv("DB_PATH") or str(_PROJECT_ROOT / "data" / "jobs.db")

# Columns this migration guarantees on the `jobs` table. Tuple of (name, decl).
NEW_COLUMNS: list[tuple[str, str]] = [
    ("salary_range", "TEXT"),
    ("cv_version", "TEXT"),
]

# Indexes added on top of MAIL-1's idx_jobs_company_outcome composite.
# Single-column index covers ``WHERE outcome = ?`` lookups cheaply.
NEW_INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_jobs_outcome ON jobs(outcome)",
]

# The enum we backfill NULLs to. Kept in sync with src/sheet/google_writer
# (single source of truth for the enum is OUTCOMES there — this is the
# default sentinel for legacy rows; sheet sync respects the OUTCOMES list).
LEGACY_OUTCOME_DEFAULT = "pending"


def _existing_columns(conn: sqlite3.Connection, table: str = "jobs") -> set[str]:
    """Return the set of column names currently declared on ``table``."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    # PRAGMA table_info returns: cid, name, type, notnull, dflt_value, pk
    return {row[1] for row in rows}


def _index_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _add_column_if_missing(
    conn: sqlite3.Connection, name: str, decl: str
) -> bool:
    """ALTER TABLE if column is missing. Return True iff we altered."""
    if name in _existing_columns(conn):
        return False
    log.info("Adding column jobs.%s %s ...", name, decl)
    conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {decl}")
    return True


def _ensure_index(conn: sqlite3.Connection, ddl: str, name: str) -> bool:
    """Return True iff the index was created this call."""
    if _index_exists(conn, name):
        return False
    log.info("Creating index %s ...", name)
    conn.execute(ddl)
    return True


def _backfill_outcome(conn: sqlite3.Connection) -> int:
    """Set outcome='pending' on rows where it is NULL. Returns rowcount.

    Safe to re-run: the WHERE clause filters to NULL only. The MAIL-1
    migration leaves outcome as nullable so legacy rows are NULL here.
    """
    cur = conn.execute(
        "UPDATE jobs SET outcome = ? WHERE outcome IS NULL",
        (LEGACY_OUTCOME_DEFAULT,),
    )
    return cur.rowcount


def run(db_path: str = _DB_PATH) -> dict:
    """Apply the migration. Returns a summary dict for tests + logs."""
    db = Path(db_path)
    if not db.exists():
        # Fresh checkout — init_db() will create the canonical schema and
        # the lifecycle columns are inherited from src/db/schema.sql. Nothing
        # for us to do.
        log.info("DB not found at %s — nothing to migrate (it will be created with full schema)", db_path)
        return {
            "created_new": False,
            "added": [],
            "indexes": [],
            "backfilled": 0,
            "already_current": True,
        }

    conn = sqlite3.connect(db)
    try:
        # Bail if the jobs table doesn't exist yet — init_db() handles it.
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='jobs'"
        ).fetchone()
        if row is None:
            log.warning("`jobs` table missing in %s — nothing to migrate", db_path)
            return {
                "created_new": False,
                "added": [],
                "indexes": [],
                "backfilled": 0,
                "already_current": True,
            }

        added: list[str] = []
        for name, decl in NEW_COLUMNS:
            if _add_column_if_missing(conn, name, decl):
                added.append(name)

        indexes: list[str] = []
        for ddl in NEW_INDEXES:
            # Pull the index name out of the DDL — "CREATE INDEX IF NOT
            # EXISTS idx_jobs_outcome ON jobs(outcome)" -> 'idx_jobs_outcome'.
            name = ddl.split("EXISTS", 1)[1].split("ON", 1)[0].strip()
            if _ensure_index(conn, ddl, name):
                indexes.append(name)

        backfilled = _backfill_outcome(conn)

        conn.commit()

        already_current = not added and not indexes and backfilled == 0
        if backfilled:
            log.info(
                "Backfilled outcome='pending' on %d legacy row(s)", backfilled
            )

        return {
            "created_new": False,
            "added": added,
            "indexes": indexes,
            "backfilled": backfilled,
            "already_current": already_current,
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Ensure jobs table has MAIL-2 lifecycle columns "
            "(salary_range, cv_version) + idx_jobs_outcome + outcome='pending' "
            "backfill for legacy NULL rows."
        )
    )
    parser.add_argument("--db", default=_DB_PATH, help="Path to SQLite jobs DB")
    args = parser.parse_args()

    summary = run(args.db)
    if summary["added"]:
        print(f"[migrate] added columns: {summary['added']}")
    if summary["indexes"]:
        print(f"[migrate] added indexes: {summary['indexes']}")
    if summary["backfilled"]:
        print(f"[migrate] backfilled outcome='pending' on {summary['backfilled']} row(s)")
    if summary["already_current"]:
        print("[migrate] schema already current — nothing to do")
    return 0


if __name__ == "__main__":
    sys.exit(main())