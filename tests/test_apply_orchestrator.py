"""Tests for the apply orchestrator (src/apply/orchestrator.py).

What these tests actually pin down
----------------------------------
The assertions that matter are about *routing* and the *safety gates*,
because every network call is stubbed. Specifically:

  - the approval gate refuses anything that is not status='approved'
  - idempotency skips anything already carrying applied_at
  - the daily cap refuses the N+1st submission and resets on a new day
  - browser channels ALWAYS return paused_for_review and never click Submit
  - a missing employer API key downgrades to the browser channel instead of
    failing the job — this is the real-world default, because the
    Greenhouse/Lever/Ashby keys belong to the employer, not the candidate
  - the URL each vendor's endpoint is called with matches what the vendor
    actually documents. These are the regression tests for three wrong
    endpoints in an earlier draft: a non-existent Greenhouse
    ``/v1/applications``, a Lever URL missing its ``{site}`` path segment,
    and a board-less Ashby ``/job-board/apply``. All three were probed live
    and returned 401/400/401.

Run: pytest tests/test_apply_orchestrator.py -v
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src.apply import orchestrator as orch
from src.apply.ats_adapters.base import ApplyResult, Profile


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SCHEMA = """
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
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

#: Slim self-host schema (src/db/jobs_db.py) — deliberately missing
#: ats_type / score / applied_channel to prove we degrade rather than explode.
SLIM_SCHEMA = """
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    company TEXT,
    url TEXT,
    career_url TEXT,
    source TEXT,
    status TEXT DEFAULT 'new',
    cv_path TEXT,
    cover_letter_path TEXT,
    applied_at TIMESTAMP
);
"""


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "jobs.db"
    conn = sqlite3.connect(p)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return p


@pytest.fixture
def slim_db(tmp_path: Path) -> Path:
    p = tmp_path / "slim.db"
    conn = sqlite3.connect(p)
    conn.executescript(SLIM_SCHEMA)
    conn.commit()
    conn.close()
    return p


@pytest.fixture
def rate_file(tmp_path: Path) -> str:
    return str(tmp_path / "daily_apply_count.json")


@pytest.fixture
def profile() -> Profile:
    return Profile(
        first_name="Lars",
        last_name="Zimmermann",
        email="lars.z@icloud.com",
        phone="+32 470 123 456",
        linkedin="https://linkedin.com/in/lars-z",
        portfolio="https://boldandigital.com",
        extras={"years_experience": 12},
    )


@pytest.fixture
def cv(tmp_path: Path) -> str:
    p = tmp_path / "cv.pdf"
    p.write_bytes(b"%PDF-1.4 stub")
    return str(p)


def seed(db: Path, rows: List[Dict[str, Any]]) -> List[int]:
    conn = sqlite3.connect(db)
    ids = []
    for r in rows:
        cols = ", ".join(r)
        marks = ", ".join("?" * len(r))
        cur = conn.execute(f"INSERT INTO jobs ({cols}) VALUES ({marks})", tuple(r.values()))
        ids.append(cur.lastrowid)
    conn.commit()
    conn.close()
    return ids


def read_row(db: Path, job_id: int) -> Dict[str, Any]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    return dict(row) if row else {}


def make(db: Path, rate_file: str, profile: Profile, **kw) -> orch.ApplyOrchestrator:
    return orch.ApplyOrchestrator(
        user_id="lars", db_path=str(db), rate_limit_path=rate_file,
        profile=profile, **kw,
    )


def _ok_browser(*a, **kw) -> ApplyResult:
    """Browser stub that submits (used where the pause itself is not the point)."""
    platform = kw.get("platform") or ""
    return ApplyResult(success=True, submitted=True, ats_type=platform)


def _pause_browser(*a, **kw) -> ApplyResult:
    platform = kw.get("platform") or ""
    return ApplyResult(success=False, paused_for_review=True,
                       error="paused for human", ats_type=platform)


