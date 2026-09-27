#!/usr/bin/env python3
"""Idempotent migration: ensure `jobs` table has `career_url` + `description` columns.

Background
----------
ADOPT-6 fix for the schema gap discovered in ADOPT-5. The pipeline reads
`career_url` + `description` from the `jobs` table, but the arbeitsagentur
and stepstone scraper CREATE TABLE statements omitted them. setup.sh ships a
unified schema that already includes both columns, but any DB created by
one of those scrapers first would lack them.

This script is safe to run repeatedly: it inspects PRAGMA table_info first,
adds the columns only when missing, and skips silently otherwise.

Usage
-----
    python3 scripts/migrate_schema_add_career_description.py            # default ./data/jobs.db
    python3 scripts/migrate_schema_add_career_description.py --db /opt/job-pipeline/data/jobs.db

Backfill
--------
If `career_url` is added to an existing DB that already has `url` rows,
backfill `career_url = url` so the column is non-empty for downstream ATS
discovery.
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("migrate_schema")

DB_PATH = os.getenv("DB_PATH", "./data/jobs.db")

# Columns this migration guarantees on the `jobs` table.
REQUIRED_COLUMNS = {
    "career_url": "TEXT",
    "description": "TEXT",
}


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Return True if `column` exists on `table` per PRAGMA table_info."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    # PRAGMA table_info returns: cid, name, type, notnull, dflt_value, pk
    return any(r[1] == column for r in rows)


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def migrate(db_path: str) -> int:
    """Apply the migration. Returns number of ALTER TABLE statements executed."""
    if not os.path.exists(db_path):
        log.warning("DB not found at %s — nothing to migrate (it will be created with full schema)", db_path)
        return 0

    conn = sqlite3.connect(db_path)
    try:
        if not table_exists(conn, "jobs"):
            log.warning("`jobs` table missing in %s — nothing to migrate", db_path)
            return 0

        applied = 0
        for col, col_type in REQUIRED_COLUMNS.items():
            if column_exists(conn, "jobs", col):
                log.info("Column jobs.%s already exists — skipping", col)
                continue
            log.info("Adding column jobs.%s %s ...", col, col_type)
            # SQLite ALTER TABLE: ADD COLUMN only (no IF NOT EXISTS pre-3.35).
            # We guard with PRAGMA table_info above so this is safe to re-run.
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {col_type}")
            applied += 1

        # Backfill: existing rows had career_url omitted. Best heuristic for the
        # scrapers in this repo: alias `url` -> `career_url` when career_url
        # is empty. Both arbeitsagentur and stepstone store the posting URL in
        # `url`, and the pipeline treats career_url as the company careers page
        # for ATS discovery. Fall back to url; ATS discovery will refine later.
        if column_exists(conn, "jobs", "career_url") and column_exists(
            conn, "jobs", "url"
        ):
            updated = conn.execute(
                """
                UPDATE jobs
                   SET career_url = url
                 WHERE (career_url IS NULL OR career_url = '')
                   AND url IS NOT NULL AND url != ''
                """
            ).rowcount
            if updated:
                log.info("Backfilled career_url from url for %d existing rows", updated)
            else:
                log.info("No rows needed career_url backfill")

        conn.commit()
        log.info(
            "Migration complete: %d column(s) added to %s", applied, db_path
        )
        return applied
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ensure jobs table has career_url + description columns."
    )
    parser.add_argument("--db", default=DB_PATH, help="Path to SQLite jobs DB")
    args = parser.parse_args()
    migrate(args.db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
