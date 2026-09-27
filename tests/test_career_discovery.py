#!/usr/bin/env python3
"""
Tests for src.discovery.career_discovery (ADOPT-14).

Coverage:
  1. detect_ats() URL pattern matching — all 18 ATS systems
  2. detect_ats() HTML signature matching — when URL is generic
  3. is_portal_url() / is_login_wall() helpers
  4. Cache TTL behavior (30-day window)
  5. Idempotency guard on UPDATE
  6. End-to-end fixture accuracy on the 20 known companies

Run: pytest tests/test_career_discovery.py -v
"""

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import pytest

# Ensure the src package is importable when run from project root.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.discovery import career_discovery as cd


# ──────────────────────────────────────────────────────────────────────
# 1. URL pattern matching — every ATS should hit its pattern.
# ──────────────────────────────────────────────────────────────────────
URL_FIXTURES = [
    # (url, expected_ats)
    ("https://jobs.softgarden.de/job/123",                         "softgarden"),
    ("https://mysoftgarden.io/apply",                             "softgarden"),
    ("https://klarna.wd3.myworkdayjobs.com/en-US/job/123",        "workday"),
    ("https://acme.workdayjobs.com/job/Engineer",                 "workday"),
    ("https://jobs.smartrecruiters.com/DeliveryHero/123",         "smartrecruiters"),
    ("https://jobs.join.com/apply/abc",                           "join"),
    ("https://jobs.ashbyhq.com/notion/123",                       "ashby"),
    ("https://boards.greenhouse.io/stripe",                       "greenhouse"),
    ("https://acme.coveto.de/jobs",                               "coveto"),
    ("https://jobs.umantis.com/v2/companies/portal",              "umantis"),
    ("https://apply.workable.com/n26",                            "workable"),
    ("https://jobs.lever.co/openai/abc",                          "lever"),
    ("https://acme.recruitee.com/o/jobs",                         "recruitee"),
    ("https://n26.jobs.personio.com/",                            "personio"),
    ("https://jobs.personio.de/x",                                "personio"),
    ("https://acme.successfactors.eu/sfcareer",                   "sap_sf"),
    ("https://acme.taleo.net/careersection",                      "taleo"),
    ("https://acme.icims.com/jobs/123",                           "icims"),
    ("https://acme.breezy.hr/",                                   "breezy"),
    ("https://acme.bamboohr.com/jobs",                            "bamboohr"),
    ("https://jobs.jobvite.com/acme/job/abc",                     "jobvite"),
    # Generic URL — should return None (no ATS detected from URL alone).
    ("https://acme.com/careers/123",                              None),
    # Edge cases — None / empty.
    ("",                                                          None),
]


@pytest.mark.parametrize("url,expected", URL_FIXTURES)
def test_detect_ats_url_patterns(url, expected):
    """All 18 ATS URL patterns + generic / empty fallback."""
    assert cd.detect_ats(url) == expected


# ──────────────────────────────────────────────────────────────────────
# 2. HTML signature matching — when URL is generic.
# ──────────────────────────────────────────────────────────────────────
HTML_FIXTURES = [
    # (html snippet, expected_ats)
    ('<meta name="generator" content="Workday">', "workday"),
    ('<meta name="generator" content="Greenhouse Job Board">', "greenhouse"),
    ('<script src="https://boards.greenhouse.io/embed/job_board"></script>', "greenhouse"),
    ('<iframe src="https://jobs.lever.co/openai"></iframe>', "lever"),
    ('<iframe src="https://jobs.ashbyhq.com/linear"></iframe>', "ashby"),
    ('<iframe src="https://acme.myworkdayjobs.com/en-US/job/123"></iframe>', "workday"),
    ('<iframe src="https://acme.smartrecruiters.com/Job/123"></iframe>', "smartrecruiters"),
    ('<iframe src="https://acme.workable.com/api/v3/widget/accounts/123"></iframe>', "workable"),
    ('<iframe src="https://acme.icims.com/jobs/123"></iframe>', "icims"),
    ('<iframe src="https://acme.jobvite.com/acme/job/abc"></iframe>', "jobvite"),
    # URL is generic but HTML embeds an ATS iframe.
    ('<html><body><iframe src="https://jobs.lever.co/vendor"></iframe></body></html>', "lever"),
]


@pytest.mark.parametrize("html,expected", HTML_FIXTURES)
def test_detect_ats_html_signatures(html, expected):
    """HTML signature matching catches ATS iframes / meta-generator tags."""
    assert cd.detect_ats("https://acme.com/careers", html=html) == expected


def test_detect_ats_url_priority_over_html():
    """URL pattern wins over HTML signature (faster, more reliable)."""
    # HTML says greenhouse but URL says workday → workday wins.
    html = '<meta name="generator" content="Greenhouse">'
    assert cd.detect_ats("https://acme.workdayjobs.com/job/123", html=html) == "workday"