@pytest.fixture(autouse=True)
def no_live_keys(monkeypatch):
    """No test may accidentally inherit a real employer API key."""
    for var in ("GREENHOUSE_JOB_BOARD_API_KEY", "LEVER_API_KEY", "ASHBY_API_KEY"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def no_browser(monkeypatch):
    """Fail loudly if a test forgets to stub the browser channel."""
    def _boom(*a, **k):
        raise AssertionError("a real browser must never launch in tests")
    monkeypatch.setattr(orch, "_open_browser", _boom)


# ---------------------------------------------------------------------------
# Safety gates — the invariants that must never regress
# ---------------------------------------------------------------------------


def test_unapproved_job_is_refused_without_touching_any_adapter(
    db, rate_file, profile, monkeypatch, no_browser
):
    """A job that is NOT approved must never reach a channel."""
    (jid,) = seed(db, [{"title": "CTO", "company": "Acme", "status": "new",
                        "ats_type": "greenhouse"}])

    def _explode(*a, **k):  # pragma: no cover — must not run
        raise AssertionError("adapter ran for an unapproved job")

    monkeypatch.setattr(orch, "_apply_api_channel", _explode)
    monkeypatch.setattr(orch, "_fill_browser_channel", _explode)

    out = make(db, rate_file, profile).apply_job(jid)
    assert out["ok"] is False
    assert out["status"] == "refused"
    assert out["error"] == "status_gate"
    assert read_row(db, jid)["status"] == "new"  # untouched


def test_already_applied_job_is_skipped_idempotently(
    db, rate_file, profile, monkeypatch, no_browser
):
    """applied_at set => skip: no channel, no cap bump."""
    (jid,) = seed(db, [{"title": "CTO", "company": "Acme", "status": "approved",
                        "ats_type": "greenhouse",
                        "applied_at": "2026-09-27T22:00:00Z"}])

    def _explode(*a, **k):  # pragma: no cover
        raise AssertionError("adapter ran for an already-applied job")

    monkeypatch.setattr(orch, "_apply_api_channel", _explode)

    out = make(db, rate_file, profile).apply_job(jid)
    assert out["status"] == "skipped"
    assert out["error"] == "already_applied"
    assert orch._user_today_count(orch._read_rate_limit(rate_file), "lars") == 0


def test_unknown_platform_is_refused_not_guessed(
    db, rate_file, profile, monkeypatch, no_browser
):
    """An unroutable ATS is refused; we never guess a channel for it."""
    (jid,) = seed(db, [{"title": "CTO", "company": "Acme", "status": "approved",
                        "ats_type": "taleo", "url": "https://acme.taleo.net/x"}])

    def _explode(*a, **k):  # pragma: no cover
        raise AssertionError("adapter ran for an unroutable platform")

    monkeypatch.setattr(orch, "_apply_api_channel", _explode)
    monkeypatch.setattr(orch, "_fill_browser_channel", _explode)

    out = make(db, rate_file, profile).apply_job(jid)
    assert out["status"] == "refused"
    assert out["channel"] == "unknown"


# ---------------------------------------------------------------------------
# API channel — request shape matches the verified vendor contracts
# ---------------------------------------------------------------------------


def test_greenhouse_posts_to_documented_endpoint_with_basic_auth(
    db, rate_file, profile, cv, monkeypatch
):
    """URL + auth shape must match Greenhouse's documented endpoint.

    The earlier draft posted to ``/v1/applications`` (no such endpoint —
    probed live: 401) with no auth and a ``file://`` resume URL.
    """
    monkeypatch.setenv("GREENHOUSE_JOB_BOARD_API_KEY", "secret-key")
    (jid,) = seed(db, [{
        "title": "CTO", "company": "Acme", "status": "approved", "ats_type": "greenhouse",
        "career_url": "https://boards.greenhouse.io/acme",
        "url": "https://boards.greenhouse.io/acme/jobs/424242",
        "cv_path": cv,
    }])
    seen: Dict[str, Any] = {}

    def _fake_post(url, payload, headers=None, timeout=30):
        seen.update(url=url, payload=payload, headers=headers or {})
        return 200, "{}"

    monkeypatch.setattr(orch, "_http_post", _fake_post)

    out = make(db, rate_file, profile).apply_job(jid)
    assert out["status"] == "applied"
    assert seen["url"] == "https://boards-api.greenhouse.io/v1/boards/acme/jobs/424242"
    # Basic auth with the board key is mandatory — assert it is present.
    assert seen["headers"].get("Authorization", "").startswith("Basic ")
    assert seen["payload"]["first_name"] == "Lars"
    assert seen["payload"]["email"] == profile.email
    # Resume travels as base64 + filename, never a file:// URL.
    assert "resume_content" in seen["payload"]
    assert seen["payload"]["resume_content_filename"] == "cv.pdf"
    assert not any(str(v).startswith("file://") for v in seen["payload"].values())

    row = read_row(db, jid)
    assert row["status"] == "submitted"
    assert row["applied_at"]


def test_lever_posts_multipart_to_site_scoped_endpoint(
    db, rate_file, profile, cv, monkeypatch
):
    """Lever needs the ``{site}`` segment and a multipart resume.

    The earlier draft omitted ``{site}`` and posted JSON. Probed live, that
    shape returns ``400 {"ok":false}``.
    """
    monkeypatch.setenv("LEVER_API_KEY", "lever-key")
    (jid,) = seed(db, [{
        "title": "CTO", "company": "Acme", "status": "approved", "ats_type": "lever",
        "career_url": "https://jobs.lever.co/acme",
        "url": ("https://jobs.lever.co/acme/"
                "8a1f2b3c-4d5e-6f70-8192-a3b4c5d6e7f8"),
        "cv_path": cv,
    }])
    seen: Dict[str, Any] = {}

    def _fake_multipart(url, fields, file_field="", file_path="", timeout=60):
        seen.update(url=url, fields=fields, file_field=file_field, file_path=file_path)
        return 201, "{}"

    monkeypatch.setattr(orch, "_http_multipart", _fake_multipart)

    out = make(db, rate_file, profile).apply_job(jid)
    assert out["status"] == "applied"
    assert seen["url"].startswith(
        "https://api.lever.co/v0/postings/acme/"
        "8a1f2b3c-4d5e-6f70-8192-a3b4c5d6e7f8"
    )
    assert "key=lever-key" in seen["url"]
    assert seen["file_field"] == "resume"
    assert seen["file_path"] == cv
    assert seen["fields"]["name"] == "Lars Zimmermann"
    assert read_row(db, jid)["status"] == "submitted"


def test_ashby_posts_to_board_scoped_endpoint(db, rate_file, profile, monkeypatch):
    """Ashby apply is board-scoped; the bare /job-board/apply path 401s."""
    monkeypatch.setenv("ASHBY_API_KEY", "ashby-key")
    (jid,) = seed(db, [{
        "title": "CTO", "company": "Acme", "status": "approved", "ats_type": "ashby",
        "career_url": "https://jobs.ashbyhq.com/acme",
        "url": ("https://jobs.ashbyhq.com/acme/"
                "9b2c3d4e-5f60-7182-93a4-b5c6d7e8f901"),
    }])
    seen: Dict[str, Any] = {}

    def _fake_post(url, payload, headers=None, timeout=30):
        seen.update(url=url, payload=payload)
        return 200, "{}"

    monkeypatch.setattr(orch, "_http_post", _fake_post)

    out = make(db, rate_file, profile).apply_job(jid)
    assert out["status"] == "applied"
    assert seen["url"].startswith(
        "https://api.ashbyhq.com/posting-api/job-board/acme/apply"
    )
    assert "key=ashby-key" in seen["url"]
    assert seen["payload"]["jobId"] == "9b2c3d4e-5f60-7182-93a4-b5c6d7e8f901"


# ---------------------------------------------------------------------------
# Missing employer keys downgrade to the browser channel (the real default)
# ---------------------------------------------------------------------------


def test_missing_greenhouse_key_downgrades_to_browser_and_pauses(
    db, rate_file, profile, monkeypatch
):
    """No employer key is the REAL default — degrade, never hard-fail."""
    (jid,) = seed(db, [{
        "title": "CTO", "company": "Acme", "status": "approved", "ats_type": "greenhouse",
        "career_url": "https://boards.greenhouse.io/acme",
        "url": "https://boards.greenhouse.io/acme/jobs/424242",
    }])
    called: Dict[str, Any] = {}

    def _fake_browser(job, prof, platform, cookies, headless=False):
        called["platform"] = platform
        return _pause_browser(platform=platform)

    monkeypatch.setattr(orch, "_fill_browser_channel", _fake_browser)

    out = make(db, rate_file, profile).apply_job(jid)
    assert called["platform"] == "greenhouse"   # fell back rather than failing
    assert out["status"] == "paused"
    assert read_row(db, jid)["status"] == "needs_human"


def test_missing_lever_key_downgrades_to_browser(db, rate_file, profile, monkeypatch):
    (jid,) = seed(db, [{
        "title": "CTO", "company": "Acme", "status": "approved", "ats_type": "lever",
        "career_url": "https://jobs.lever.co/acme",
        "url": "https://jobs.lever.co/acme/8a1f2b3c-4d5e-6f70-8192-a3b4c5d6e7f8",
    }])
    monkeypatch.setattr(orch, "_fill_browser_channel", _pause_browser)
    out = make(db, rate_file, profile).apply_job(jid)
    assert out["status"] == "paused"


# ---------------------------------------------------------------------------
# Browser channel — fills and pauses, NEVER submits
# ---------------------------------------------------------------------------


class _FakePage:
    """Minimal Playwright page double that records every call."""

    def __init__(self):
        self.goto_calls: List[str] = []
        self.filled: Dict[str, str] = {}
        self.clicked: List[str] = []   # must stay EMPTY
        self.screenshots: List[str] = []

    def goto(self, url, timeout=None, wait_until=None):
        self.goto_calls.append(url)

    def fill(self, selector, value, timeout=None):
        self.filled[selector] = value

    def screenshot(self, path=None, **kw):
        self.screenshots.append(path)


class _Cred:
    def __init__(self, cookies):
        self._cookies = cookies

    def to_playwright_cookies(self):
        return self._cookies


def _raise_no_cred():
    raise RuntimeError("no credential profile for user_id 'lars'")


def test_xing_browser_fills_then_pauses_without_clicking_submit(
    db, rate_file, profile, monkeypatch
):
    """XING must fill and stop. No click on anything, ever."""
    (jid,) = seed(db, [{
        "title": "CTO", "company": "Acme", "status": "approved", "ats_type": "xing",
        "url": "https://www.xing.com/jobs/acme/cto",
    }])
    page = _FakePage()
    cookies_seen: List[Any] = []

    def _fake_open(cookies, headless=False):
        cookies_seen.append(cookies)
        return {"playwright": None, "browser": None, "context": None, "page": page}

    monkeypatch.setattr(orch, "_open_browser", _fake_open)
    monkeypatch.setattr(orch, "get_credential", lambda u, p: _Cred(
        [{"name": "session", "value": "x", "domain": ".xing.com"}]))

    out = make(db, rate_file, profile).apply_job(jid)
    assert out["status"] == "paused"
    assert page.goto_calls == ["https://www.xing.com/jobs/acme/cto"]
    assert page.filled, "expected some fields to be filled"
    assert cookies_seen and cookies_seen[0][0]["name"] == "session"
    # THE invariant: nothing was clicked.
    assert page.clicked == []
    row = read_row(db, jid)
    assert row["status"] == "needs_human"
    assert row["applied_at"] is None   # never marked applied


def test_browser_channel_without_cookies_pauses_with_actionable_error(
    db, rate_file, profile, monkeypatch
):
    """No session => pause and say how to fix it. Do not guess at fields."""
    (jid,) = seed(db, [{
        "title": "CTO", "company": "Acme", "status": "approved", "ats_type": "linkedin",
        "url": "https://www.linkedin.com/jobs/view/123",
    }])
    monkeypatch.setattr(orch, "get_credential", _raise_no_cred)
    out = make(db, rate_file, profile).apply_job(jid)
    assert out["status"] == "paused"
    assert "captainauth" in (out["error"] or "")
    assert read_row(db, jid)["applied_at"] is None


def test_browser_launch_failure_does_not_mark_applied(
    db, rate_file, profile, monkeypatch
):
    """A broken browser install must not be recorded as a submission."""
    (jid,) = seed(db, [{
        "title": "CTO", "company": "Acme", "status": "approved", "ats_type": "workday",
        "url": "https://acme.wd5.myworkdaysite.com/en-US/x",
    }])
    monkeypatch.setattr(orch, "get_credential", lambda u, p: _Cred(
        [{"name": "s", "value": "x", "domain": ".myworkdaysite.com"}]))
    monkeypatch.setattr(orch, "_open_browser",
                        lambda cookies, headless=False: (_ for _ in ()).throw(
                            RuntimeError("playwright is not installed")))

    out = make(db, rate_file, profile).apply_job(jid)
    assert out["status"] == "paused"
    assert read_row(db, jid)["applied_at"] is None


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_daily_cap_refuses_the_submission_after_the_limit(
    db, rate_file, profile, monkeypatch
):
    """cap=2 => the 3rd attempt is refused and does not consume the counter."""
    monkeypatch.setattr(orch, "_fill_browser_channel", _ok_browser)
    ids = seed(db, [
        {"title": f"CTO {i}", "company": "Acme", "status": "approved", "ats_type": "xing",
         "url": f"https://www.xing.com/jobs/acme/{i}"}
        for i in range(3)
    ])
    attempted = make(db, rate_file, profile, daily_cap=2).apply_all_pending()

    assert attempted == 2, "third job must not be attempted"
    assert orch._user_today_count(orch._read_rate_limit(rate_file), "lars") == 2
    assert read_row(db, ids[0])["status"] == "submitted"
    assert read_row(db, ids[1])["status"] == "submitted"
    assert read_row(db, ids[2])["status"] == "approved"
    assert read_row(db, ids[2])["applied_at"] is None


def test_daily_cap_is_scoped_per_user_and_resets_on_a_new_day(rate_file):
    """One user hitting his cap must not affect another; yesterday != today."""
    today = orch._today_iso()
    state = {"version": 1, "users": {
        "lars": {today: {"count": 20}},
        "other": {today: {"count": 1}},
        "yest": {"2000-01-01": {"count": 99}},
    }}
    Path(rate_file).write_text(json.dumps(state))

    assert orch._user_today_count(state, "lars", today) == 20
    assert orch._user_today_count(state, "other", today) == 1
    assert orch._user_today_count(state, "yest", today) == 0  # new day => fresh

    o = orch.ApplyOrchestrator(user_id="lars", db_path="x",
                               rate_limit_path=rate_file, daily_cap=20)
    assert o._check_daily_cap() is not None     # lars is capped
    o2 = orch.ApplyOrchestrator(user_id="other", db_path="x",
                                rate_limit_path=rate_file, daily_cap=20)
    assert o2._check_daily_cap() is None        # other is fine
    o3 = orch.ApplyOrchestrator(user_id="yest", db_path="x",
                                rate_limit_path=rate_file, daily_cap=20)
    assert o3._check_daily_cap() is None        # new day resets


def test_yesterdays_cap_does_not_block_today(db, rate_file, profile, monkeypatch):
    """End-to-end: a full 20 yesterday must not refuse a job today."""
    monkeypatch.setenv("GREENHOUSE_JOB_BOARD_API_KEY", "k")
    yesterday = "2000-01-01"
    Path(rate_file).write_text(json.dumps(
        {"version": 1, "users": {"lars": {yesterday: {"count": 20}}}}))
    (jid,) = seed(db, [{
        "title": "CTO", "company": "Acme", "status": "approved", "ats_type": "greenhouse",
        "career_url": "https://boards.greenhouse.io/acme",
        "url": "https://boards.greenhouse.io/acme/jobs/1",
    }])
    monkeypatch.setattr(orch, "_http_post", lambda *a, **k: (200, "{}"))

    out = make(db, rate_file, profile, daily_cap=20).apply_job(jid)
    assert out["status"] == "applied"
    data = json.loads(Path(rate_file).read_text())
    assert data["users"]["lars"][orch._today_iso()]["count"] == 1
    assert data["users"]["lars"][yesterday]["count"] == 20  # history preserved


def test_rate_limit_file_is_atomic_and_survives_corruption(rate_file):
    """A truncated counter file must not crash, and must leave no temp files."""
    Path(rate_file).write_text('{"users": {"lars": ')  # truncated JSON
    assert orch._read_rate_limit(rate_file) == {"version": 1, "users": {}}

    orch._bump_user_today_count(rate_file, "lars", 7)
    data = json.loads(Path(rate_file).read_text())
    today = data["users"]["lars"][orch._today_iso()]
    assert today["count"] == 1
    assert today["last_job_id"] == 7
    # No temp files left behind.
    assert [p.name for p in Path(rate_file).parent.iterdir()] == [Path(rate_file).name]


def test_failed_apply_does_not_consume_daily_cap(db, rate_file, profile, monkeypatch):
    """A hard failure is not a submission — it must not burn the cap."""
    monkeypatch.setenv("GREENHOUSE_JOB_BOARD_API_KEY", "k")
    (jid,) = seed(db, [{
        "title": "CTO", "company": "Acme", "status": "approved", "ats_type": "greenhouse",
        "career_url": "https://boards.greenhouse.io/acme",
        "url": "https://boards.greenhouse.io/acme/jobs/1",
    }])
    monkeypatch.setattr(orch, "_http_post", lambda *a, **k: (500, "boom"))

    out = make(db, rate_file, profile).apply_job(jid)
    assert out["status"] == "failed"
    assert orch._user_today_count(orch._read_rate_limit(rate_file), "lars") == 0


def test_paused_apply_consumes_daily_cap(db, rate_file, profile, monkeypatch):
    """A filled form is a real attempt on that board — it counts."""
    (jid,) = seed(db, [{
        "title": "CTO", "company": "Acme", "status": "approved", "ats_type": "xing",
        "url": "https://www.xing.com/jobs/acme/1",
    }])
    monkeypatch.setattr(orch, "_fill_browser_channel", _pause_browser)
    out = make(db, rate_file, profile).apply_job(jid)
    assert out["status"] == "paused"
    assert orch._user_today_count(orch._read_rate_limit(rate_file), "lars") == 1


# ---------------------------------------------------------------------------
# Failure handling + retry
# ---------------------------------------------------------------------------


def test_api_failure_is_recorded_and_retried_on_the_next_run(
    db, rate_file, profile, monkeypatch
):
    """Failure => status='failed', applied_at NULL so a retry is possible."""
    monkeypatch.setenv("GREENHOUSE_JOB_BOARD_API_KEY", "k")
    (jid,) = seed(db, [{
        "title": "CTO", "company": "Acme", "status": "approved", "ats_type": "greenhouse",
        "career_url": "https://boards.greenhouse.io/acme",
        "url": "https://boards.greenhouse.io/acme/jobs/424242",
    }])
    monkeypatch.setattr(orch, "_http_post", lambda *a, **k: (500, "boom"))

    out = make(db, rate_file, profile).apply_job(jid)
    assert out["status"] == "failed"
    row = read_row(db, jid)
    assert row["status"] == "failed"
    assert row["applied_at"] is None

    # Re-approve (the dashboard's retry path) and try again — now it works.
    conn = sqlite3.connect(db)
    conn.execute("UPDATE jobs SET status='approved' WHERE id=?", (jid,))
    conn.commit()
    conn.close()
    monkeypatch.setattr(orch, "_http_post", lambda *a, **k: (200, "{}"))

    out2 = make(db, rate_file, profile).apply_job(jid)
    assert out2["status"] == "applied"
    assert read_row(db, jid)["status"] == "submitted"


def test_transport_error_fails_without_marking_applied(
    db, rate_file, profile, monkeypatch
):
    """A network outage is a failure, not a submission."""
    monkeypatch.setenv("GREENHOUSE_JOB_BOARD_API_KEY", "k")
    (jid,) = seed(db, [{
        "title": "CTO", "company": "Acme", "status": "approved", "ats_type": "greenhouse",
        "career_url": "https://boards.greenhouse.io/acme",
        "url": "https://boards.greenhouse.io/acme/jobs/1",
    }])
    monkeypatch.setattr(orch, "_http_post", lambda *a, **k: (0, "URLError: down"))

    out = make(db, rate_file, profile).apply_job(jid)
    assert out["status"] == "failed"
    assert read_row(db, jid)["applied_at"] is None


def test_missing_posting_id_fails_cleanly(db, rate_file, profile, monkeypatch):
    """No parseable job id => clear error, no crash, nothing marked applied."""
    monkeypatch.setenv("GREENHOUSE_JOB_BOARD_API_KEY", "k")
    (jid,) = seed(db, [{
        "title": "CTO", "company": "Acme", "status": "approved", "ats_type": "greenhouse",
        "career_url": "", "url": "",
    }])
    out = make(db, rate_file, profile).apply_job(jid)
    assert out["status"] == "failed"
    assert "missing" in (out["error"] or "").lower()
    assert read_row(db, jid)["applied_at"] is None


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------


def test_discord_fires_on_success_and_failure(db, rate_file, profile, monkeypatch):
    """Every outcome notifies, and the webhook URL never leaks into a message."""
    msgs: List[str] = []
    monkeypatch.setattr(orch, "_discord_notify",
                        lambda hook, msg: (msgs.append(msg), True)[1])
    monkeypatch.setenv("GREENHOUSE_JOB_BOARD_API_KEY", "k")
    ok_id, fail_id = seed(db, [
        {"title": "Good", "company": "Acme", "status": "approved", "ats_type": "greenhouse",
         "career_url": "https://boards.greenhouse.io/acme",
         "url": "https://boards.greenhouse.io/acme/jobs/1"},
        {"title": "Bad", "company": "Acme", "status": "approved", "ats_type": "greenhouse",
         "career_url": "https://boards.greenhouse.io/acme",
         "url": "https://boards.greenhouse.io/acme/jobs/2"},
    ])

    calls = {"n": 0}

    def _post(*a, **k):
        calls["n"] += 1
        return (200, "{}") if calls["n"] == 1 else (500, "boom")

    monkeypatch.setattr(orch, "_http_post", _post)

    o = make(db, rate_file, profile, discord_webhook="https://discord/secret-hook")
    o.apply_job(ok_id)
    o.apply_job(fail_id)

    assert any("✅" in m for m in msgs)
    assert any("❌" in m for m in msgs)
    assert all("secret-hook" not in m for m in msgs)


def test_broken_discord_webhook_never_breaks_the_apply(
    db, rate_file, profile, monkeypatch
):
    """A broken webhook must not turn a successful apply into an exception."""
    monkeypatch.setenv("GREENHOUSE_JOB_BOARD_API_KEY", "k")
    (jid,) = seed(db, [{
        "title": "CTO", "company": "Acme", "status": "approved", "ats_type": "greenhouse",
        "career_url": "https://boards.greenhouse.io/acme",
        "url": "https://boards.greenhouse.io/acme/jobs/1",
    }])
    monkeypatch.setattr(orch, "_http_post", lambda *a, **k: (200, "{}"))
    monkeypatch.setattr(orch, "_discord_notify",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("network down")))

    out = make(db, rate_file, profile, discord_webhook="https://discord/x").apply_job(jid)
    assert out["status"] == "applied"
    assert read_row(db, jid)["status"] == "submitted"


