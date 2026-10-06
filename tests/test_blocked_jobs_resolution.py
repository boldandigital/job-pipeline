"""
tests/test_blocked_jobs_resolution.py

Phase 1.8 unblock resolution tests. Verifies that:

1. The two EYES_UNBLOCK_SOLUTION.md files exist and have non-empty
   content (Lightcone + cfab).
2. The Andercore EMAIL_QUALITY_REPORT.md exists.
3. The two XING Easy-Apply drafts (xHeron, businessangels) carry the
   "PDFs verified fresh on 2026-10-06 21:58" footer line.
4. The two formerly-BLOCKED jobs in data/jobs.db (id=56 Lightcone,
   id=80 cfab) have been flipped to status='needs_human'.

The tests are hermetic — they read real files on disk and the real
local SQLite DB; nothing is mocked because there is no network, no
browser, no side effect, and no email sent. The whole point of
Phase 1.8 is to *land* these artifacts and DB rows, not to test
their content correctness in depth (that's what the human reviewer
does via the .md files).

Run:  pytest tests/test_blocked_jobs_resolution.py -v
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

# Make the project root importable for `import data.jobs_db`-style
# helpers (the conftest already does this for `src.*` packages; the
# batch-level tests here just need Path resolution).
ROOT = Path(__file__).resolve().parent.parent
BATCH_DIR = ROOT / "data" / "batches" / "2026-10-02"
DB_PATH = ROOT / "data" / "jobs.db"

# The exact footer line that Phase 1.8 mandates on every XING draft.
EXPECTED_FOOTER = "PDFs verified fresh on 2026-10-06 21:58 — use these."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_text(path: Path) -> str:
    """Read a UTF-8 text file; raise with a useful message on missing."""
    if not path.exists():
        raise FileNotFoundError(f"expected file missing: {path}")
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. EYES_UNBLOCK_SOLUTION.md artifacts
# ---------------------------------------------------------------------------


def test_lightcone_unblock_md_exists_with_nonempty_content():
    """Lightcone EYES_UNBLOCK_SOLUTION.md exists, is non-empty, and
    references the real .capital TLD (not the parked .com)."""
    p = BATCH_DIR / "Lightcone_Capital" / "EYES_UNBLOCK_SOLUTION.md"
    text = _read_text(p)
    assert len(text) > 200, f"file too short ({len(text)} chars) — empty stub?"
    # The file MUST mention the real domain, otherwise the run is wrong.
    assert "lightcone.capital" in text, (
        "expected the real .capital TLD in the Lightcone unblock report; "
        "the DB's .com URL is a parked domain"
    )
    # The file MUST mention the verified contact email.
    assert "berlin@lightcone.capital" in text, (
        "expected berlin@lightcone.capital (verified on the real /contact page)"
    )
    # Sanity: it should also flag the parked-domain misdiagnosis.
    assert "parked" in text.lower() or "lander" in text.lower(), (
        "expected the unblock report to flag the parked-domain root cause"
    )


def test_cfab_unblock_md_exists_with_nonempty_content():
    """cfab EYES_UNBLOCK_SOLUTION.md exists, is non-empty, and quotes
    the explicit on-site-in-Düsseldorf requirement from the JD."""
    p = BATCH_DIR / "cfab" / "EYES_UNBLOCK_SOLUTION.md"
    text = _read_text(p)
    assert len(text) > 200, f"file too short ({len(text)} chars) — empty stub?"
    # Must quote the actual geographic-requirement sentence from the JD.
    assert "täglich" in text and "Düsseldorf" in text, (
        "expected the unblock report to quote the JD's 'täglich an unserem "
        "Standort in Düsseldorf präsent zu sein' requirement"
    )
    # Must name the company properly (Multiecom GmbH), proving we went
    # past the XING listing to verify the real entity.
    assert "Multiecom" in text, (
        "expected the unblock report to identify Multiecom GmbH as cfab's "
        "legal entity (verified via career.cfab.com + impressum)"
    )


# ---------------------------------------------------------------------------
# 2. Andercore email quality report
# ---------------------------------------------------------------------------


def test_andercore_email_quality_md_exists():
    """EMAIL_QUALITY_REPORT.md exists, is non-empty, and references the
    audited email file (no body modifications performed)."""
    p = BATCH_DIR / "Andercore" / "EMAIL_QUALITY_REPORT.md"
    text = _read_text(p)
    assert len(text) > 500, f"file too short ({len(text)} chars)"
    # It must reference the audited email file by name.
    assert "email_to_talent_at_andercore.txt" in text, (
        "expected the report to reference the audited email file"
    )
    # It must declare read-only intent (no body edits were performed).
    assert "READ-ONLY" in text or "not modified" in text.lower(), (
        "expected the report to declare that the email body was not modified"
    )
    # It must contain at least one line-numbered issue (the format the
    # brief mandates: "line numbers for any issues found").
    assert "Line" in text or "L" in text, (
        "expected line-numbered issue references (e.g. 'Line 18' / 'L18')"
    )


# ---------------------------------------------------------------------------
# 3. XING Easy-Apply drafts — fresh-PDF footer
# ---------------------------------------------------------------------------


def test_xheron_draft_has_fresh_pdf_footer():
    """xHeron XING draft carries the Phase 1.8 footer line and did not
    lose its existing content (body still ends with the salary/contact
    block, not replaced by the footer)."""
    p = BATCH_DIR / "xHeron_Solutions" / "xing_application_draft.txt"
    text = _read_text(p)
    assert EXPECTED_FOOTER in text, (
        f"missing footer line: {EXPECTED_FOOTER!r}"
    )
    # Regression: the existing body must still be there.
    assert "Founding Marketer" in text, (
        "regression: body content was clobbered while adding the footer"
    )
    assert "Salary expectation" in text, (
        "regression: salary/contact block was clobbered while adding the footer"
    )


def test_businessangels_draft_has_fresh_pdf_footer():
    """businessangels.de XING draft carries the Phase 1.8 footer line
    and the existing body is intact."""
    p = BATCH_DIR / "businessangels_de" / "xing_application_draft.txt"
    text = _read_text(p)
    assert EXPECTED_FOOTER in text, (
        f"missing footer line: {EXPECTED_FOOTER!r}"
    )
    # Regression: the existing body must still be there.
    assert "Chief Operating Officer" in text or "COO" in text, (
        "regression: body content was clobbered while adding the footer"
    )
    assert "Salary expectation" in text, (
        "regression: salary/contact block was clobbered while adding the footer"
    )


# ---------------------------------------------------------------------------
# 4. DB status flips for the two formerly-BLOCKED jobs
# ---------------------------------------------------------------------------


@pytest.fixture
def jobs_db():
    """Read-only connection to the real local jobs.db."""
    if not DB_PATH.exists():
        pytest.skip(f"jobs.db not present at {DB_PATH}")
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def test_lightcone_job_status_is_needs_human(jobs_db):
    """Job id=56 (Lightcone EIR) is now status='needs_human' and was
    updated after the original 'new' status (updated_at > created_at)."""
    row = jobs_db.execute(
        "SELECT status, created_at, updated_at FROM jobs WHERE id = 56"
    ).fetchone()
    assert row is not None, "jobs row id=56 (Lightcone) missing from DB"
    assert row["status"] == "needs_human", (
        f"expected status='needs_human', got {row['status']!r} "
        f"(created_at={row['created_at']}, updated_at={row['updated_at']})"
    )
    # updated_at should be strictly newer than created_at (proves we
    # actually wrote the row, not just inherited an old status).
    assert row["updated_at"] > row["created_at"], (
        f"updated_at ({row['updated_at']}) should be newer than "
        f"created_at ({row['created_at']})"
    )


def test_cfab_job_status_is_needs_human(jobs_db):
    """Job id=80 (cfab COO) is now status='needs_human' and was
    updated after the original 'new' status."""
    row = jobs_db.execute(
        "SELECT status, created_at, updated_at FROM jobs WHERE id = 80"
    ).fetchone()
    assert row is not None, "jobs row id=80 (cfab) missing from DB"
    assert row["status"] == "needs_human", (
        f"expected status='needs_human', got {row['status']!r} "
        f"(created_at={row['created_at']}, updated_at={row['updated_at']})"
    )
    assert row["updated_at"] > row["created_at"], (
        f"updated_at ({row['updated_at']}) should be newer than "
        f"created_at ({row['created_at']})"
    )
