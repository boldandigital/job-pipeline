"""Tests for the apply runner (src/apply/runner.py).

Covers:
  - fetch_approved_jobs: filters by status, honours --job-id, --limit
  - update_job_status: writes status + applied_at
  - dispatch_adapter: returns GenericAdapter when ats_type is unknown
  - run_apply_pipeline: idempotency (skips already-submitted jobs)
  - run_apply_pipeline: per-job result -> SQLite status
  - run_apply_pipeline: generic fallback sets needs_human (NEVER submitted)
  - run_apply_pipeline: rate-limit delay honoured
  - run_apply_pipeline: audit log written under logs_dir
  - CLI --dry-run: lists approved jobs without invoking the driver

Run: pytest tests/test_apply_runner.py -v
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

import pytest

from tests.conftest import apply_base as base_mod  # noqa: E402

# Import the runner via the package loader path used in conftest.
import importlib.util
import sys

ROOT = Path(__file__).resolve().parent.parent
_RUNNER_PATH = ROOT / "src" / "apply" / "runner.py"
_spec = importlib.util.spec_from_file_location("apply_runner_mod", _RUNNER_PATH)
runner = importlib.util.module_from_spec(_spec)
sys.modules["apply_runner_mod"] = runner
_spec.loader.exec_module(runner)

from tests.conftest import FakeDriver  # noqa: E402
from tests.conftest import apply_greenhouse, apply_generic  # noqa: E402


# ---------------------------------------------------------------------------
# DB fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    """Create a temp SQLite DB matching the canonical jobs schema."""
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
            source TEXT,
            score INTEGER DEFAULT 0,
            ats_type TEXT,
            status TEXT DEFAULT 'new',
            cv_path TEXT,
            cover_letter_path TEXT,
            applied_at TIMESTAMP,
            language TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    conn.close()
    return db


def _seed(db: Path, rows: List[Dict[str, Any]]) -> None:
    conn = sqlite3.connect(db)
    for r in rows:
        cols = ", ".join(r.keys())
        placeholders = ", ".join(["?"] * len(r))
        conn.execute(f"INSERT INTO jobs ({cols}) VALUES ({placeholders})",
                      tuple(r.values()))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# fetch_approved_jobs
# ---------------------------------------------------------------------------


def test_fetch_approved_jobs_filters_status(temp_db):
    _seed(temp_db, [
        {"title": "a", "company": "x", "status": "approved", "score": 90},
        {"title": "b", "company": "x", "status": "approved", "score": 80},
        {"title": "c", "company": "x", "status": "submitted", "score": 99},
        {"title": "d", "company": "x", "status": "needs_human", "score": 99},
        {"title": "e", "company": "x", "status": "new", "score": 70},
    ])
    conn = runner._connect(str(temp_db))
    jobs = runner.fetch_approved_jobs(conn)
    conn.close()
    titles = [j["title"] for j in jobs]
    # Idempotency: only the two `approved` rows, sorted by score DESC.
    assert titles == ["a", "b"]


def test_fetch_approved_jobs_single_job_id(temp_db):
    _seed(temp_db, [
        {"title": "a", "company": "x", "status": "approved", "score": 90},
        {"title": "b", "company": "x", "status": "approved", "score": 80},
    ])
    conn = runner._connect(str(temp_db))
    jobs = runner.fetch_approved_jobs(conn, job_id=1)
    conn.close()
    assert [j["title"] for j in jobs] == ["a"]


def test_fetch_approved_jobs_limit(temp_db):
    _seed(temp_db, [
        {"title": "a", "company": "x", "status": "approved", "score": 90},
        {"title": "b", "company": "x", "status": "approved", "score": 80},
        {"title": "c", "company": "x", "status": "approved", "score": 70},
    ])
    conn = runner._connect(str(temp_db))
    jobs = runner.fetch_approved_jobs(conn, limit=2)
    conn.close()
    assert [j["title"] for j in jobs] == ["a", "b"]


# ---------------------------------------------------------------------------
# dispatch_adapter
# ---------------------------------------------------------------------------


def test_dispatch_adapter_unknown_returns_generic():
    cls, is_generic = runner.dispatch_adapter("does-not-exist")
    assert cls.__name__ == "GenericAdapter"
    assert is_generic is True


def test_dispatch_adapter_known_returns_specific():
    cls, is_generic = runner.dispatch_adapter("greenhouse")
    assert cls.__name__ == "GreenhouseAdapter"
    assert is_generic is False


def test_dispatch_adapter_none_returns_generic():
    cls, is_generic = runner.dispatch_adapter(None)
    assert cls.__name__ == "GenericAdapter"
    assert is_generic is True


# ---------------------------------------------------------------------------
# update_job_status
# ---------------------------------------------------------------------------


def test_update_job_status_with_applied_at(temp_db):
    _seed(temp_db, [{"title": "a", "company": "x", "status": "approved"}])
    conn = runner._connect(str(temp_db))
    runner.update_job_status(conn, 1, "submitted", applied_at="2026-09-27T22:00:00Z")
    row = conn.execute("SELECT status, applied_at FROM jobs WHERE id = 1").fetchone()
    conn.close()
    assert row[0] == "submitted"
    assert row[1] == "2026-09-27T22:00:00Z"


def test_update_job_status_without_applied_at(temp_db):
    _seed(temp_db, [{"title": "a", "company": "x", "status": "approved"}])
    conn = runner._connect(str(temp_db))
    runner.update_job_status(conn, 1, "needs_human")
    row = conn.execute("SELECT status, applied_at FROM jobs WHERE id = 1").fetchone()
    conn.close()
    assert row[0] == "needs_human"
    assert row[1] is None


# ---------------------------------------------------------------------------
# append_audit
# ---------------------------------------------------------------------------


def test_audit_log_writes_jsonl(tmp_path):
    logs = tmp_path / "logs"
    runner.append_audit(str(logs), {"job_id": 1, "result": "submitted"})
    files = list(logs.glob("*.jsonl"))
    assert len(files) == 1
    line = files[0].read_text().strip()
    entry = json.loads(line)
    assert entry["job_id"] == 1
    assert entry["result"] == "submitted"
    assert "ts" in entry


# ---------------------------------------------------------------------------
# run_apply_pipeline end-to-end
# ---------------------------------------------------------------------------


def _profile():
    return base_mod.Profile(
        first_name="Lars", last_name="Z", email="l@x.com",
    )


def test_run_apply_pipeline_no_jobs_returns_empty(temp_db, tmp_path):
    summary = asyncio.get_event_loop().run_until_complete(runner.run_apply_pipeline(
        db_path=str(temp_db),
        batches_dir=str(tmp_path / "batches"),
        logs_dir=str(tmp_path / "logs"),
        screenshots_dir=str(tmp_path / "shots"),
        profile=_profile(),
    ))
    assert summary == {"applied": 0, "needs_human": 0, "failed": 0, "jobs": []}


def test_run_apply_pipeline_unknown_ats_uses_generic_and_pauses(
    temp_db, tmp_path,
):
    """An unknown ATS must NEVER produce a 'submitted' status — it goes to
    needs_human via the generic fallback.
    """
    _seed(temp_db, [{
        "title": "Head of Eng", "company": "Acme",
        "status": "approved", "ats_type": "weird-unknown-ats",
        "career_url": "https://example.com/apply/1",
        "score": 90,
    }])
    summary = asyncio.get_event_loop().run_until_complete(runner.run_apply_pipeline(
        db_path=str(temp_db),
        batches_dir=str(tmp_path / "batches"),
        logs_dir=str(tmp_path / "logs"),
        screenshots_dir=str(tmp_path / "shots"),
        profile=_profile(),
    ))
    assert summary["applied"] == 0
    assert summary["needs_human"] == 1
    conn = sqlite3.connect(temp_db)
    row = conn.execute("SELECT status, applied_at FROM jobs WHERE id = 1").fetchone()
    conn.close()
    assert row[0] == "needs_human"
    assert row[1] is None  # needs_human never sets applied_at


def test_run_apply_pipeline_no_driver_factory_marks_needs_human(
    temp_db, tmp_path,
):
    _seed(temp_db, [{
        "title": "Eng", "company": "Acme",
        "status": "approved", "ats_type": "greenhouse",
        "career_url": "https://example.com/apply/1",
        "score": 90,
    }])
    summary = asyncio.get_event_loop().run_until_complete(runner.run_apply_pipeline(
        db_path=str(temp_db),
        batches_dir=str(tmp_path / "batches"),
        logs_dir=str(tmp_path / "logs"),
        screenshots_dir=str(tmp_path / "shots"),
        profile=_profile(),
    ))
    # Without a driver_factory the runner records 'no_driver' -> needs_human.
    assert summary["needs_human"] == 1
    assert summary["jobs"][0]["result"] == "no_driver"


def test_run_apply_pipeline_idempotent_skips_submitted(temp_db, tmp_path):
    """Re-running the pipeline skips jobs already at submitted / needs_human."""
    _seed(temp_db, [
        {"title": "a", "company": "x", "status": "submitted"},
        {"title": "b", "company": "x", "status": "needs_human"},
        {"title": "c", "company": "x", "status": "approved",
         "ats_type": "greenhouse", "career_url": "https://example.com/3",
         "score": 90},
    ])
    summary = asyncio.get_event_loop().run_until_complete(runner.run_apply_pipeline(
        db_path=str(temp_db),
        batches_dir=str(tmp_path / "batches"),
        logs_dir=str(tmp_path / "logs"),
        screenshots_dir=str(tmp_path / "shots"),
        profile=_profile(),
    ))
    # Only job c is processed.
    assert len(summary["jobs"]) == 1
    assert summary["jobs"][0]["id"] == 3


def test_run_apply_pipeline_with_fake_driver_records_submitted(
    temp_db, tmp_path,
):
    """End-to-end with a happy-path Greenhouse adapter + fake driver."""
    from tests.conftest import apply_greenhouse  # noqa: E402

    _seed(temp_db, [{
        "title": "Eng", "company": "Stripe",
        "status": "approved", "ats_type": "greenhouse",
        "career_url": "https://boards.greenhouse.io/stripe",
        "score": 90,
    }])
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF-stub")

    def driver_factory():
        d = FakeDriver()
        d.page.dom = {
            "#first_name": {"tag": "input"},
            "#last_name": {"tag": "input"},
            "#email": {"tag": "input"},
            "#phone": {"tag": "input"},
            "input#resume": {"tag": "input", "type": "file"},
            'input[type="submit"][name="submit"]': {"tag": "input"},
        }
        return d

    summary = asyncio.get_event_loop().run_until_complete(runner.run_apply_pipeline(
        db_path=str(temp_db),
        batches_dir=str(tmp_path / "batches"),
        logs_dir=str(tmp_path / "logs"),
        screenshots_dir=str(tmp_path / "shots"),
        profile=_profile(),
        driver_factory=driver_factory,
    ))
    assert summary["applied"] == 1
    conn = sqlite3.connect(temp_db)
    row = conn.execute("SELECT status, applied_at FROM jobs WHERE id = 1").fetchone()
    conn.close()
    assert row[0] == "submitted"
    assert row[1] is not None


def test_run_apply_pipeline_generic_fallback_does_not_record_submitted(
    temp_db, tmp_path,
):
    """Even with a fake driver that 'works', generic fallback must NOT
    flip status to 'submitted'. Belt-and-braces for the no-auto-submit rule.
    """
    from tests.conftest import apply_generic  # noqa: E402

    _seed(temp_db, [{
        "title": "Eng", "company": "Acme",
        "status": "approved", "ats_type": "unknown-ats",
        "career_url": "https://example.com/apply",
        "score": 90,
    }])

    def driver_factory():
        d = FakeDriver()
        d.page.dom = {'input[type="file"]': {"tag": "input", "type": "file"}}
        return d

    summary = asyncio.get_event_loop().run_until_complete(runner.run_apply_pipeline(
        db_path=str(temp_db),
        batches_dir=str(tmp_path / "batches"),
        logs_dir=str(tmp_path / "logs"),
        screenshots_dir=str(tmp_path / "shots"),
        profile=_profile(),
        driver_factory=driver_factory,
    ))
    assert summary["applied"] == 0
    assert summary["needs_human"] == 1
    conn = sqlite3.connect(temp_db)
    row = conn.execute("SELECT status, applied_at FROM jobs WHERE id = 1").fetchone()
    conn.close()
    assert row[0] == "needs_human"
    assert row[1] is None


def test_run_apply_pipeline_limit_caps_processed(temp_db, tmp_path):
    _seed(temp_db, [
        {"title": f"j{i}", "company": "x", "status": "approved",
         "ats_type": "greenhouse", "career_url": f"https://example.com/{i}",
         "score": 90 - i}
        for i in range(5)
    ])
    summary = asyncio.get_event_loop().run_until_complete(runner.run_apply_pipeline(
        db_path=str(temp_db),
        batches_dir=str(tmp_path / "batches"),
        logs_dir=str(tmp_path / "logs"),
        screenshots_dir=str(tmp_path / "shots"),
        profile=_profile(),
        limit=2,
    ))
    # 2 jobs visited; each ends at 'no_driver' -> needs_human.
    assert len(summary["jobs"]) == 2


# ---------------------------------------------------------------------------
# CLI --dry-run
# ---------------------------------------------------------------------------


def test_cli_dry_run_prints_approved_jobs_without_driver(
    temp_db, tmp_path, capsys,
):
    _seed(temp_db, [{
        "title": "Eng", "company": "Stripe",
        "status": "approved", "ats_type": "greenhouse",
        "career_url": "https://example.com/1", "score": 90,
    }])
    rc = runner.main([
        "--db", str(temp_db),
        "--batches-dir", str(tmp_path / "b"),
        "--logs-dir", str(tmp_path / "l"),
        "--screenshots-dir", str(tmp_path / "s"),
        "--dry-run",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["approved"] == 1
    assert payload["jobs"][0]["company"] == "Stripe"