# ---------------------------------------------------------------------------
# Classification, dry-run, schema degradation
# ---------------------------------------------------------------------------


def test_classification_prefers_ats_over_scraped_source():
    """A Greenhouse role scraped off LinkedIn applies through Greenhouse."""
    assert orch._classify({"ats_type": "greenhouse", "source": "linkedin"}) == \
        ("api", "greenhouse")
    assert orch._classify({"source": "xing", "ats_type": "workday"}) == \
        ("browser", "workday")
    # No ats_type: fall back to the URL host.
    assert orch._classify({"url": "https://boards.greenhouse.io/x/jobs/9"}) == \
        ("api", "greenhouse")
    assert orch._classify({"url": "https://www.xing.com/jobs/x/1"}) == \
        ("browser", "xing")
    # Neither: refuse.
    assert orch._classify({"source": "stepstone", "url": "https://stepstone.de/x"})[0] \
        == "unknown"


def test_dry_run_has_no_side_effects(db, rate_file, profile):
    """--dry-run must not write status, applied_at, or the cap counter."""
    (jid,) = seed(db, [{"title": "CTO", "company": "Acme", "status": "approved",
                        "ats_type": "greenhouse"}])
    out = make(db, rate_file, profile, dry_run=True).apply_job(jid)
    assert out["status"] == "dry_run"
    assert out["platform"] == "greenhouse"
    row = read_row(db, jid)
    assert row["status"] == "approved"      # untouched
    assert row["applied_at"] is None
    assert not Path(rate_file).exists()     # no counter written


