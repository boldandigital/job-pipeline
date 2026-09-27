"""Database helpers for the job search pipeline.

Canonical schema lives in :mod:`src.db.schema` (loaded from
``src/db/schema.sql``). ADOPT-9 pulled the inline ``CREATE TABLE`` from the
three scrapers (arbeitsagentur / stepstone / import_jobspy) into a single
file so a fresh install no longer crashes on ``no such table: jobs``.

Usage:
    from src.db import init_db, ensure_data_dirs

    ensure_data_dirs(project_dir)               # mkdir -p data/ logs/
    init_db("./data/jobs.db")                   # creates table + indexes

Both functions are idempotent — safe to call repeatedly (e.g. once from
``setup.sh`` and once defensively from ``lars-daily-run.sh`` at startup).
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Union

# Resolve the schema.sql path relative to this file (not the caller's CWD).
# Layout: src/db/__init__.py  ↔  src/db/schema.sql
_HERE = Path(__file__).resolve().parent
_SCHEMA_PATH = _HERE / "schema.sql"

__all__ = ["init_db", "ensure_data_dirs", "ensure_dirs"]


def ensure_dirs(*paths: Union[str, os.PathLike]) -> None:
    """mkdir -p for each path. No-op if path already exists or is empty."""
    for p in paths:
        if not p:
            continue
        Path(p).mkdir(parents=True, exist_ok=True)


def ensure_data_dirs(project_dir: Union[str, os.PathLike]) -> None:
    """Create ``data/`` and ``logs/`` under ``project_dir`` if missing.

    Called by both setup.sh and the daily-run wrapper, so cron jobs that
    land on a fresh checkout don't crash on missing dirs.
    """
    project = Path(project_dir)
    ensure_dirs(project / "data", project / "logs")


def init_db(db_path: Union[str, os.PathLike]) -> sqlite3.Connection:
    """Create/connect to ``db_path`` and apply the canonical schema.

    Idempotent: ``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT
    EXISTS`` — safe to call on every startup. Returns the open connection;
    caller is responsible for ``conn.close()`` (or use as context manager).

    Args:
        db_path: Absolute or project-relative path to the SQLite file.
                 Parent directory is created if missing.

    Returns:
        Open :class:`sqlite3.Connection` with the canonical ``jobs`` table.
    """
    db = Path(db_path)
    parent = db.parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db))
    # WAL is friendlier for concurrent scraper + scorer; busy_timeout avoids
    # "database is locked" churn on macOS where file locking behaves loosely.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")

    sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(sql)
    conn.commit()
    return conn
