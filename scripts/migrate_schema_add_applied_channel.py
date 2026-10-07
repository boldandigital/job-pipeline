#!/usr/bin/env python3
"""Idempotent migration: add ``applied_channel`` to the ``jobs`` table.

Background
----------
The apply orchestrator submits through one of two channels — an HTTP API
POST (Greenhouse / Lever / Ashby) or a Playwright browser fill that pauses
for human submit (XING / LinkedIn / Indeed / Workday). Recording *which*
one fired is what makes an application auditable after the fact, and it is
the column the orchestrator writes on a successful submission.

``applied_channel`` is deliberately OPTIONAL at the orchestrator level: it
probes ``PRAGMA table_info`` before writing and falls back to a plain
``status``/``applied_at`` update when the column is absent. This migration
exists so the admin DB and ``src/db/schema.sql`` gain the column for real
rather than relying on that fallback forever.

Idempotency
-----------
Column add is guarded by a ``PRAGMA table_info`` probe, so re-runs are a
no-op. Safe to call on every boot.

Usage
-----
    python3 scripts/migrate_schema_add_applied_channel.py
    python3 scripts/migrate_schema_add_applied_channel.py --db /opt/jp/data/jobs.db

Returns a summary dict (also printed):
    added           — columns ALTER-ADDed this run ([] when already current)
    already_current — True when nothing changed
    path            — DB that was inspected
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("migrate_applied_channel")

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent  # scripts/ -> job-pipeline/
_DB_PATH = os.getenv("DB_PATH") or str(_PROJECT_ROOT / "data" / "jobs.db")

#: Column this migration guarantees. (name, declaration)
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("applied_channel", "TEXT"),
)


def migrate(db_path: str) -> dict:
    """Apply the migration to ``db_path``. Returns a summary dict."""
    added: list[str] = []
    if not Path(db_path).exists():
        raise FileNotFoundError(f"DB not found at {db_path}")

    conn = sqlite3.connect(db_path)
    try:
        existing = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if not existing:
            log.warning("no `jobs` table at %s — nothing to migrate", db_path)
            return {"added": [], "already_current": True, "path": db_path}

        for name, decl in _COLUMNS:
            if name in existing:
                continue
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {decl}")
            added.append(name)
            log.info("added column %s %s", name, decl)

        conn.commit()
    finally:
        conn.close()

    return {"added": added, "already_current": not added, "path": db_path}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Add the applied_channel column to the jobs table (idempotent)."
    )
    parser.add_argument("--db", default=_DB_PATH, help="Path to the jobs SQLite DB")
    args = parser.parse_args(argv)
    try:
        summary = migrate(args.db)
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return 1
    log.info("migration summary: %s", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())