def test_works_against_the_slim_self_host_schema(slim_db, rate_file, profile, monkeypatch):
    """Self-host DBs lack ats_type/score/applied_channel — degrade, don't crash."""
    (jid,) = seed(slim_db, [{"title": "CTO", "company": "Acme", "status": "approved",
                             "source": "xing",
                             "url": "https://www.xing.com/jobs/acme/1"}])
    monkeypatch.setattr(orch, "_fill_browser_channel", _ok_browser)

    o = make(slim_db, rate_file, profile)
    assert o.pending_count() == 1
    out = o.apply_job(jid)
    assert out["status"] == "applied"
    assert read_row(slim_db, jid)["status"] == "submitted"


def test_applied_channel_is_recorded_when_the_column_exists(
    db, rate_file, profile, monkeypatch
):
    """applied_channel is the audit trail for which submit path fired."""
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE jobs ADD COLUMN applied_channel TEXT")
    conn.commit()
    conn.close()

    monkeypatch.setenv("GREENHOUSE_JOB_BOARD_API_KEY", "k")
    (jid,) = seed(db, [{
        "title": "CTO", "company": "Acme", "status": "approved", "ats_type": "greenhouse",
        "career_url": "https://boards.greenhouse.io/acme",
        "url": "https://boards.greenhouse.io/acme/jobs/1",
    }])
    monkeypatch.setattr(orch, "_http_post", lambda *a, **k: (200, "{}"))
    out = make(db, rate_file, profile).apply_job(jid)
    assert out["status"] == "applied"
    assert read_row(db, jid)["applied_channel"] == "api"


