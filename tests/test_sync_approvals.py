#!/usr/bin/env python3
"""
ADOPT-11 — Sheet→DB approval sync tests.

Mock the Sheets API so these run offline (no creds, no quota, no network).
Cover the four contracts the wrapper script depends on:

  1. Row pulling recognises the writer's status values
     (approved / rejected / blank) and routes them correctly.
  2. apply_to_db is idempotent — second call is a no-op.
  3. Unknown rejection_reason strings fall back to ('other', verbatim).
  4. SyncResult is JSON-serializable for the Discord summary line.

Run:
    .venv/bin/python -m pytest tests/test_sync_approvals.py -v
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Make `src.*` importable when pytest runs from the repo root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.sheet import sync_approvals as sa  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures: ephemeral SQLite DB matching the canonical schema
# ---------------------------------------------------------------------------

def _make_db(tmp_path: Path) -> Path:
    """Create a SQLite DB matching the post-ADOPT-11 canonical schema.

    Includes the ADOPT-11 columns (approved_at, rejection_reason, rejection_note)
    so apply_to_db can write them without an ALTER TABLE round-trip.
    """
    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        company TEXT NOT NULL,
        location TEXT,
        url TEXT,
        career_url TEXT,
        description TEXT,
        score INTEGER DEFAULT 0,
        status TEXT DEFAULT 'new',
        approved_at TIMESTAMP,
        rejection_reason TEXT,
        rejection_note TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(title, company)
    );
    INSERT INTO jobs (title, company, location, url, score, status) VALUES
        ('Senior Product Manager', 'Acme Corp',   'Berlin',   'https://acme.test/1', 180, 'new'),
        ('CTO',                   'Beta Inc',    'Munich',   'https://beta.test/2', 220, 'new'),
        ('Junior Analyst',        'Gamma SE',    'Hamburg',  'https://gamma.test/3', 40,  'new'),
        ('Founder',               'Delta GmbH',  'Vienna',   'https://delta.test/4', 260, 'new'),
        ('DevOps Engineer',       'Epsilon AG',  'Brussels', 'https://eps.test/5',  150, 'new');
    """)
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def db_path(tmp_path):
    return _make_db(tmp_path)


# Mocked Sheets service: returns canned rows for any tab queried.
@pytest.fixture
def mock_svc():
    """A fake sheets service that returns two approved + two rejected rows.

    Mirrors the writer's 12-column schema (DEFAULT_HEADERS_EN) so the
    pull_* functions work via positional index.
    """
    svc = MagicMock()

    # The canned data — note the status column values test the alias map
    # (e.g. "Yes" vs "approved", "X" vs "rejected").
    raw_rows = [
        sa.DEFAULT_HEADERS_EN,  # header row
        ["2026-09-26", "arbeitsagentur", "Senior Product Manager", "Acme Corp",
         "Berlin", 180, "", "https://acme.test/1", "", "good role", "approved", ""],
        ["2026-09-26", "stepstone",      "CTO",                   "Beta Inc",
         "Munich", 220, "", "https://beta.test/2", "", "exec role",  "Yes",       ""],
        ["2026-09-26", "arbeitsagentur", "Junior Analyst",        "Gamma SE",
         "Hamburg", 40,  "", "https://gamma.test/3", "", "too junior","rejected",  "too_junior"],
        ["2026-09-26", "stepstone",      "Founder",               "Delta GmbH",
         "Vienna", 260, "", "https://delta.test/4", "", "exec role",  "X",         ""],
        ["2026-09-26", "arbeitsagentur", "DevOps Engineer",       "Epsilon AG",
         "Brussels", 150, "", "https://eps.test/5", "", "wrong loc",  "rejected",  "berlin_only"],
    ]

    # google_writer.get_rows -> svc.spreadsheets().values().get().execute()
    svc.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
        "values": raw_rows,
    }

    return svc


# ---------------------------------------------------------------------------
# Schema / constants
# ---------------------------------------------------------------------------