# ──────────────────────────────────────────────────────────────────────
# 3. is_portal_url / is_login_wall helpers.
# ──────────────────────────────────────────────────────────────────────
PORTAL_FIXTURES = [
    ("https://www.linkedin.com/jobs/view/123", True),
    ("https://de.indeed.com/jobs?q=python",   True),
    ("https://www.stepstone.de/stelle/123",   True),
    ("https://www.xing.com/jobs/abc",         True),
    ("https://boards.greenhouse.io/stripe",   False),  # ATS but NOT a portal — direct careers
    ("https://klarna.wd3.myworkdayjobs.com",  False),
    ("https://www.acme.com/careers",          False),
    ("",                                     True),
    (None,                                   True),
]


@pytest.mark.parametrize("url,is_portal", PORTAL_FIXTURES)
def test_is_portal_url(url, is_portal):
    assert cd.is_portal_url(url) == is_portal


LOGIN_FIXTURES = [
    ('<html><head><title>Sign In - Acme</title></head><body>Please sign in.</body></html>', True),
    ('<html><head><title>Log In</title></head><body>Log in to continue.</body></html>',      True),
    ('<html><head><title>Bitte anmelden</title></head></html>',                              True),
    ('<html><head><title>Single Sign-On</title></head></html>',                             True),
    ('<html><head><title>Careers - Acme</title></head><body>Open positions...</body></html>', False),
    ('<html><body>We are hiring! <a href="/jobs/123">Apply</a></body></html>',              False),
    ("", False),
]


@pytest.mark.parametrize("html,is_wall", LOGIN_FIXTURES)
def test_is_login_wall(html, is_wall):
    assert cd.is_login_wall(html) == is_wall


# ──────────────────────────────────────────────────────────────────────
# 4. Cache TTL — 30-day window, refresh on miss.
# ──────────────────────────────────────────────────────────────────────
def test_cache_put_and_get():
    cd._cache.clear()
    cd._cache_put("Acme Corp", "https://acme.com/careers", "greenhouse")
    result = cd._cache_get("Acme Corp")
    assert result is not None
    url, ats = result
    assert url == "https://acme.com/careers"
    assert ats == "greenhouse"


def test_cache_normalizes_company_case():
    """Cache lookup should be case-insensitive."""
    cd._cache.clear()
    cd._cache_put("Acme Corp", "https://acme.com/careers", None)
    assert cd._cache_get("ACME CORP") is not None
    assert cd._cache_get("acme corp") is not None


def test_cache_ttl_expiry(monkeypatch):
    """Entries older than 30 days should be evicted."""
    cd._cache.clear()
    cd._cache_put("Acme Corp", "https://acme.com/careers", "greenhouse")
    # Fast-forward time.
    real_time = time.time
    monkeypatch.setattr(cd.time, "time", lambda: real_time() + cd.CACHE_TTL_SECONDS + 1)
    assert cd._cache_get("Acme Corp") is None


def test_cache_miss_returns_none():
    cd._cache.clear()
    assert cd._cache_get("Unknown Co") is None


# ──────────────────────────────────────────────────────────────────────
# 5. Idempotency guard — UPDATE only fires when career_url is empty.
# ──────────────────────────────────────────────────────────────────────
def test_layer1_idempotent_update(tmp_path):
    """A second run with a populated career_url should not overwrite it."""
    db = tmp_path / "jobs.db"
    jobspy = tmp_path / "jobspy.db"

    # Set up main DB with one job that already has career_url.
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY,
            title TEXT, company TEXT,
            career_url TEXT, ats_type TEXT,
            status TEXT DEFAULT 'new'
        )
    """)
    conn.execute(
        "INSERT INTO jobs (id, title, company, career_url, ats_type) "
        "VALUES (1, 'Senior CTO', 'Stripe', 'https://existing.com/careers', 'workday')"
    )
    conn.commit()

    # Set up JobSpy DB with a *different* career_url for the same job.
    js = sqlite3.connect(jobspy)
    js.execute("""
        CREATE TABLE jobs (
            title TEXT, company_name TEXT, job_url_direct TEXT, company_url TEXT
        )
    """)
    js.execute(
        "INSERT INTO jobs VALUES ('Senior CTO', 'Stripe', 'https://stripe.com/jobs', '')"
    )
    js.commit()
    js.close()

    # Run Layer 1 — should be a no-op because career_url is already populated.
    updated = cd.layer1_jobspy_xref(conn, str(jobspy))
    assert updated == 0
    row = conn.execute("SELECT career_url, ats_type FROM jobs WHERE id = 1").fetchone()
    assert row[0] == "https://existing.com/careers"
    assert row[1] == "workday"

    conn.close()


def test_layer1_populates_when_empty(tmp_path):
    """A first run with empty career_url should populate it from JobSpy."""
    db = tmp_path / "jobs.db"
    jobspy = tmp_path / "jobspy.db"

    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY,
            title TEXT, company TEXT,
            career_url TEXT, ats_type TEXT,
            status TEXT DEFAULT 'new'
        )
    """)
    conn.execute(
        "INSERT INTO jobs (id, title, company) VALUES (1, 'Senior CTO', 'Stripe')"
    )
    conn.commit()

    js = sqlite3.connect(jobspy)
    js.execute("""
        CREATE TABLE jobs (
            title TEXT, company_name TEXT, job_url_direct TEXT, company_url TEXT
        )
    """)
    js.execute(
        "INSERT INTO jobs VALUES ('Senior CTO', 'Stripe', 'https://boards.greenhouse.io/stripe', '')"
    )
    js.commit()
    js.close()

    updated = cd.layer1_jobspy_xref(conn, str(jobspy))
    assert updated == 1
    row = conn.execute("SELECT career_url, ats_type FROM jobs WHERE id = 1").fetchone()
    assert row[0] == "https://boards.greenhouse.io/stripe"
    assert row[1] == "greenhouse"

    conn.close()


