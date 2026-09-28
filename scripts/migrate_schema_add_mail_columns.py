"""Idempotent migration: add mail-watcher lifecycle columns to jobs table.

MAIL-1 introduces the iCloud IMAP watcher. It writes back per-job state when
a reply is matched, so we need four new columns + one new index:

    outcome            TEXT    -- 'received' | 'interview' | 'offer' |
                                -- 'rejected' | 'declined' | NULL
    mail_received_at   TIMESTAMP  -- first time we saw a reply (idempotent)
    last_email_subject TEXT
    last_email_at      TIMESTAMP  -- last time we saw ANY email for this job
    interview_at       TIMESTAMP  -- best-effort date parse from invite
    rejection_reason    TEXT     -- already exists from ADOPT-11 (manual sync)
                                -- we add the email_rejection enum key here

Plus an index on (company, outcome) so the matcher can pick the most recent
applied job cheaply.

Idempotent: every statement uses `IF NOT EXISTS` semantics — safe to re-run.
Called from setup.sh and lars-daily-run.sh so the project remains
crash-free on a fresh checkout. Matches the ADOPT-9 / ADOPT-11 / ADOPT-13
pattern of "schema.sql + idempotent migration script".

Run:
    python3 scripts/migrate_schema_add_mail_columns.py
    # or via wrapper:
    bash scripts/lars-daily-run.sh  # (runs it on startup)
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

# Resolve project root relative to this script so we can run it from anywhere.
_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent  # scripts/ -> job-pipeline/
_DB_PATH = os.getenv("DB_PATH") or str(_PROJECT_ROOT / "data" / "jobs.db")

# ---------------------------------------------------------------------------
# SQL — every column add is a no-op if it already exists.
# SQLite has no IF NOT EXISTS for ALTER TABLE ADD COLUMN, so we probe
# pragma_table_info first and skip the column if it's already there.
# ---------------------------------------------------------------------------

NEW_COLUMNS: list[tuple[str, str]] = [
    ("outcome", "TEXT"),
    ("mail_received_at", "TIMESTAMP"),
    ("last_email_subject", "TEXT"),
    ("last_email_at", "TIMESTAMP"),
    ("interview_at", "TIMESTAMP"),
]

NEW_INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_jobs_company_outcome ON jobs(company, outcome)",
]


def _existing_columns(conn: sqlite3.Connection, table: str = "jobs") -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def _add_column_if_missing(
    conn: sqlite3.Connection, name: str, decl: str
) -> bool:
    """Return True if we actually altered the table."""
    if name in _existing_columns(conn):
        return False
    conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {decl}")
    return True


def run(db_path: str = _DB_PATH) -> dict:
    """Run the migration. Returns a summary dict for tests + logs."""
    db = Path(db_path)
    if not db.exists():
        # Fresh checkout — init_db() will create the canonical schema and
        # the mail columns will be inherited from src/db/schema.sql. Nothing
        # for us to do.
        return {"created_new": False, "added": [], "indexes": []}

    conn = sqlite3.connect(db)
    try:
        added: list[str] = []
        for name, decl in NEW_COLUMNS:
            if _add_column_if_missing(conn, name, decl):
                added.append(name)

        indexes: list[str] = []
        for ddl in NEW_INDEXES:
            before = conn.execute(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type='index' AND name LIKE 'idx_jobs_%'"
            ).fetchone()[0]
            conn.execute(ddl)
            after = conn.execute(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type='index' AND name LIKE 'idx_jobs_%'"
            ).fetchone()[0]
            if after > before:
                indexes.append(ddl)

        conn.commit()
        return {"created_new": False, "added": added, "indexes": indexes}
    finally:
        conn.close()


def main() -> int:
    summary = run()
    if summary["added"]:
        print(f"[migrate] added columns: {summary['added']}")
    if summary["indexes"]:
        print(f"[migrate] added indexes: {len(summary['indexes'])}")
    if not summary["added"] and not summary["indexes"]:
        print("[migrate] schema already current — nothing to do")
    return 0


if __name__ == "__main__":
    sys.exit(main())