def test_sheet_schema_matches_writer():
    """The sync module's SHEET_SCHEMA headers must equal the writer's."""
    from src.sheet.google_writer import DEFAULT_HEADERS_EN
    assert sa.SHEET_SCHEMA["headers"] == DEFAULT_HEADERS_EN
    assert sa.SHEET_SCHEMA["status_column"] == "status"
    assert sa.SHEET_SCHEMA["rejection_reason_column"] == "rejection_reason"


def test_rejection_reasons_have_other_fallback():
    """The 'other' key must exist so unknown reasons have a safe home."""
    assert "other" in sa.REJECTION_REASONS
    assert "too_junior" in sa.REJECTION_REASONS
    assert "wrong_location" in sa.REJECTION_REASONS
    # All keys in REJECTION_REASONS must also be in VALID_REJECTION_KEYS.
    assert set(sa.REJECTION_REASONS) == set(sa.VALID_REJECTION_KEYS)


# ---------------------------------------------------------------------------
# Pull side
# ---------------------------------------------------------------------------

def test_pull_approvals_recognises_aliases(mock_svc):
    """Both 'approved' and 'Yes' should route into the approvals bucket."""
    approvals = sa.pull_approvals(mock_svc, "sheet-1", "2026-09-26")
    titles = sorted(r["title"] for r in approvals)
    assert titles == ["CTO", "Senior Product Manager"]


def test_pull_rejections_captures_reasons(mock_svc):
    """Both known enum keys and free text should land in rejections."""
    rejections = sa.pull_rejections(mock_svc, "sheet-1", "2026-09-26")
    assert len(rejections) == 3
    by_title = {r["title"]: r for r in rejections}
    assert by_title["Junior Analyst"]["rejection_reason"] == "too_junior"
    # Founder row has status="X" (alias for rejected) but no reason text.
    assert by_title["Founder"]["status"] == "x"
    assert by_title["Founder"]["rejection_reason"] == ""
    # Free-text reason (not in the enum) should pass through unchanged.
    assert by_title["DevOps Engineer"]["rejection_reason"] == "berlin_only"


def test_pull_skips_blank_status(mock_svc):
    """Rows with no status filled in are ignored — neither approved nor rejected."""
    # Add a blank row to the mock.
    mock_svc.spreadsheets.return_value.values.return_value.get.return_value \
        .execute.return_value["values"].append(
            ["2026-09-26", "stepstone", "Blank", "Blank Co",
             "Berlin", 100, "", "https://blank.test", "", "blank", "", ""]
        )
    approvals = sa.pull_approvals(mock_svc, "sheet-1", "2026-09-26")
    rejections = sa.pull_rejections(mock_svc, "sheet-1", "2026-09-26")
    assert "Blank" not in {r["title"] for r in approvals}
    assert "Blank" not in {r["title"] for r in rejections}


# ---------------------------------------------------------------------------
# Apply side — happy path
# ---------------------------------------------------------------------------

def test_apply_to_db_marks_approved_with_timestamp(db_path, mock_svc):
    approvals = sa.pull_approvals(mock_svc, "sheet-1", "2026-09-26")
    rejections = sa.pull_rejections(mock_svc, "sheet-1", "2026-09-26")
    fixed_now = datetime(2026, 9, 27, 9, 0, 0)

    result = sa.apply_to_db(approvals, rejections, str(db_path), now=fixed_now)

    assert result.approved_applied == 2
    assert result.rejected_applied == 3
    assert result.skipped_already_approved == 0
    assert result.errors == []
    assert result.total_applied == 5

    # Approved rows now have status + approved_at populated.
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    acme = conn.execute(
        "SELECT status, approved_at, rejection_reason FROM jobs WHERE company='Acme Corp'"
    ).fetchone()
    assert acme["status"] == "approved"
    assert acme["approved_at"] is not None
    assert acme["rejection_reason"] is None  # cleared on approval

    # Rejected rows have enum key (or 'other' for free text) + note.
    gamma = conn.execute(
        "SELECT status, rejection_reason, rejection_note FROM jobs WHERE company='Gamma SE'"
    ).fetchone()
    assert gamma["status"] == "rejected"
    assert gamma["rejection_reason"] == "too_junior"

    epsilon = conn.execute(
        "SELECT status, rejection_reason, rejection_note FROM jobs WHERE company='Epsilon AG'"
    ).fetchone()
    assert epsilon["status"] == "rejected"
    assert epsilon["rejection_reason"] == "other"   # free text fallback
    assert epsilon["rejection_note"] == "berlin_only"
    conn.close()


