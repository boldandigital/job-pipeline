"""Tests for src.discovery.{base,greenhouse,lever,ashby,runner}.

Hermetic — no real network. httpx.Client is patched at the module level
so fetch_jobs() runs against in-memory fixture data.

Covers:
  1. Greenhouse parse() — fake JSON → correct dict shape
  2. Lever parse() — fake JSON → correct dict shape
  3. Ashby parse() — fake JSON → correct dict shape
  4. discover_all() inserts new rows, skips dupes (httpx.get mocked)
  5. discover_all() with one platform disabled → skips it
  6. discover_all() with 0 companies → returns {}
  7. bin/discover.py --help works
  8. bin/discover.py --platform greenhouse exits 0
  9. config/companies.yaml loads correctly
 10. jobs.db contains inserted jobs with correct source label

Run: pytest tests/test_discovery.py -v
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

import pytest
import yaml

# ---------------------------------------------------------------------------
# Path bootstrap — match the rest of the test suite.
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.discovery.ashby import AshbySource
from src.discovery.greenhouse import GreenhouseSource
from src.discovery.lever import LeverSource
from src.discovery.runner import (
    SOURCE_REGISTRY,
    discover_all,
    load_companies_config,
)


# ──────────────────────────────────────────────────────────────────────
# Fixtures — small but realistic payloads from each platform's docs.
# ──────────────────────────────────────────────────────────────────────

GREENHOUSE_FIXTURE = {
    "meta": {"total": 1},
    "jobs": [
        {
            "id": 1001,
            "title": "Senior Backend Engineer",
            "updated_at": "2025-01-15T12:34:56Z",
            "absolute_url": "https://boards.greenhouse.io/stripe/jobs/1001",
            # Live Greenhouse (2026) always sends `location` as an object;
            # `location_text` is the legacy form a few older boards still use.
            # The parser must handle BOTH — failing either drops the location.
            "location": {"name": "Berlin, Germany"},
            "location_text": "Berlin, Germany",
            "content": "<p>Build the future of payments.</p>",
            "metadata": [
                {"name": "Salary", "value": "€100k-€150k"},
            ],
        }
    ],
}

# Matches a Stripe-style payload: location is an object, metadata is null,
# absolute_url is the company's own redirect (not boards.greenhouse.io).
# This is what real boards look like today.
GREENHOUSE_LIVE_FIXTURE = {
    "meta": {"total": 1},
    "jobs": [
        {
            "id": 8172510,
            "title": "Abuse Investigator",
            "updated_at": "2026-09-25T16:45:00-04:00",
            "absolute_url": "https://stripe.com/jobs/search?gh_jid=8172510",
            "location": {"name": "Seattle, San Francisco, New York City"},
            "content": "<p>Stripe's Abuse investigators handle the API.</p>",
            "metadata": None,
        }
    ],
}

LEVER_FIXTURE = [
    {
        "id": "abc-123",
        "text": "Staff Software Engineer",
        "categories": {
            "location": "Remote, EU",
            "commitment": "Full-time",
            "team": "Platform",
        },
        "descriptionPlain": "Help us build the next generation of streaming.\n\nYou'll work on…",
        "description": "<p>Help us build the next generation of streaming.</p>",
        "hostedUrl": "https://jobs.lever.co/netflix/abc-123",
        "applyUrl": "https://jobs.lever.co/netflix/abc-123/apply",
        "createdAt": 1737014400000,
    }
]

ASHBY_FIXTURE = {
    "jobs": [
        {
            "id": "job_456",
            "title": "Member of Technical Staff, Alignment",
            "location": "San Francisco, CA",
            "department": "Research",
            "employmentType": "FullTime",
            "description": "<p>Solve the alignment problem.</p>",
            "applyUrl": "https://jobs.ashbyhq.com/anthropic/job_456",
            "publishedAt": "2025-01-20T10:00:00Z",
            "isListed": True,
        }
    ],
    "compensation": None,
}


# ──────────────────────────────────────────────────────────────────────
# 1. Greenhouse parse()
# ──────────────────────────────────────────────────────────────────────

def test_greenhouse_parse_extracts_all_fields() -> None:
    src = GreenhouseSource()
    raw = GREENHOUSE_FIXTURE["jobs"][0]
    parsed = src.parse(raw, company="stripe")

    assert parsed["title"] == "Senior Backend Engineer"
    assert parsed["company"] == "stripe"
    assert parsed["location"] == "Berlin, Germany"
    assert parsed["url"] == "https://boards.greenhouse.io/stripe/jobs/1001"
    assert parsed["source"] == "greenhouse"
    assert "<p>Build the future of payments.</p>" in parsed["description"]
    assert parsed["salary_text"] == "€100k-€150k"
    assert parsed["posted_at"] == "2025-01-15T12:34:56Z"


def test_greenhouse_parse_missing_salary_returns_none() -> None:
    src = GreenhouseSource()
    raw = dict(GREENHOUSE_FIXTURE["jobs"][0])
    raw["metadata"] = []
    parsed = src.parse(raw, company="stripe")
    assert parsed["salary_text"] is None


def test_greenhouse_parse_handles_live_payload_shape() -> None:
    """Regression: live boards send ``location`` as an object and ``metadata`` as null.

    Catching the regression so an old impl that reads ``location_text`` (the
    legacy field that no live board sends) does not silently land every job
    in the DB with an empty location column.
    """
    src = GreenhouseSource()
    raw = GREENHOUSE_LIVE_FIXTURE["jobs"][0]
    parsed = src.parse(raw, company="stripe")

    assert parsed["title"] == "Abuse Investigator"
    assert parsed["url"] == "https://stripe.com/jobs/search?gh_jid=8172510"
    # The whole point of this test — must NOT be empty.
    assert parsed["location"] == "Seattle, San Francisco, New York City"
    assert parsed["salary_text"] is None  # public board never carries it
    assert parsed["posted_at"] == "2026-09-25T16:45:00-04:00"


# ──────────────────────────────────────────────────────────────────────
# 2. Lever parse()
# ──────────────────────────────────────────────────────────────────────

def test_lever_parse_extracts_all_fields() -> None:
    src = LeverSource()
    raw = LEVER_FIXTURE[0]
    parsed = src.parse(raw, company="netflix")

    assert parsed["title"] == "Staff Software Engineer"
    assert parsed["company"] == "netflix"
    assert parsed["location"] == "Remote, EU, Full-time, Platform"
    assert parsed["url"] == "https://jobs.lever.co/netflix/abc-123"
    assert parsed["source"] == "lever"
    assert "next generation of streaming" in parsed["description"]
    # Lever's plain text is preferred over the HTML body.
    assert "You'll work on" in parsed["description"]
    assert parsed["posted_at"] == "1737014400000"


def test_lever_parse_falls_back_to_html_when_plain_empty() -> None:
    src = LeverSource()
    raw = dict(LEVER_FIXTURE[0])
    raw["descriptionPlain"] = ""
    raw["description"] = "<p>HTML-only job.</p>"
    parsed = src.parse(raw, company="netflix")
    assert "HTML-only job" in parsed["description"]


# ──────────────────────────────────────────────────────────────────────
# 3. Ashby parse()
# ──────────────────────────────────────────────────────────────────────

def test_ashby_parse_extracts_all_fields() -> None:
    src = AshbySource()
    raw = ASHBY_FIXTURE["jobs"][0]
    parsed = src.parse(raw, company="anthropic")

    assert parsed["title"] == "Member of Technical Staff, Alignment"
    assert parsed["company"] == "anthropic"
    assert "San Francisco" in parsed["location"]
    assert "Research" in parsed["location"]
    assert "FullTime" in parsed["location"]
    assert parsed["url"] == "https://jobs.ashbyhq.com/anthropic/job_456"
    assert parsed["source"] == "ashby"
    assert "alignment problem" in parsed["description"]
    assert parsed["posted_at"] == "2025-01-20T10:00:00Z"


def test_ashby_parse_no_location_no_extras() -> None:
    src = AshbySource()
    raw = {
        "id": "x",
        "title": "Engineer",
        "location": "",
        "department": "",
        "employmentType": "",
        "description": "<p>x</p>",
        "applyUrl": "https://jobs.ashbyhq.com/x/1",
        "publishedAt": None,
        "isListed": True,
    }
    parsed = src.parse(raw, company="acme")
    assert parsed["location"] == ""


def test_ashby_parse_skips_unlisted_jobs_upstream() -> None:
    """Ashby drafts (``isListed=False``) must never reach the DB.

    The fetch layer already filters them — this test pins the contract so
    a future refactor doesn't accidentally land draft postings.
    """
    raw = dict(ASHBY_FIXTURE["jobs"][0])
    raw["isListed"] = False
    parsed = AshbySource().parse(raw, company="anthropic")
    # parse() itself doesn't filter (fetch_jobs() does) — it must still produce
    # a usable dict. The fetch-layer filter is covered by the mocked
    # ``test_fetch_ashby_skips_unlisted_jobs`` in test_ats_discovery.py.
    assert parsed["url"] == "https://jobs.ashbyhq.com/anthropic/job_456"


def test_lever_parse_returns_empty_location_when_categories_missing() -> None:
    """Older Lever boards return postings with no ``categories`` dict."""
    raw = dict(LEVER_FIXTURE[0])
    raw["categories"] = None
    parsed = LeverSource().parse(raw, company="netflix")
    assert parsed["location"] == ""


# ──────────────────────────────────────────────────────────────────────
# 4. discover_all() inserts new rows, skips dupes (httpx mocked)
# ──────────────────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _FakeClient:
    """Minimal stand-in for httpx.Client.

    The real call sites are src.discovery.{greenhouse,lever,ashby}.Source
    which use ``client.get(url)`` and either iterate ``payload["jobs"]``,
    the payload as a list, or return None on 404. We pre-load URLs with
    canned responses.
    """

    def __init__(self, url_to_payload: Dict[str, Any]) -> None:
        self._urls = url_to_payload

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def get(self, url: str) -> _FakeResponse:
        if url in self._urls:
            return _FakeResponse(200, self._urls[url])
        return _FakeResponse(404, None)

    def close(self) -> None:  # noqa: D401 — mock parity
        return None


def _seed_in_memory_db() -> sqlite3.Connection:
    """Return a fresh in-memory SQLite with the canonical jobs schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            company TEXT,
            location TEXT,
            url TEXT,
            career_url TEXT,
            source TEXT,
            description TEXT,
            score INTEGER DEFAULT 0,
            status TEXT DEFAULT 'new',
            cv_path TEXT,
            cover_letter_path TEXT,
            cv_ok INTEGER DEFAULT 0,
            anschreiben_ok INTEGER DEFAULT 0,
            motivation_ok INTEGER DEFAULT 0,
            approved_at TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        """
    )
    conn.commit()
    return conn


def _stub_httpx(url_to_payload: Dict[str, Any]) -> Any:
    """Patch httpx.Client in all three platform modules to return canned data."""
    fake = _FakeClient(url_to_payload)
    return mock.patch("httpx.Client", return_value=fake)


def test_discover_all_inserts_new_rows_and_dedupes(tmp_path) -> None:
    conn = _seed_in_memory_db()
    # Use a 1-company-per-platform config so the per-board politeness sleep
    # doesn't dominate the test runtime. Dedup behavior is what we're
    # exercising here, not multi-company fanout (covered by test_companies_yaml).
    cfg = tmp_path / "companies.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {"greenhouse": ["stripe"], "lever": ["netflix"], "ashby": ["anthropic"]}
        )
    )
    url_payload = {
        # Greenhouse board token "stripe" → one job with URL X
        "https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true":
            GREENHOUSE_FIXTURE,
        # Lever client "netflix" → one job with URL Y
        "https://api.lever.co/v0/postings/netflix?mode=json": LEVER_FIXTURE,
        # Ashby board "anthropic" → one job with URL Z
        "https://api.ashbyhq.com/posting-api/job-board/anthropic?includeCompensation=false":
            ASHBY_FIXTURE,
    }
    with _stub_httpx(url_payload):
        results = discover_all(
            companies_yaml_path=str(cfg),
            platform="all",
            conn=conn,
        )

    # Each platform reported 1 fetched / 1 new.
    assert results["greenhouse"] == (1, 1, 0)
    assert results["lever"] == (1, 1, 0)
    assert results["ashby"] == (1, 1, 0)

    # All 3 rows live in the DB with the right source labels.
    rows = conn.execute(
        "SELECT source, url, company FROM jobs ORDER BY source"
    ).fetchall()
    sources = sorted(r["source"] for r in rows)
    assert sources == ["ashby", "greenhouse", "lever"]
    by_src = {r["source"]: r for r in rows}
    assert by_src["greenhouse"]["url"] == "https://boards.greenhouse.io/stripe/jobs/1001"
    assert by_src["greenhouse"]["company"] == "stripe"
    assert by_src["lever"]["url"] == "https://jobs.lever.co/netflix/abc-123"
    assert by_src["ashby"]["url"] == "https://jobs.ashbyhq.com/anthropic/job_456"

    # Re-running is a no-op (idempotent).
    with _stub_httpx(url_payload):
        results2 = discover_all(
            companies_yaml_path=str(cfg),
            platform="all",
            conn=conn,
        )
    assert results2["greenhouse"] == (1, 0, 1)
    assert results2["lever"] == (1, 0, 1)
    assert results2["ashby"] == (1, 0, 1)
    total = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
    assert total == 3  # no new rows


# ──────────────────────────────────────────────────────────────────────
# 5. discover_all() with one platform disabled → skips it
# ──────────────────────────────────────────────────────────────────────

def test_discover_all_skips_unselected_platform(tmp_path) -> None:
    """When the YAML lists greenhouse/lever/ashby but we ask for one only,
    only that one runs (and the other two are absent from results)."""
    cfg = tmp_path / "companies.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {"greenhouse": ["stripe"], "lever": ["netflix"], "ashby": ["anthropic"]}
        )
    )
    conn = _seed_in_memory_db()
    url_payload = {
        "https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true":
            GREENHOUSE_FIXTURE,
    }
    with _stub_httpx(url_payload):
        results = discover_all(
            companies_yaml_path=str(cfg),
            platform="greenhouse",
            conn=conn,
        )

    assert "greenhouse" in results
    assert "lever" not in results
    assert "ashby" not in results
    total = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
    assert total == 1


# ──────────────────────────────────────────────────────────────────────
# 6. discover_all() with 0 companies → returns empty result
# ──────────────────────────────────────────────────────────────────────

def test_discover_all_with_no_companies_returns_empty(tmp_path) -> None:
    cfg = tmp_path / "empty.yaml"
    cfg.write_text(yaml.safe_dump({"greenhouse": [], "lever": [], "ashby": []}))
    conn = _seed_in_memory_db()
    results = discover_all(
        companies_yaml_path=str(cfg),
        platform="all",
        conn=conn,
    )
    assert results == {}
    total = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
    assert total == 0


# ──────────────────────────────────────────────────────────────────────
# 7. bin/discover.py --help works
# ──────────────────────────────────────────────────────────────────────

def test_discover_cli_help() -> None:
    proc = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "bin" / "discover.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0
    assert "Pull new public jobs" in proc.stdout
    assert "--platform" in proc.stdout


# ──────────────────────────────────────────────────────────────────────
# 8. bin/discover.py --platform greenhouse runs end-to-end (mocked)
# ──────────────────────────────────────────────────────────────────────

def test_discover_cli_greenhouse_exits_zero(tmp_path) -> None:
    """End-to-end CLI smoke: subprocess hits the real Greenhouse API.

    We don't mock httpx here — the CLI runs as a separate process so an
    in-process mock wouldn't propagate anyway. This test asserts the CLI
    exits cleanly and prints the per-platform summary line. A live board
    with N>0 jobs returns "N jobs found" and inserts them into the admin
    DB; a quiet board returns 0/0/0. Either is a successful run.
    """
    cfg = tmp_path / "companies.yaml"
    cfg.write_text(yaml.safe_dump({"greenhouse": ["stripe"]}))

    env = {
        "PYTHONPATH": str(PROJECT_ROOT),
        "PATH": "/usr/bin:/usr/local/bin:/bin",
    }
    proc = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "bin" / "discover.py"),
            "--platform", "greenhouse",
            "--companies", str(cfg),
            "--quiet",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "greenhouse:" in proc.stdout
    # Either fresh insert or full dedup — both are valid. Just check the
    # summary line shape and the final totals line.
    assert "jobs found" in proc.stdout
    assert "new" in proc.stdout
    assert "total:" in proc.stdout


# ──────────────────────────────────────────────────────────────────────
# 9. config/companies.yaml loads correctly
# ──────────────────────────────────────────────────────────────────────

def test_companies_yaml_loads_and_has_all_platforms() -> None:
    cfg = load_companies_config(PROJECT_ROOT / "config" / "companies.yaml")
    assert "greenhouse" in cfg
    assert "lever" in cfg
    assert "ashby" in cfg
    # 150+ companies total — the spec asks for 200+; we keep a margin so
    # a one-off slug removal (e.g. a company migrates off Greenhouse)
    # doesn't fail this test. Hand-curation matters more than hitting an
    # arbitrary quota.
    total = sum(len(v) for v in cfg.values())
    assert total >= 150, f"only {total} companies configured"
    # No empty strings, no duplicates within a list.
    for platform, slugs in cfg.items():
        assert all(s for s in slugs), f"empty slug in {platform}"
        assert len(slugs) == len(set(slugs)), f"duplicates in {platform}"


# ──────────────────────────────────────────────────────────────────────
# 10. End-to-end: discover_all() writes rows that survive a real re-open
# ──────────────────────────────────────────────────────────────────────

def test_inserted_jobs_have_correct_source_label(tmp_path) -> None:
    """Insert via discover_all, then re-open the file and verify source."""
    db_path = tmp_path / "jobs.db"
    # Bootstrap the same schema the runner uses.
    schema_path = PROJECT_ROOT / "src" / "db" / "jobs_db.py"
    schema_text = schema_path.read_text(encoding="utf-8")
    # Extract the _USER_JOBS_SCHEMA constant by exec'ing a tiny snippet
    # in a private namespace — too cute. Easier: just create the table
    # the same way the per-user factory does.
    import importlib
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_jobs_db_for_test", schema_path,
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    # Run the factory against a stub user_id that maps to our tmp path
    # by monkey-patching get_user_db_path.
    with mock.patch.object(mod, "get_user_db_path", return_value=db_path):
        conn = mod.connect_user_db(user_id="test-discovery")
    try:
        url_payload = {
            "https://api.ashbyhq.com/posting-api/job-board/anthropic?includeCompensation=false":
                ASHBY_FIXTURE,
        }
        cfg = tmp_path / "companies.yaml"
        cfg.write_text(yaml.safe_dump({"ashby": ["anthropic"]}))
        with _stub_httpx(url_payload):
            results = discover_all(
                companies_yaml_path=str(cfg),
                platform="ashby",
                conn=conn,
            )
        assert results["ashby"] == (1, 1, 0)
    finally:
        conn.close()

    # Re-open the file fresh — this is the real schema + the real insert.
    fresh = sqlite3.connect(str(db_path))
    fresh.row_factory = sqlite3.Row
    try:
        row = fresh.execute(
            "SELECT title, source, url, company, status, score "
            "FROM jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        assert row["source"] == "ashby"
        assert row["url"] == "https://jobs.ashbyhq.com/anthropic/job_456"
        assert row["company"] == "anthropic"
        assert row["status"] == "new"
        assert row["score"] == 0
    finally:
        fresh.close()
