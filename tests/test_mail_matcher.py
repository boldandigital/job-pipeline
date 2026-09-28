"""Tests for the mail matcher — sender domain → ATS / company → job_id.

Covers:
  - Signal 1 (sender domain): ATS subdomains, careers@<company>, <company>.com
  - Signal 2 (subject): "Title at Company" EN, "Company — Title" DE
  - Signal 3 (body): Position: / Role: regex
  - Priority: domain > subject > body
  - 0-match returns None (don't pollute DB)
  - Most-recently-applied job wins among same-company candidates
  - No-match for jobs without applied_at (we never submitted → skip)

Run: pytest tests/test_mail_matcher.py -v
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

import pytest

from src.mail import matcher


# ---------------------------------------------------------------------------
# Fixtures: in-memory SQLite matching the canonical jobs schema (no DB file)
# ---------------------------------------------------------------------------


def _seed_db(rows: List[Dict[str, Any]]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            ats_type TEXT,
            applied_at TIMESTAMP
        )
    """)
    for r in rows:
        conn.execute(
            """
            INSERT INTO jobs (id, title, company, ats_type, applied_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                r.get("id"),
                r.get("title", "Senior CTO"),
                r.get("company", "Bold and Digital"),
                r.get("ats_type"),
                r.get("applied_at"),
            ),
        )
    conn.commit()
    return conn


@pytest.fixture
def empty_db() -> sqlite3.Connection:
    return _seed_db([])


@pytest.fixture
def single_job_db() -> sqlite3.Connection:
    return _seed_db([
        {"id": 1, "title": "Senior CTO", "company": "Bold and Digital",
         "ats_type": "greenhouse", "applied_at": "2026-09-25 10:00:00"},
    ])


@pytest.fixture
def multi_company_db() -> sqlite3.Connection:
    return _seed_db([
        {"id": 10, "title": "Senior CTO", "company": "Bold and Digital",
         "ats_type": "greenhouse", "applied_at": "2026-09-25 09:00:00"},  # newest
        {"id": 11, "title": "VP Engineering", "company": "Bold and Digital",
         "ats_type": "greenhouse", "applied_at": "2026-09-20 09:00:00"},
        {"id": 20, "title": "CTO", "company": "HostSalt",
         "ats_type": "workday", "applied_at": "2026-09-22 09:00:00"},
        {"id": 30, "title": "Engineering Manager", "company": "Acme Corp",
         "ats_type": None, "applied_at": None},  # never applied — should NOT match
    ])


# ---------------------------------------------------------------------------
# Signal 1: sender domain
# ---------------------------------------------------------------------------


def test_match_ats_domain_known_provider(single_job_db: sqlite3.Connection) -> None:
    m = matcher.match_email(
        single_job_db,
        from_header="noreply@boards.greenhouse.io",
        subject="Update on your application",
        body="",
    )
    assert m is not None
    assert m.job_id == 1
    assert m.signal == "sender_domain"


def test_match_company_subdomain(multi_company_db: sqlite3.Connection) -> None:
    m = matcher.match_email(
        multi_company_db,
        from_header="careers@boldandigital.com",
        subject="Update on your application",
        body="",
    )
    assert m is not None
    assert m.company == "Bold and Digital"
    # Most recently applied job for Bold and Digital wins.
    assert m.job_id == 10  # Senior CTO, applied 2026-09-25


def test_match_ats_domain_with_multiple_jobs(multi_company_db: sqlite3.Connection) -> None:
    """An ATS sub-domain matches any job with the same ats_type. HostSalt
    is on Workday; Bold and Digital is on Greenhouse."""
    m = matcher.match_email(
        multi_company_db,
        from_header="careers@myworkdayjobs.com",
        subject="Application update",
        body="",
    )
    assert m is not None
    assert m.job_id == 20
    assert m.company == "HostSalt"


def test_no_match_unknown_domain_returns_none(single_job_db: sqlite3.Connection) -> None:
    m = matcher.match_email(
        single_job_db,
        from_header="hello@strangerandomco.com",
        subject="Re: Senior CTO",
        body="",
    )
    assert m is None


# ---------------------------------------------------------------------------
# Signal 2: subject
# ---------------------------------------------------------------------------


def test_match_subject_en_at_form(single_job_db: sqlite3.Connection) -> None:
    m = matcher.match_email(
        single_job_db,
        from_header="random@strange.com",
        subject="Re: Senior CTO at Bold and Digital — Interview",
        body="",
    )
    assert m is not None
    assert m.job_id == 1
    assert m.signal == "subject_at"


def test_match_subject_dash_form(single_job_db: sqlite3.Connection) -> None:
    m = matcher.match_email(
        single_job_db,
        from_header="random@strange.com",
        subject="Bold and Digital — Senior CTO / Interview",
        body="",
    )
    assert m is not None
    assert m.job_id == 1
    assert m.signal == "subject_dash"


def test_subject_priority_over_body_when_present(single_job_db: sqlite3.Connection) -> None:
    m = matcher.match_email(
        single_job_db,
        from_header="random@strange.com",
        subject="Re: Bold and Digital — Senior CTO",
        body="Role: Some other title",
    )
    assert m is not None
    assert m.signal == "subject_dash"


# ---------------------------------------------------------------------------
# Signal 3: body
# ---------------------------------------------------------------------------


def test_match_body_position(single_job_db: sqlite3.Connection) -> None:
    m = matcher.match_email(
        single_job_db,
        from_header="random@strange.com",
        subject="Re: Your application",
        body="Position: Senior CTO at Bold and Digital",
    )
    assert m is not None
    assert m.signal == "body_position"


def test_match_body_role(single_job_db: sqlite3.Connection) -> None:
    m = matcher.match_email(
        single_job_db,
        from_header="random@strange.com",
        subject="Re: Your application",
        body="Role: Senior CTO",
    )
    assert m is not None
    assert m.signal == "body_role"


# ---------------------------------------------------------------------------
# Priority: domain > subject > body
# ---------------------------------------------------------------------------


def test_priority_domain_beats_subject(multi_company_db: sqlite3.Connection) -> None:
    """Sender domain for Bold and Digital → sender_domain signal, even
    though the subject is generic."""
    m = matcher.match_email(
        multi_company_db,
        from_header="careers@boldandigital.com",
        subject="Interview invitation — completely unrelated title",
        body="",
    )
    assert m is not None
    assert m.signal == "sender_domain"
    assert m.company == "Bold and Digital"


def test_priority_subject_beats_body(single_job_db: sqlite3.Connection) -> None:
    m = matcher.match_email(
        single_job_db,
        from_header="random@strange.com",
        subject="Re: Bold and Digital — Senior CTO",
        body="Position: Marketing Intern",
    )
    assert m is not None
    assert m.signal == "subject_dash"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_no_match_returns_none(empty_db: sqlite3.Connection) -> None:
    m = matcher.match_email(
        empty_db,
        from_header="noreply@boards.greenhouse.io",
        subject="Interview invitation",
        body="",
    )
    assert m is None


def test_job_without_applied_at_is_skipped(empty_db: sqlite3.Connection) -> None:
    """A company match for a job we never submitted is filtered out — we
    don't want to attribute emails to jobs that haven't gone out yet."""
    conn = _seed_db([
        {"id": 99, "title": "Senior CTO", "company": "Bold and Digital",
         "ats_type": None, "applied_at": None},
    ])
    m = matcher.match_email(
        conn,
        from_header="careers@boldandigital.com",
        subject="Application update",
        body="",
    )
    assert m is None


def test_from_header_with_display_name(single_job_db: sqlite3.Connection) -> None:
    m = matcher.match_email(
        single_job_db,
        from_header="Greenhouse Recruiting <noreply@boards.greenhouse.io>",
        subject="Update",
        body="",
    )
    assert m is not None


def test_empty_from_header_with_subject_match(single_job_db: sqlite3.Connection) -> None:
    m = matcher.match_email(
        single_job_db,
        from_header="",
        subject="Re: Bold and Digital — Senior CTO",
        body="",
    )
    assert m is not None
    assert m.signal == "subject_dash"


def test_most_recently_applied_wins(multi_company_db: sqlite3.Connection) -> None:
    """Two jobs for the same company → pick the one with newer applied_at."""
    m = matcher.match_email(
        multi_company_db,
        from_header="careers@boldandigital.com",
        subject="Update",
        body="",
    )
    assert m is not None
    assert m.job_id == 10  # Senior CTO, applied 2026-09-25 (newer)


def test_domain_word_overlap_heuristic() -> None:
    """Direct unit test of the heuristic — used as a guard for the more
    complex company-domain match in the sender path."""
    assert matcher._company_matches_domain("Bold and Digital", "boldandigital.com") is True
    assert matcher._company_matches_domain("Stripe", "stripe.com") is True
    assert matcher._company_matches_domain("Vercel", "shopify.com") is False
    assert matcher._company_matches_domain("", "boldandigital.com") is False
    assert matcher._company_matches_domain("Bold and Digital", "") is False


def test_parse_from_handles_weird_values() -> None:
    assert matcher._parse_from("") == ("", "")
    assert matcher._parse_from("Lars") == ("", "Lars")
    name, addr = matcher._parse_from("Lars <lars@bold.digital>")
    assert name == "Lars"
    assert addr == "lars@bold.digital"


def test_domain_of_handles_garbage() -> None:
    assert matcher._domain_of("careers@bold.digital") == "bold.digital"
    assert matcher._domain_of("lars@bold.digital>") == "bold.digital"
    assert matcher._domain_of("nope") == ""
    assert matcher._domain_of("") == ""