# ---------------------------------------------------------------------------
# Idempotency — the most important contract for cron safety
# ---------------------------------------------------------------------------

def test_apply_to_db_is_idempotent(db_path, mock_svc):
    """Second sync must not overwrite approved_at, and must not double-count."""
    approvals = sa.pull_approvals(mock_svc, "sheet-1", "2026-09-26")
    rejections = sa.pull_rejections(mock_svc, "sheet-1", "2026-09-26")
    fixed_now = datetime(2026, 9, 27, 9, 0, 0)

    first = sa.apply_to_db(approvals, rejections, str(db_path), now=fixed_now)
    assert first.approved_applied == 2
    assert first.rejected_applied == 3

    # Re-run with a DIFFERENT timestamp — must be a no-op for unchanged
    # decisions. approved_at must NOT change either.
    later = datetime(2026, 9, 27, 17, 30, 0)
    second = sa.apply_to_db(approvals, rejections, str(db_path), now=later)
    assert second.approved_applied == 0, "second run must not re-apply approvals"
    assert second.rejected_applied == 0, "second run must not re-apply rejections"
    # Counter is reused for "skipped because already in target state".
    assert second.skipped_already_approved == 5

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    acme = conn.execute(
        "SELECT approved_at FROM jobs WHERE company='Acme Corp'"
    ).fetchone()
    conn.close()
    # First sync's timestamp should still be there (NOT overwritten).
    assert acme["approved_at"].startswith("2026-09-27 09:00:00")


def test_apply_to_db_handles_unknown_rejection(db_path, mock_svc):
    """Garbage in the rejection_reason column is recorded as 'other' + verbatim,
    and surfaces in ``unknown_rejection_keys`` even when the DB lookup misses.

    The "Blank Two Co" row is intentionally NOT in the fixture DB — this lets
    us verify that the unknown-reason tracking fires BEFORE the no-match skip,
    which matters for analytics (you want to see the bogus reasons regardless
    of whether the SQLite row exists)."""
    # Patch the mock to add a row with bogus rejection reason.
    mock_svc.spreadsheets.return_value.values.return_value.get.return_value \
        .execute.return_value["values"].append(
            ["2026-09-26", "stepstone", "Blank Two", "Blank Two Co",
             "Berlin", 100, "", "https://b2.test", "", "weird", "rejected", "not-a-real-enum"]
        )
    rejections = sa.pull_rejections(mock_svc, "sheet-1", "2026-09-26")
    result = sa.apply_to_db([], rejections, str(db_path))

    # The bogus reason must be recorded even though we couldn't match a DB row.
    assert any(t[1] == "not-a-real-enum" for t in result.unknown_rejection_keys), (
        "unknown_rejection_keys should record the bogus value"
    )
    # And the no-match counter should reflect the skipped row.
    assert result.skipped_no_match == 1
    assert result.rejected_applied == 3  # the 3 that DID match


def test_apply_to_db_writes_other_enum_for_unknown_reason(db_path, mock_svc):
    """When the unknown-reason row DOES have a DB match, the SQLite write
    stores enum='other' with the verbatim text in rejection_note."""
    # Insert a matching DB row first.
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO jobs (title, company) VALUES ('Blank Two', 'Blank Two Co')"
    )
    conn.commit()
    conn.close()

    mock_svc.spreadsheets.return_value.values.return_value.get.return_value \
        .execute.return_value["values"].append(
            ["2026-09-26", "stepstone", "Blank Two", "Blank Two Co",
             "Berlin", 100, "", "https://b2.test", "", "weird", "rejected", "not-a-real-enum"]
        )
    rejections = sa.pull_rejections(mock_svc, "sheet-1", "2026-09-26")
    sa.apply_to_db([], rejections, str(db_path))

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT rejection_reason, rejection_note FROM jobs WHERE company='Blank Two Co'"
    ).fetchone()
    conn.close()
    assert row["rejection_reason"] == "other"
    assert row["rejection_note"] == "not-a-real-enum"


