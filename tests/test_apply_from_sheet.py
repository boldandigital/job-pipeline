#!/usr/bin/env python3
"""
FIX-11 regression test for bin/apply-from-sheet.sh --dry-run.

QA-11 caught a bug: the dry-run SELECT referenced a `language` column that
is NOT in the canonical schema (ADOPT-9's schema.sql has 22 columns, no
`language`). Running against the real DB produced:

    sqlite3.OperationalError: no such column: language

This test pins the fix two ways:

  1. Static — confirms no `language` column reference remains in the
     SELECT string built by build_sql().
  2. Dynamic — applies the canonical schema.sql to a temp DB, runs
     `bash bin/apply-from-sheet.sh --dry-run` against it, and asserts
     the script exits 0 with no OperationalError.

Run:
    .venv/bin/python -m pytest tests/test_apply_from_sheet.py -v
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# 1. Static check — the bug was a literal `language` in the SQL string
# ---------------------------------------------------------------------------

def test_no_language_column_in_apply_from_sheet_sh() -> None:
    """The dry-run SELECT must not reference a `language` column.

    The canonical jobs table (src/db/schema.sql) has no `language` column.
    Re-introducing it in bin/apply-from-sheet.sh would break --dry-run
    again with OperationalError.
    """
    script = (REPO_ROOT / "bin" / "apply-from-sheet.sh").read_text()

    # Match any `language` token that sits inside a SELECT statement.
    # Tolerates whitespace and case-insensitivity of the SQL keyword.
    select_blocks = re.findall(
        r"SELECT\b[^;]*?(?=FROM|$)", script, flags=re.IGNORECASE | re.DOTALL
    )
    assert select_blocks, "expected at least one SELECT in apply-from-sheet.sh"

    for block in select_blocks:
        assert "language" not in block.lower(), (
            f"Found `language` reference in SELECT block:\n{block}\n"
            "The canonical jobs schema has no `language` column."
        )


# ---------------------------------------------------------------------------
# 2. Dynamic check — actually run the dry-run against a fresh canonical DB
# ---------------------------------------------------------------------------

def _apply_canonical_schema(db_path: Path) -> None:
    """Create a DB matching src/db/schema.sql exactly."""
    schema_sql = (REPO_ROOT / "src" / "db" / "schema.sql").read_text()
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(schema_sql)
        # Seed one approved job so the script has something to count
        # (still exits 0 even with zero rows, but this proves the SELECT works).
        conn.execute(
            """
            INSERT INTO jobs (title, company, location, url, score, status, source)
            VALUES (?, ?, ?, ?, ?, 'approved', 'stepstone')
            """,
            ("Senior Product Manager", "Acme GmbH", "Berlin",
             "https://acme.test/1", 180),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not available on this system"
)
def test_dry_run_exits_zero_against_canonical_schema(tmp_path: Path) -> None:
    """bin/apply-from-sheet.sh --dry-run must exit 0 on the canonical schema.

    Pre-fix this raised sqlite3.OperationalError: no such column: language.
    """
    db_path = tmp_path / "jobs.db"
    _apply_canonical_schema(db_path)

    env = os.environ.copy()
    env["DB_PATH"] = str(db_path)

    proc = subprocess.run(
        ["bash", str(REPO_ROOT / "bin" / "apply-from-sheet.sh"), "--dry-run"],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
    )

    assert proc.returncode == 0, (
        f"dry-run exited {proc.returncode}\n"
        f"--- STDOUT ---\n{proc.stdout}\n"
        f"--- STDERR ---\n{proc.stderr}"
    )
    # The pre-fix bug surfaced as this exact phrase. Guard against any
    # SQLite column-not-found error so we don't regress in any shape.
    combined = (proc.stdout + proc.stderr).lower()
    assert "operationalerror" not in combined, (
        f"OperationalError in dry-run output:\n{proc.stderr}"
    )
    assert "no such column" not in combined, (
        f"`no such column` in dry-run output — schema drift:\n{proc.stderr}"
    )


# ---------------------------------------------------------------------------
# 3. Belt-and-braces — bash syntax-check on every edit
# ---------------------------------------------------------------------------

def test_bash_syntax_clean() -> None:
    """bash -n must exit 0 on bin/apply-from-sheet.sh."""
    proc = subprocess.run(
        ["bash", "-n", str(REPO_ROOT / "bin" / "apply-from-sheet.sh")],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"bash -n failed: {proc.stderr}"
    )