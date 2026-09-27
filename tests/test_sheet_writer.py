#!/usr/bin/env python3
"""
ADOPT-10 — Google Sheet writer tests.

Mock the Sheets API so these run offline (no creds, no quota). Smoke
the row schema, idempotency, description truncation, and the auth-error
path that bubbles up if the SA JSON is missing.

Run:
    .venv/bin/python -m pytest tests/test_sheet_writer.py -v
    .venv/bin/python -m pytest tests/test_sheet_writer.py -v -k "schema"
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Make `src.*` importable when pytest runs from the repo root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.sheet import google_writer as gw
from src.sheet import (
    DEFAULT_HEADERS_EN,
    create_daily_tab,
    append_jobs,
    list_tabs,
    get_rows,
    get_service,
    row_from_job,
)


# ───────────────────────────────────────────────────────────────────────
# A disciplined fake Sheets service
# ───────────────────────────────────────────────────────────────────────
#
# MagicMock auto-chains break the moment you assert on call counts, because
# every attribute access is itself a "call" on the parent mock. Instead we
# build a hand-rolled recorder that records actual method invocations and
# returns canned responses. The point of these tests is to prove the row
# schema + idempotency + truncation — not the URL grammar.

class _Recorder:
    """Records every method call made on the fake service."""

    def __init__(self):
        self.calls: list = []      # (op_name, args_tuple, kwargs_dict)
        self.tabs: list = []       # tab names that "exist"
        self._next_sheet_id = 1000
        # Stash the inner objects so tests can monkey-patch methods on them
        # (e.g. inject a clear() that raises). Returning fresh instances per
        # `.spreadsheets()` call would lose the patch on the next access.
        self._spreadsheets = _Spreadsheets(self)
        self._values = _Values(self)

    def spreadsheets(self):
        # The real googleapiclient Resource has `.spreadsheets()` as a
        # method — match that call shape so production code paths exercise
        # our fake unmodified.
        return self._spreadsheets


class _Spreadsheets:
    def __init__(self, rec):
        self._r = rec

    def get(self, **kwargs):
        self._r.calls.append(("get", (), kwargs))
        return _GetResp(self._r)

    def batchUpdate(self, **kwargs):
        self._r.calls.append(("batchUpdate", (), kwargs))
        return _BatchUpdateResp(self._r)

    def values(self):
        # googleapiclient exposes `.values()` as a method on the inner
        # `spreadsheets` resource. Match that call shape.
        return self._r._values


class _GetResp:
    def __init__(self, rec):
        self._r = rec

    def execute(self):
        sheets = [
            {"properties": {"title": t, "sheetId": 2000 + i, "index": i}}
            for i, t in enumerate(self._r.tabs)
        ]
        return {"sheets": sheets}


class _BatchUpdateResp:
    def __init__(self, rec):
        self._r = rec

    def execute(self):
        for c in reversed(self._r.calls):
            if c[0] == "batchUpdate":
                reqs = c[2].get("body", {}).get("requests", [])
                for req in reqs:
                    if "addSheet" in req:
                        title = req["addSheet"]["properties"]["title"]
                        self._r.tabs.append(title)
                        return {
                            "replies": [
                                {"addSheet": {"properties": {
                                    "title": title,
                                    "sheetId": self._r._next_sheet_id,
                                    "index": len(self._r.tabs) - 1,
                                }}}
                            ]
                        }
        raise AssertionError("batchUpdate had no addSheet request")


class _Values:
    def __init__(self, rec):
        self._r = rec

    def get(self, **kwargs):
        self._r.calls.append(("values.get", (), kwargs))
        return _ValuesGetResp(self._r)

    def update(self, **kwargs):
        self._r.calls.append(("values.update", (), kwargs))
        return _GenericResp(
            self._r,
            {"updatedRows": len(kwargs.get("body", {}).get("values", []))},
        )

    def clear(self, **kwargs):
        self._r.calls.append(("values.clear", (), kwargs))
        return _GenericResp(self._r, {})


class _ValuesGetResp:
    def __init__(self, rec):
        self._r = rec

    def execute(self):
        # We don't simulate row contents — tests that need real values
        # use a real DB roundtrip instead.
        return {"values": []}


class _GenericResp:
    def __init__(self, rec, payload):
        self._r = rec
        self._p = payload

    def execute(self):
        return self._p


def _fake_service(initial_tabs=None):
    """Build a fake service that records every call."""
    rec = _Recorder()
    rec.tabs = list(initial_tabs or [])
    return rec


def _calls_named(rec, name):
    """Return all (op, args, kwargs) tuples for the given operation name."""
    return [c for c in rec.calls if c[0] == name]


def _sample_jobs():
    return [
        {
            "source": "arbeitsagentur",
            "title": "Founder & CTO",
            "company": "Bold & Digital",
            "location": "Berlin (remote DACH)",
            "score": 165,
            "llm_score": 92,
            "url": "https://example.com/jobs/1",
            "career_url": "https://example.com/careers/1",
            "description": "Lead engineering at a fast-growing startup.",
        },
        {
            "source": "stepstone",
            "title": "VP Engineering",
            "company": "Acme GmbH",
            "location": "Munich",
            "score": 140,
            "llm_score": 85,
            "url": "https://example.com/jobs/2",
            "career_url": "",
            "description": "Own the engineering org. Hire, ship, scale.",
        },
        {
            "source": "jobspy",
            "title": "Head of Product",
            "company": "FinTech AG",
            "location": "Remote EU",
            "score": 100,
            "llm_score": None,
            "url": "https://example.com/jobs/3",
            "career_url": "",
            "description": "x" * 1200,  # way over the 500-char cap
        },
    ]


# ───────────────────────────────────────────────────────────────────────
# row_from_job — schema
# ───────────────────────────────────────────────────────────────────────

class TestRowFromJob:
    def test_returns_12_columns_in_canonical_order(self):
        row = row_from_job({"title": "x", "company": "y"}, date_str="2026-09-28")
        assert len(row) == 12
        assert row[0] == "2026-09-28"
        assert row[1] == ""            # source missing → blank
        assert row[3] == "y"           # company
        assert row[10] == ""          # status — Lars edits
        assert row[11] == ""          # rejection_reason — Lars edits

    def test_default_headers_match_row_order(self):
        """Headers in create_daily_tab are 1:1 with row_from_job columns."""
        row = row_from_job({"title": "x", "company": "y"}, date_str="2026-09-28")
        assert len(DEFAULT_HEADERS_EN) == len(row) == 12
        assert DEFAULT_HEADERS_EN == [
            "date_added", "source", "title", "company", "location", "score",
            "llm_score", "url", "career_url", "description", "status",
            "rejection_reason",
        ]

    def test_description_truncated_at_500_with_ellipsis(self):
        long_desc = "y" * 1200
        row = row_from_job({"description": long_desc}, date_str="2026-09-28")
        assert len(row[9]) == 500
        assert row[9].endswith("...")
        assert row[9].count("y") == 497

    def test_short_description_not_truncated(self):
        row = row_from_job({"description": "short"}, date_str="2026-09-28")
        assert row[9] == "short"

    def test_score_and_llm_score_blanks_when_missing(self):
        row = row_from_job({"title": "x"}, date_str="2026-09-28")
        assert row[5] == ""
        assert row[6] == ""

    def test_uses_today_when_no_date_str(self):
        row = row_from_job({"title": "x"})
        from datetime import datetime, timezone
        assert row[0] == datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ───────────────────────────────────────────────────────────────────────
# create_daily_tab — header writing + idempotent tab reuse
# ───────────────────────────────────────────────────────────────────────

class TestCreateDailyTab:
    def test_writes_headers_to_A1(self):
        rec = _fake_service()
        create_daily_tab(rec, "fake_sheet_id", "2026-09-28")

        updates = _calls_named(rec, "values.update")
        assert len(updates) == 1
        kwargs = updates[0][2]
        assert kwargs["spreadsheetId"] == "fake_sheet_id"
        assert kwargs["range"] == "2026-09-28!A1"
        assert kwargs["body"]["values"] == [DEFAULT_HEADERS_EN]
        assert kwargs["valueInputOption"] == "RAW"

    def test_creates_tab_when_missing(self):
        rec = _fake_service()
        create_daily_tab(rec, "fake_sheet_id", "2026-09-28")

        batches = _calls_named(rec, "batchUpdate")
        assert len(batches) == 1
        body = batches[0][2]["body"]
        assert body["requests"][0]["addSheet"]["properties"]["title"] == "2026-09-28"

    def test_reuses_existing_tab_without_creating(self):
        rec = _fake_service(initial_tabs=["2026-09-28"])

        create_daily_tab(rec, "fake_sheet_id", "2026-09-28")

        assert _calls_named(rec, "batchUpdate") == []
        assert len(_calls_named(rec, "values.update")) == 1

    def test_empty_headers_skips_update(self):
        rec = _fake_service()
        create_daily_tab(rec, "fake_sheet_id", "2026-09-28", headers=[])
        assert _calls_named(rec, "values.update") == []


# ───────────────────────────────────────────────────────────────────────
# append_jobs — idempotent wipe + write
# ───────────────────────────────────────────────────────────────────────

class TestAppendJobs:
    def test_empty_jobs_does_not_clear_or_write(self):
        rec = _fake_service()
        resp = append_jobs(rec, "fake", "2026-09-28", [])
        assert _calls_named(rec, "values.clear") == []
        assert _calls_named(rec, "values.update") == []
        assert resp["updatedRows"] == 0

    def test_clears_then_writes(self):
        rec = _fake_service()
        append_jobs(rec, "fake", "2026-09-28", _sample_jobs(), date_str="2026-09-28")

        clears = _calls_named(rec, "values.clear")
        updates = _calls_named(rec, "values.update")
        assert len(clears) == 1
        assert len(updates) == 1

        clear_kwargs = clears[0][2]
        assert clear_kwargs["range"] == "2026-09-28!A1:L5000"

        update_kwargs = updates[0][2]
        assert update_kwargs["range"] == "2026-09-28!A2"
        assert update_kwargs["valueInputOption"] == "USER_ENTERED"

    def test_writes_one_row_per_job(self):
        rec = _fake_service()
        append_jobs(rec, "fake", "2026-09-28", _sample_jobs(), date_str="2026-09-28")
        rows = _calls_named(rec, "values.update")[0][2]["body"]["values"]
        assert len(rows) == 3
        assert all(len(r) == 12 for r in rows)

    def test_long_description_in_jobs_is_truncated(self):
        rec = _fake_service()
        append_jobs(rec, "fake", "2026-09-28", _sample_jobs(), date_str="2026-09-28")
        rows = _calls_named(rec, "values.update")[0][2]["body"]["values"]
        assert len(rows[2][9]) == 500
        assert rows[2][9].endswith("...")

    def test_clear_failure_does_not_crash(self):
        """An empty tab can refuse clear() — we log + continue, not crash."""
        from googleapiclient.errors import HttpError

        rec = _fake_service()

        def bad_clear(**kwargs):
            rec.calls.append(("values.clear", (), kwargs))
            err = MagicMock()
            err.status = 400
            raise HttpError(resp=err, content=b'{"error":{"message":"bad range"}}')

        rec._values.clear = bad_clear  # type: ignore[attr-defined]

        # Should not raise
        append_jobs(rec, "fake", "2026-09-28", _sample_jobs(), date_str="2026-09-28")
        assert len(_calls_named(rec, "values.update")) == 1


# ───────────────────────────────────────────────────────────────────────
# list_tabs + get_rows — thin wrappers but worth pinning behavior
# ───────────────────────────────────────────────────────────────────────

class TestListTabs:
    def test_returns_tab_props(self):
        rec = _fake_service(initial_tabs=["2026-09-27", "Summary"])
        tabs = list_tabs(rec, "fake")
        assert len(tabs) == 2
        assert tabs[0]["name"] == "2026-09-27"
        assert tabs[1]["name"] == "Summary"
        assert tabs[0]["sheetId"] != tabs[1]["sheetId"]

    def test_empty_sheet_returns_empty_list(self):
        rec = _fake_service()
        assert list_tabs(rec, "fake") == []


class TestGetRows:
    def test_passes_range_to_api(self):
        rec = _fake_service()
        get_rows(rec, "fake", "2026-09-28", a1_range="2026-09-28!A1:L5")
        gets = _calls_named(rec, "values.get")
        assert len(gets) == 1
        kwargs = gets[0][2]
        assert kwargs["spreadsheetId"] == "fake"
        assert kwargs["range"] == "2026-09-28!A1:L5"

    def test_returns_values_defaulting_to_empty(self):
        rec = _fake_service()
        assert get_rows(rec, "fake", "2026-09-28") == []


# ───────────────────────────────────────────────────────────────────────
# get_service — auth & error path
# ───────────────────────────────────────────────────────────────────────

def _valid_sa_payload():
    # We don't actually validate the private_key — patch
    # service_account.Credentials.from_service_account_file in tests that
    # exercise get_service(). This keeps the SA file realistic-looking
    # without the overhead of generating a real RSA key per test run.
    return {
        "type": "service_account",
        "client_email": "x@y.iam.gserviceaccount.com",
        "private_key": "PEM_PLACEHOLDER",
        "token_uri": "https://oauth2.googleapis.com/token",
    }


class TestGetService:
    def test_raises_if_sa_file_missing(self, tmp_path):
        with patch.dict(os.environ, {"GOOGLE_APPLICATION_CREDENTIALS": str(tmp_path / "nope.json")}):
            with pytest.raises(FileNotFoundError, match="Service account JSON not found"):
                get_service()

    def test_uses_default_path_when_env_unset(self, tmp_path):
        """With env cleared, get_service falls back to DEFAULT_SA_PATH and
        hands the file to google.oauth2. We patch `build` so no real TLS."""
        sa = tmp_path / "sa.json"
        sa.write_text(json.dumps(_valid_sa_payload()))

        with patch.dict(os.environ, {}, clear=True):
            with patch("src.sheet.google_writer.DEFAULT_SA_PATH", str(sa)):
                # Skip the real PEM validation
                with patch(
                    "src.sheet.google_writer.service_account.Credentials.from_service_account_file",
                    return_value=MagicMock(),
                ):
                    with patch("src.sheet.google_writer.build") as build_mock:
                        build_mock.return_value = "fake-service"
                        svc = get_service()
                        assert svc == "fake-service"
                        build_mock.assert_called_once()
                        kwargs = build_mock.call_args.kwargs
                        assert "credentials" in kwargs

    def test_caches_service_across_calls(self, tmp_path):
        sa = tmp_path / "sa.json"
        sa.write_text(json.dumps(_valid_sa_payload()))

        with patch.dict(os.environ, {"GOOGLE_APPLICATION_CREDENTIALS": str(sa)}):
            with patch(
                "src.sheet.google_writer.service_account.Credentials.from_service_account_file",
                return_value=MagicMock(),
            ):
                with patch("src.sheet.google_writer.build") as build_mock:
                    build_mock.return_value = "cached-svc"
                    s1 = get_service()
                    s2 = get_service()
                    assert s1 is s2
                    assert build_mock.call_count == 1


def test_module_reexports_public_api():
    """`from src.sheet import X` works for everything in __init__."""
    import src.sheet as sheet_pkg
    for name in [
        "SHEETS_SCOPE", "DEFAULT_HEADERS_EN", "get_service", "create_daily_tab",
        "append_jobs", "list_tabs", "get_rows", "row_from_job",
    ]:
        assert hasattr(sheet_pkg, name), f"src.sheet missing {name!r}"


# ───────────────────────────────────────────────────────────────────────
# _write_from_db — CLI mode used by lars-daily-run.sh --sheet
# ───────────────────────────────────────────────────────────────────────

class TestWriteFromDbCli:
    """Pins the contract lars-daily-run.sh relies on: --db + --tab + --sheet-id."""

    def _build_jobs_db(self, tmp_path) -> str:
        """Create a tiny SQLite DB matching the schema subset we read."""
        import sqlite3
        db = tmp_path / "jobs.db"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY,
                title TEXT, company TEXT, location TEXT,
                url TEXT, career_url TEXT, score INTEGER,
                source TEXT, description TEXT,
                llm_score INTEGER,
                status TEXT DEFAULT 'new'
            );
        """)
        # Columns (in order): id, title, company, location, url, career_url,
        # score, source, description, llm_score, status
        jobs = [
            (1, "Founder", "Acme", "Berlin", "https://a", "", 200,
             "arbeitsagentur", "desc1", 90, "new"),
            (2, "CTO",     "Beta", "Munich", "https://b", "", 150,
             "stepstone",      "desc2", 80, "new"),
            (3, "DevOps",  "Gamma", "Remote", "https://c", "", 25,
             "jobspy",         "desc3", None, "applied"),  # already applied
        ]
        conn.executemany(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)", jobs
        )
        conn.commit()
        conn.close()
        return str(db)

    def test_selects_jobs_above_min_score_and_skips_terminal_status(self, tmp_path):
        db = self._build_jobs_db(tmp_path)

        with patch("src.sheet.google_writer.get_service") as get_svc_mock:
            rec = _fake_service()
            get_svc_mock.return_value = rec

            with patch("src.sheet.google_writer.create_daily_tab") as cdt:
                with patch("src.sheet.google_writer.append_jobs") as aj:
                    rc = gw._write_from_db(
                        sheet_id="fake_sheet",
                        db_path=db,
                        tab_name="2026-09-28",
                        limit=50,
                        min_score=20,
                    )

        assert rc == 0
        cdt.assert_called_once()
        # aj receives the selected job dicts
        aj.assert_called_once()
        kwargs = aj.call_args.kwargs
        passed_jobs = aj.call_args.args[3]  # positional svc, sheet_id, tab, jobs
        # Jobs 1 + 2 qualify (score>=20, status not applied/filtered/batched/skipped)
        assert len(passed_jobs) == 2
        titles = sorted(j["title"] for j in passed_jobs)
        assert titles == ["CTO", "Founder"]
        assert passed_jobs[0]["score"] >= passed_jobs[1]["score"]  # ordered desc

    def test_no_matching_jobs_returns_zero_and_skips_writes(self, tmp_path):
        db = self._build_jobs_db(tmp_path)

        with patch("src.sheet.google_writer.get_service") as get_svc_mock:
            with patch("src.sheet.google_writer.create_daily_tab") as cdt:
                with patch("src.sheet.google_writer.append_jobs") as aj:
                    rc = gw._write_from_db(
                        sheet_id="fake_sheet", db_path=db, tab_name="2026-09-28",
                        limit=50, min_score=999,  # threshold too high
                    )

        assert rc == 0
        cdt.assert_not_called()
        aj.assert_not_called()

    def test_missing_db_returns_nonzero(self, tmp_path):
        with patch("src.sheet.google_writer.get_service") as get_svc_mock:
            rc = gw._write_from_db(
                sheet_id="fake_sheet",
                db_path=str(tmp_path / "nope.db"),
                tab_name="2026-09-28",
                limit=50,
                min_score=20,
            )
        assert rc != 0