def test_apply_to_db_skips_unmatched_rows(db_path, mock_svc):
    """Sheet rows with no DB counterpart are silently skipped, not errors."""
    mock_svc.spreadsheets.return_value.values.return_value.get.return_value \
        .execute.return_value["values"].append(
            ["2026-09-26", "stepstone", "Ghost Job", "Ghost Co",
             "Nowhere", 999, "", "https://ghost.test", "", "ghost", "approved", ""]
        )
    approvals = sa.pull_approvals(mock_svc, "sheet-1", "2026-09-26")
    rejections = sa.pull_rejections(mock_svc, "sheet-1", "2026-09-26")
    result = sa.apply_to_db(approvals, rejections, str(db_path))

    assert result.approved_applied == 2  # the two we already had
    assert result.skipped_no_match == 1
    assert result.errors == []  # not an error — just informational


def test_apply_to_db_missing_db_returns_error(mock_svc):
    """Calling on a missing DB path returns a SyncResult with errors set."""
    approvals = sa.pull_approvals(mock_svc, "sheet-1", "2026-09-26")
    rejections = sa.pull_rejections(mock_svc, "sheet-1", "2026-09-26")
    result = sa.apply_to_db(approvals, rejections, "/no/such/path/jobs.db")

    assert len(result.errors) >= 1
    assert "DB not found" in result.errors[0]


# ---------------------------------------------------------------------------
# SyncResult contract
# ---------------------------------------------------------------------------

def test_sync_result_is_json_serializable(db_path, mock_svc):
    """The wrapper pipes SyncResult through to Discord — JSON must round-trip."""
    approvals = sa.pull_approvals(mock_svc, "sheet-1", "2026-09-26")
    rejections = sa.pull_rejections(mock_svc, "sheet-1", "2026-09-26")
    result = sa.apply_to_db(approvals, rejections, str(db_path))

    payload = result.to_json()
    roundtrip = json.loads(payload)
    assert roundtrip["tab_name"] == "(in-memory)"  # in-memory call has no tab
    assert roundtrip["approved_applied"] == 2
    assert roundtrip["rejected_applied"] == 3
    assert isinstance(roundtrip["errors"], list)


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------

def test_cli_dry_run_with_mock(monkeypatch, capsys, mock_svc):
    """`python -m src.sheet.sync_approvals --dry-run TAB` prints counts."""
    monkeypatch.setenv("GOOGLE_SPREADSHEET_ID", "fake-sheet-id")
    # google_writer.get_service returns the service directly (no kwarg).
    monkeypatch.setattr(sa, "get_service", lambda *a, **kw: mock_svc)

    rc = sa.main(["2026-09-26", "--dry-run"])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert rc == 0
    assert parsed["approvals"] == 2
    assert parsed["rejections"] == 3
    assert "Senior Product Manager" in parsed["approval_examples"][0] or \
           any("Senior Product Manager" in s for s in parsed["approval_examples"])


def test_cli_missing_spreadsheet_id(monkeypatch, capsys):
    """No GOOGLE_SPREADSHEET_ID -> SyncResult with error, exit 2."""
    monkeypatch.delenv("GOOGLE_SPREADSHEET_ID", raising=False)
    # Mock get_service to raise so the dry-run path returns an error
    # JSON instead of trying to hit the real Sheets API.
    def _raise(*a, **kw):
        raise RuntimeError("SA file not found at /no/sa.json")
    monkeypatch.setattr(sa, "get_service", _raise)

    rc = sa.main(["2026-09-26", "--dry-run"])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert rc == 2
    assert "not found" in parsed["error"] or "unavailable" in parsed["error"]
