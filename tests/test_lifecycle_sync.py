#!/usr/bin/env python3
"""
MAIL-2 — sync_lifecycle tests (DB → Sheet).

Covers the four contracts of ``src.sheet.google_writer.sync_lifecycle``:

  1. Match on (title, company): only jobs that have a Sheet row get
     their lifecycle cells patched. Unmatched jobs are logged, not fatal.
  2. Per-cell diff idempotency: re-running with unchanged DB state is a
     no-op (zero cells patched). Per-column diff means a single cell
     update still counts as "1 patch".
  3. Only M→S cells are written. A→L is never touched.
  4. The lifecycle cells come out in the right column order
     (mail_received_at, last_email_subject, ..., cv_version).

Run:
    .venv/bin/python -m pytest tests/test_lifecycle_sync.py -v
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.sheet.google_writer import (  # noqa: E402
    DEFAULT_HEADERS_EN,
    LIFECYCLE_DB_COLUMNS,
    _SHEET_KEY_INDICES,
    _col_idx_to_letter,
    lifecycle_cells_from_job,
    sync_lifecycle,
)


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────

def _make_test_db(path: Path) -> None:
    """A SQLite DB with the MAIL-2 columns + a few sample jobs."""
    schema_path = Path(__file__).resolve().parent.parent / "src" / "db" / "schema.sql"
    schema = schema_path.read_text(encoding="utf-8")
    conn = sqlite3.connect(str(path))
    conn.executescript(schema)
    conn.executescript("""
        INSERT INTO jobs (
            title, company, location, score, status,
            outcome, mail_received_at, last_email_subject,
            last_email_at, interview_at, salary_range, cv_version,
            applied_at
        ) VALUES
            ('CTO',          'Acme AG',     'Berlin',  180, 'new',
                'received',  '2026-09-26 10:00:00', 'Thanks for applying',
                '2026-09-26 10:05:00', NULL,         NULL, NULL,
                '2026-09-25 12:00:00'),
            ('Founder',      'Beta GmbH',   'Munich',  220, 'applied',
                'interview', '2026-09-26 09:00:00', 'Phone screen invite',
                '2026-09-26 09:30:00', '2026-09-30 14:00:00', '€120-150k',
                'v3-hosting-cto-2026-09-25', '2026-09-24 09:00:00'),
            ('DevOps Eng',   'Gamma Inc',   'Hamburg', 50,  'new',
                NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL),
            ('Junior Anal.', 'Delta SE',    'Vienna',  40,  'rejected',
                'rejected',  '2026-09-26 11:00:00', 'We went with another',
                '2026-09-26 11:00:00', NULL,         NULL, NULL,
                '2026-09-23 09:00:00');
    """)
    conn.commit()
    conn.close()


def _make_sheet_data(rows: list[tuple[str, str]] | None = None) -> list[list[str]]:
    """Build a fake Sheets tab as list-of-lists.

    Default = 4 jobs in the canonical order. ``rows`` lets the test
    pass (title, company) tuples to build a custom subset/order.
    """
    if rows is None:
        rows = [
            ("CTO",          "Acme AG"),
            ("Founder",      "Beta GmbH"),
            ("DevOps Eng",   "Gamma Inc"),
            ("Junior Anal.", "Delta SE"),
        ]
    ncols = len(DEFAULT_HEADERS_EN)
    header = list(DEFAULT_HEADERS_EN)
    data = [header]
    for title, company in rows:
        # Fill A:L from canonical order; lifecycle columns default blank.
        row = [""] * ncols
        row[DEFAULT_HEADERS_EN.index("title")] = title
        row[DEFAULT_HEADERS_EN.index("company")] = company
        data.append(row)
    return data


class _FakeSheetSvc:
    """A MagicMock-shaped Sheets service.

    Records values.get, values.batchUpdate, spreadsheets.get, and
    spreadsheets.batchUpdate. ``existing_data`` seeds the tab read.
    """

    def __init__(self, existing_data: list[list[str]]):
        self.calls: list[tuple[str, tuple, dict]] = []
        self.existing_data = existing_data
        self._next_sheet_id = 1000

        # Wire child mocks. googleapiclient Resource chain:
        # svc.spreadsheets().values().get(spreadsheetId=, range=).execute()
        # svc.spreadsheets().values().batchUpdate(...).execute()
        # svc.spreadsheets().get(spreadsheetId=).execute()
        # svc.spreadsheets().batchUpdate(...).execute()
        #
        # Strategy: build one MagicMock per chain with the canned
        # execute() response, then replace the chain entry point with a
        # recording wrapper that records the call AND forwards to the
        # original chain method. self.calls ends up populated, the
        # canned responses still flow through.

        # values.get() / values.batchUpdate() — canned tab read + write
        values = MagicMock()
        values.get.return_value.execute.return_value = {"values": existing_data}
        values.batchUpdate.return_value.execute.return_value = {"replies": [{}]}

        # spreadsheets.get() / batchUpdate() — canned tab list + batchUpdate
        spreadsheets = MagicMock()
        spreadsheets.get.return_value.execute.return_value = {
            "sheets": [
                {"properties": {
                    "title": existing_data[0][DEFAULT_HEADERS_EN.index("date_added")] if existing_data else "tab",
                    "sheetId": 42,
                    "index": 0,
                }}
            ],
        }
        spreadsheets.batchUpdate.return_value.execute.return_value = {"replies": [{}]}
        spreadsheets.values.return_value = values

        def _wrap(method, op_name):
            """Return a callable that records the call and forwards to method."""
            def _impl(*a, **kw):
                self.calls.append((op_name, a, kw))
                return method(*a, **kw)
            return _impl

        # Rebuild the chain entry points with recording wrappers.
        self._values = MagicMock()
        self._values.get.side_effect = _wrap(values.get, "values.get")
        self._values.batchUpdate.side_effect = _wrap(values.batchUpdate, "values.batchUpdate")

        self._spreadsheets = MagicMock()
        self._spreadsheets.get.side_effect = _wrap(spreadsheets.get, "spreadsheets.get")
        self._spreadsheets.batchUpdate.side_effect = _wrap(spreadsheets.batchUpdate, "spreadsheets.batchUpdate")
        self._spreadsheets.values.return_value = self._values

    def spreadsheets(self):
        return self._spreadsheets


@pytest.fixture
def test_db(tmp_path: Path) -> Path:
    db = tmp_path / "jobs.db"
    _make_test_db(db)
    return db


@pytest.fixture
def svc() -> _FakeSheetSvc:
    return _FakeSheetSvc(_make_sheet_data())


def _calls_named(rec, op: str) -> list[tuple]:
    """All recorded calls to a given op name on the fake service."""
    return [c for c in rec.calls if c[0] == op]


# ─────────────────────────────────────────────────────────────────────
# Lifecycle cells — helper correctness
# ─────────────────────────────────────────────────────────────────────

def test_lifecycle_cells_from_job_returns_7_cells_in_order():
    """The 7 cells must come out in the same order as DEFAULT_HEADERS_EN[12:19]."""
    job = {
        "mail_received_at": "2026-09-26 10:00:00",
        "last_email_subject": "Phone screen invite" * 10,  # way over the 80-char cap
        "last_email_at": "2026-09-26 09:30:00",
        "interview_at": "2026-09-30 14:00:00",
        "outcome": "interview",
        "salary_range": "€120-150k",
        "cv_version": "v3-hosting-cto-2026-09-25",
    }
    cells = lifecycle_cells_from_job(job)
    assert len(cells) == 7
    assert cells[0] == "2026-09-26 10:00:00"  # mail_received_at
    # Subject truncated to 80 chars
    assert len(cells[1]) <= 80
    assert cells[1].endswith("...")
    assert cells[2] == "2026-09-26 09:30:00"  # last_email_at
    assert cells[3] == "2026-09-30 14:00:00"  # interview_at
    assert cells[4] == "interview"            # outcome
    assert cells[5] == "€120-150k"            # salary_range
    assert cells[6] == "v3-hosting-cto-2026-09-25"  # cv_version


def test_lifecycle_cells_from_job_none_values_become_empty():
    job = {
        "mail_received_at": None,
        "last_email_subject": None,
        "last_email_at": None,
        "interview_at": None,
        "outcome": None,
        "salary_range": None,
        "cv_version": None,
    }
    cells = lifecycle_cells_from_job(job)
    assert cells == ["", "", "", "", "", "", ""]


def test_lifecycle_cells_from_job_unknown_outcome_passes_through():
    """Defensive: a typo from the watcher still gets emitted (Sheets UI will reject on edit)."""
    cells = lifecycle_cells_from_job({"outcome": "invited_to_phone_screen"})
    assert cells[4] == "invited_to_phone_screen"


def test_lifecycle_cells_order_matches_headers():
    """The order of cells from lifecycle_cells_from_job must match the
    order of lifecycle columns in DEFAULT_HEADERS_EN[12:19]."""
    expected = (
        "mail_received_at",
        "last_email_subject",
        "last_email_at",
        "interview_at",
        "outcome",
        "salary_range",
        "cv_version",
    )
    assert DEFAULT_HEADERS_EN[12:19] == list(expected)


def test_lifecycle_db_columns_match():
    """sync_lifecycle relies on this tuple for the SELECT projection."""
    expected = (
        "id", "title", "company",
        "mail_received_at", "last_email_subject", "last_email_at",
        "interview_at", "outcome", "salary_range", "cv_version",
    )
    assert LIFECYCLE_DB_COLUMNS == expected


# ─────────────────────────────────────────────────────────────────────
# sync_lifecycle — happy path
# ─────────────────────────────────────────────────────────────────────

def test_sync_lifecycle_patches_lifecycle_columns(test_db, svc):
    """All 3 jobs with lifecycle state get their M→S cells patched.

    Gamma Inc has no lifecycle signal (NULL outcome, NULL mail, NULL
    applied_at) and is filtered out by the SELECT — that's the spec.
    """
    summary = sync_lifecycle(svc, "fake", "2026-09-28", db_path=str(test_db))

    assert summary["matched"] == 3   # Acme, Beta, Delta — Gamma has no state
    assert summary["unmatched"] == 0
    assert summary["patched"] > 0

    # Exactly one batchUpdate call (batched for API efficiency)
    bu_calls = _calls_named(svc, "values.batchUpdate")
    assert len(bu_calls) == 1

    # The body covers 3 rows × 7 cells = 21 cells worth of data.
    body = bu_calls[0][2]["body"]
    data = body["data"]
    assert len(data) == 3
    for entry in data:
        assert entry["values"][0]  # non-empty list of cells


def test_sync_lifecycle_writes_only_ms_columns(test_db, svc):
    """A: L: must NEVER appear in the patch (would clobber Lars's edits)."""
    sync_lifecycle(svc, "fake", "2026-09-28", db_path=str(test_db))

    bu_calls = _calls_named(svc, "values.batchUpdate")
    assert len(bu_calls) == 1
    body = bu_calls[0][2]["body"]

    for entry in body["data"]:
        a1_range = entry["range"]
        # Range must start at column M and end at column S.
        sheet_name, rng = a1_range.split("!", 1)
        start_col = rng.split(":")[0].rstrip("0123456789")
        end_col = rng.split(":")[1].rstrip("0123456789")
        assert start_col == "M", f"expected start column M, got {start_col!r}"
        assert end_col == "S", f"expected end column S, got {end_col!r}"


def test_sync_lifecycle_pulls_only_jobs_with_lifecycle_state(test_db, svc):
    """Jobs with no lifecycle signal (NULL outcome + NULL mail + NULL applied)
    are excluded from the patch set."""
    # Insert a "blank" job that should NOT appear in the patch.
    conn = sqlite3.connect(str(test_db))
    conn.execute("""
        INSERT INTO jobs (title, company, score, status)
        VALUES ('Blank', 'Epsilon Ltd', 75, 'new')
    """)
    conn.commit()
    conn.close()

    # Sheet only knows about the 4 original jobs (no Epsilon).
    summary = sync_lifecycle(svc, "fake", "2026-09-28", db_path=str(test_db))

    # 3 matched (Acme/Beta/Delta — Gamma excluded by WHERE filter).
    assert summary["matched"] == 3
    # The blank job's WHERE clause filter: no lifecycle state, so it
    # shouldn't even be SELECTed. Result: 0 unmatched.
    assert summary["unmatched"] == 0


def test_sync_lifecycle_reports_unmatched_jobs_without_crashing(test_db, svc):
    """A DB row whose (title, company) isn't in the Sheet is logged, not fatal."""
    conn = sqlite3.connect(str(test_db))
    conn.execute("""
        INSERT INTO jobs (title, company, outcome)
        VALUES ('Phantom', 'NoSheet AG', 'received')
    """)
    conn.commit()
    conn.close()

    summary = sync_lifecycle(svc, "fake", "2026-09-28", db_path=str(test_db))

    assert summary["matched"] == 3
    assert summary["unmatched"] == 1
    # The patch set is still the 3 matched rows; Phantom is logged.
    bu_calls = _calls_named(svc, "values.batchUpdate")
    assert len(bu_calls) == 1


# ─────────────────────────────────────────────────────────────────────
# sync_lifecycle — idempotency
# ─────────────────────────────────────────────────────────────────────

def test_sync_lifecycle_is_idempotent_on_second_run(test_db, svc):
    """Second run with unchanged DB state = zero cells patched."""
    # First run patches everything.
    s1 = sync_lifecycle(svc, "fake", "2026-09-28", db_path=str(test_db))
    assert s1["patched"] > 0

    # Second run: the fake service still serves the OLD pre-patch data
    # (because our fake doesn't actually mutate its own state). The
    # function should still issue a batchUpdate with the same cells
    # and the patched count is non-zero on the second run, because the
    # Sheet state hasn't been updated to match the DB.
    #
    # The CONTRACT being tested here is: a second call doesn't crash
    # and doesn't lose data. The "no-op on second call" property is
    # tested separately below by pre-seeding the Sheet with DB state.
    s2 = sync_lifecycle(svc, "fake", "2026-09-28", db_path=str(test_db))
    assert s2["matched"] == s1["matched"]
    assert s2["unmatched"] == s1["unmatched"]


def test_sync_lifecycle_no_op_when_sheet_already_matches(test_db):
    """If the Sheet already reflects the DB state, sync_lifecycle is a
    no-op (zero cells patched)."""
    # Build a Sheet that already has the lifecycle values from the DB.
    conn = sqlite3.connect(str(test_db))
    conn.row_factory = sqlite3.Row
    db_rows = conn.execute(
        f"SELECT {', '.join(LIFECYCLE_DB_COLUMNS)} FROM jobs"
    ).fetchall()
    conn.close()

    ncols = len(DEFAULT_HEADERS_EN)
    header = list(DEFAULT_HEADERS_EN)
    data = [header]
    for r in db_rows:
        row = [""] * ncols
        row[DEFAULT_HEADERS_EN.index("title")] = r["title"]
        row[DEFAULT_HEADERS_EN.index("company")] = r["company"]
        # Fill M→S from the DB so the diff is empty.
        from src.sheet.google_writer import lifecycle_cells_from_job
        cells = lifecycle_cells_from_job(dict(r))
        for offset, col_idx in enumerate([
            DEFAULT_HEADERS_EN.index(c) for c in
            ["mail_received_at", "last_email_subject", "last_email_at",
             "interview_at", "outcome", "salary_range", "cv_version"]
        ]):
            row[col_idx] = cells[offset] or ""
        data.append(row)

    svc = _FakeSheetSvc(data)
    summary = sync_lifecycle(svc, "fake", "2026-09-28", db_path=str(test_db))

    # matched + unmatched still count, but patched = 0.
    assert summary["patched"] == 0
    assert summary["matched"] == 3
    # No batchUpdate call when there's nothing to write.
    assert _calls_named(svc, "values.batchUpdate") == []


# ─────────────────────────────────────────────────────────────────────
# Edge cases
# ─────────────────────────────────────────────────────────────────────

def test_sync_lifecycle_handles_empty_sheet(test_db):
    """Empty tab → no patch needed, function returns gracefully."""
    svc = _FakeSheetSvc([list(DEFAULT_HEADERS_EN)])  # header only, no data
    summary = sync_lifecycle(svc, "fake", "2026-09-28", db_path=str(test_db))
    assert summary["matched"] == 0
    assert summary["patched"] == 0


def test_sync_lifecycle_handles_completely_empty_tab(test_db):
    """Missing tab (no rows at all) → empty list, no crash."""
    svc = _FakeSheetSvc([])
    summary = sync_lifecycle(svc, "fake", "2026-09-28", db_path=str(test_db))
    assert summary["matched"] == 0


def test_sync_lifecycle_requires_db_path_or_conn(test_db, svc):
    """No conn + no db_path → ValueError."""
    with pytest.raises(ValueError, match="conn or db_path"):
        sync_lifecycle(svc, "fake", "2026-09-28")


def test_sync_lifecycle_uses_pre_opened_connection(test_db, svc):
    """A pre-opened connection is used (and not closed) by sync_lifecycle."""
    conn = sqlite3.connect(str(test_db))
    conn.row_factory = sqlite3.Row
    try:
        summary = sync_lifecycle(svc, "fake", "2026-09-28", conn=conn)
        assert summary["matched"] == 3
        # Connection is still usable after the call (function didn't close it).
        count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert count == 4
    finally:
        conn.close()


def test_sync_lifecycle_case_insensitive_title_company(test_db, svc):
    """(title, company) match is case-insensitive — Sheet vs DB drift."""
    # Sheet has lowercase variant of one row.
    mixed = [
        list(DEFAULT_HEADERS_EN),
        # row 2: lowercase variant of "Acme AG" row
        *([([""] * len(DEFAULT_HEADERS_EN))] * 1),
        *([([""] * len(DEFAULT_HEADERS_EN))] * 3),
    ]
    mixed[1][DEFAULT_HEADERS_EN.index("title")] = "cto"
    mixed[1][DEFAULT_HEADERS_EN.index("company")] = "acme ag"
    svc = _FakeSheetSvc(mixed)

    summary = sync_lifecycle(svc, "fake", "2026-09-28", db_path=str(test_db))

    # The lowercase variant should still match the "CTO"/"Acme AG" DB row.
    assert summary["matched"] == 1


# ─────────────────────────────────────────────────────────────────────
# Helper: _col_idx_to_letter — sanity
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("idx,expected", [
    (1, "A"), (13, "M"), (17, "Q"), (19, "S"), (26, "Z"), (27, "AA"),
    (52, "AZ"), (53, "BA"), (702, "ZZ"), (703, "AAA"),
])
def test_col_idx_to_letter(idx, expected):
    assert _col_idx_to_letter(idx) == expected