def test_missing_job_id_fails_cleanly(db, rate_file, profile):
    out = make(db, rate_file, profile).apply_job(9999)
    assert out["ok"] is False
    assert out["status"] == "failed"
    assert out["error"] == "job not found"


def test_apply_all_pending_is_a_noop_with_nothing_approved(db, rate_file, profile):
    seed(db, [{"title": "New", "company": "Acme", "status": "new"}])
    o = make(db, rate_file, profile)
    assert o.pending_count() == 0
    assert o.apply_all_pending() == 0


# ---------------------------------------------------------------------------
# URL parsing helpers
# ---------------------------------------------------------------------------


def test_posting_id_and_board_token_parsing():
    """Numeric Greenhouse ids and UUID Lever/Ashby ids both parse."""
    gh = {"career_url": "https://boards.greenhouse.io/acme",
          "url": "https://boards.greenhouse.io/acme/jobs/424242"}
    assert orch._board_token(gh) == "acme"
    assert orch._posting_id(gh) == "424242"

    lev = {"career_url": "https://jobs.lever.co/acme",
           "url": "https://jobs.lever.co/acme/8a1f2b3c-4d5e-6f70-8192-a3b4c5d6e7f8"}
    assert orch._board_token(lev) == "acme"
    assert orch._posting_id(lev) == "8a1f2b3c-4d5e-6f70-8192-a3b4c5d6e7f8"

    ash = {"career_url": "https://jobs.ashbyhq.com/acme",
           "url": "https://jobs.ashbyhq.com/acme/9b2c3d4e-5f60-7182-93a4-b5c6d7e8f901"}
    assert orch._board_token(ash) == "acme"
    assert orch._posting_id(ash) == "9b2c3d4e-5f60-7182-93a4-b5c6d7e8f901"

    # Unusable input degrades to '' rather than raising.
    assert orch._posting_id({"url": ""}) == ""
    assert orch._board_token({}) == ""


def test_credentials_are_never_logged_on_failure(caplog, db, rate_file, profile, monkeypatch):
    """A credential-store failure must not leak cookie material into logs."""
    import logging

    def _leaky(user, platform):
        raise RuntimeError("boom for session=SECRETVALUE")

    monkeypatch.setattr(orch, "get_credential", _leaky)
    (jid,) = seed(db, [{"title": "CTO", "company": "Acme", "status": "approved",
                        "ats_type": "linkedin",
                        "url": "https://www.linkedin.com/jobs/view/1"}])

    with caplog.at_level(logging.DEBUG, logger="apply.orchestrator"):
        out = make(db, rate_file, profile).apply_job(jid)

    assert out["status"] == "paused"
    assert "SECRETVALUE" not in caplog.text