# ──────────────────────────────────────────────────────────────────────
# 6. End-to-end fixture accuracy — 20 known companies.
# ──────────────────────────────────────────────────────────────────────
FIXTURES_PATH = PROJECT_ROOT / "tests" / "fixtures" / "ats_test_cases.json"


def _load_fixtures():
    with open(FIXTURES_PATH) as f:
        return json.load(f)["fixtures"]


def test_fixture_accuracy():
    """Run detect_ats on the 20 known ATS deployments; assert >=80% accuracy."""
    fixtures = _load_fixtures()
    correct = 0
    misses = []
    for f in fixtures:
        got = cd.detect_ats(f["url"])
        if got == f["expected_ats"]:
            correct += 1
        else:
            misses.append((f["company"], f["url"], f["expected_ats"], got))

    accuracy = correct / len(fixtures) * 100
    print(f"\n  ATS fixture accuracy: {correct}/{len(fixtures)} = {accuracy:.0f}%")
    if misses:
        print("  Misses:")
        for company, url, exp, got in misses:
            print(f"    - {company:20s} expected={exp:18s} got={got}")

    assert accuracy >= 80, (
        f"ATS detection accuracy {accuracy:.0f}% below 80% threshold "
        f"({len(misses)} misses: {[m[0] for m in misses]})"
    )


def test_portal_urls_are_not_career_urls():
    """All portal URLs should be rejected by is_portal_url — even if they look like careers."""
    portal_urls = [
        "https://www.linkedin.com/jobs/view/123",
        "https://de.indeed.com/pagead/clk",
        "https://www.stepstone.de/stelle--123",
        "https://www.xing.com/jobs/abc",
        "https://www.glassdoor.de/Job/abc",
    ]
    for url in portal_urls:
        assert cd.is_portal_url(url), f"Portal URL not flagged: {url}"


# ──────────────────────────────────────────────────────────────────────
# 7. discover_career_for_jobs() — top-level orchestrator.
# ──────────────────────────────────────────────────────────────────────
def test_discover_career_for_jobs_returns_counts(tmp_path):
    """Top-level orchestrator should return a counts dict with the right keys."""
    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY,
            title TEXT, company TEXT,
            url TEXT,
            career_url TEXT, ats_type TEXT,
            status TEXT DEFAULT 'new', source TEXT
        )
    """)
    conn.execute("INSERT INTO jobs (id, title, company, source) VALUES (1, 'X', 'Y', 'stepstone')")
    conn.commit()

    # Empty JobSpy DB → layer1 returns 0 immediately.
    jobspy = tmp_path / "jobspy.db"
    sqlite3.connect(jobspy).close()

    counts = cd.discover_career_for_jobs(conn, max_jobs=5, config={"jobspy_db": str(jobspy)})
    assert set(counts.keys()) == {"layer1", "layer2", "layer3", "total"}
    assert counts["total"] >= 0

    conn.close()


def test_discover_career_for_jobs_layers_subset(tmp_path):
    """`enabled_layers` config should let us run only L1 (fastest, no network)."""
    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY,
            title TEXT, company TEXT,
            career_url TEXT, ats_type TEXT,
            status TEXT DEFAULT 'new', source TEXT
        )
    """)
    conn.commit()

    jobspy = tmp_path / "jobspy.db"
    sqlite3.connect(jobspy).close()

    counts = cd.discover_career_for_jobs(
        conn, max_jobs=5,
        config={"jobspy_db": str(jobspy), "enabled_layers": (1,)},
    )
    assert counts["layer1"] == 0  # no JobSpy data
    assert counts["layer2"] == 0  # skipped
    assert counts["layer3"] == 0  # skipped

    conn.close()
