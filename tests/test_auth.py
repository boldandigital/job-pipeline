"""
Tests for the Phase 1.1 session-cookie auth.

Verifies:
  - POST /api/v1/auth/login with bootstrap admin creds → 200 + Set-Cookie
  - GET /api/v1/auth/me with cookie → 200 + user info
  - GET /api/v1/auth/me without cookie → 401
  - GET /api/v1/auth/me with expired cookie → 401
  - POST /api/v1/auth/logout → 200 + clear cookie
  - Jobs endpoints reject unauthenticated requests (even with stale token)
  - Jobs endpoints accept session cookie
  - HTTP Basic legacy auth still works (for existing curl scripts)
"""
import base64
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.web.auth import (
    COOKIE_NAME,
    make_session_cookie,
    _sign,
)
from src.web.app import app as _app  # for re-mounting


# ----- shared fixtures (copied from test_web_app) ------------------------

@pytest.fixture
def temp_db(tmp_path):
    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY, title TEXT, company TEXT, location TEXT,
            url TEXT, career_url TEXT, source TEXT, description TEXT,
            score INTEGER, status TEXT DEFAULT 'new', cv_path TEXT,
            cover_letter_path TEXT, created_at TEXT, updated_at TEXT
        );
        INSERT INTO jobs VALUES (1, 'T', 'TestCo', 'Berlin',
            'https://x.test', '', 'xing', 'desc', 500, 'new', '', '',
            '2026-10-03T00:00:00+00:00', '');
    """)
    conn.commit(); conn.close()
    return db


@pytest.fixture
def app(tmp_path, temp_db, monkeypatch):
    from src.web import app as app_mod
    monkeypatch.setattr(app_mod, "DB_PATH", temp_db)
    empty_batches = tmp_path / "batches"
    empty_batches.mkdir()
    monkeypatch.setattr(app_mod, "BATCHES_DIR", empty_batches)
    return app_mod.app


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def auth_headers():
    raw = base64.b64encode(b"lars:captain").decode()
    return {"Authorization": f"Basic {raw}"}


def _session_cookie_for(user_id: str = "lars") -> dict:
    return {COOKIE_NAME: make_session_cookie(user_id)}


# ----- new auth tests ----------------------------------------------------

def test_login_success_returns_cookie(client):
    r = client.post("/api/v1/auth/login", json={"user_id": "lars", "password": "captain"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["user"]["id"] == "lars"
    assert body["user"]["plan"] == "admin"
    # Cookie must be set
    assert COOKIE_NAME in r.cookies
    assert r.cookies[COOKIE_NAME]


def test_login_wrong_password_401(client):
    r = client.post("/api/v1/auth/login", json={"user_id": "lars", "password": "WRONG"})
    assert r.status_code == 401


def test_login_unknown_user_401(client):
    r = client.post("/api/v1/auth/login", json={"user_id": "ghost", "password": "anything"})
    assert r.status_code == 401


def test_me_with_session_cookie_returns_admin(client):
    r = client.get("/api/v1/auth/me", cookies=_session_cookie_for())
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "lars"
    assert body["plan"] == "admin"
    assert "email" in body


def test_me_without_cookie_401(client):
    r = client.get("/api/v1/auth/me")
    assert r.status_code == 401


def test_me_with_tampered_cookie_401(client):
    """Cookie signature must be verified — tampering invalidates the session."""
    bad_cookie = {COOKIE_NAME: "eyJ1aWQiOiJsYXJzIn0.aaaaaaaaaaaaaaaaaaaaaa"}
    r = client.get("/api/v1/auth/me", cookies=bad_cookie)
    assert r.status_code == 401


def test_me_with_expired_cookie_401(client):
    """Expired tokens (>7 days old) are rejected."""
    expired = _sign({"uid": "lars", "exp": int(time.time()) - 60})
    r = client.get("/api/v1/auth/me", cookies={COOKIE_NAME: expired})
    assert r.status_code == 401


def test_logout_clears_cookie(client):
    """Calling logout returns a Set-Cookie that expires the session."""
    r = client.post("/api/v1/auth/logout", cookies=_session_cookie_for())
    assert r.status_code == 200
    # delete_cookie sets max-age=0 — check the raw Set-Cookie header instead
    set_cookie_header = r.headers.get("set-cookie", "")
    assert COOKIE_NAME in set_cookie_header
    assert "max-age=0" in set_cookie_header.lower() or "expires=" in set_cookie_header.lower()
    # After logout, /me with a new client (no cookies) should be 401
    r2 = client.get("/api/v1/auth/me")
    assert r2.status_code == 401


def test_jobs_endpoint_accepts_session_cookie(client):
    """Logged-in users can fetch their jobs (no Basic auth needed)."""
    r = client.get("/api/jobs", cookies=_session_cookie_for())
    assert r.status_code == 200
    assert isinstance(r.json(), list)
    assert len(r.json()) == 1


def test_jobs_endpoint_rejects_unauthenticated(client):
    r = client.get("/api/jobs")
    assert r.status_code == 401


def test_jobs_endpoint_still_accepts_legacy_basic(client, auth_headers):
    """Phase 1.x backward compat: HTTP Basic still works during migration."""
    r = client.get("/api/jobs", headers=auth_headers)
    assert r.status_code == 200


def test_approve_with_session_cookie_persists_status(client, temp_db):
    """Approve with cookie → DB row updated (per-user path)."""
    r = client.post("/api/jobs/1/approve", cookies=_session_cookie_for())
    assert r.status_code == 200
    assert r.json()["status"] == "approved"
    # Verify in DB
    conn = sqlite3.connect(str(temp_db))
    row = conn.execute("SELECT status FROM jobs WHERE id=1").fetchone()
    assert row[0] == "approved"
    conn.close()


def test_skip_with_session_cookie_persists_status(client, temp_db):
    r = client.post("/api/jobs/1/skip", cookies=_session_cookie_for())
    assert r.status_code == 200
    assert r.json()["status"] == "skipped"
    conn = sqlite3.connect(str(temp_db))
    row = conn.execute("SELECT status FROM jobs WHERE id=1").fetchone()
    assert row[0] == "skipped"
    conn.close()


def test_full_login_flow(client):
    """End-to-end: login → me → fetch jobs → logout → me 401."""
    # 1. Login
    r = client.post("/api/v1/auth/login", json={"user_id": "lars", "password": "captain"})
    assert r.status_code == 200
    cookies = r.cookies
    # 2. /me with cookie
    r = client.get("/api/v1/auth/me", cookies=cookies)
    assert r.status_code == 200
    assert r.json()["id"] == "lars"
    # 3. Fetch jobs with cookie
    r = client.get("/api/jobs", cookies=cookies)
    assert r.status_code == 200
    # 4. Logout
    r = client.post("/api/v1/auth/logout", cookies=cookies)
    assert r.status_code == 200
    # 5. /me with cleared cookie → 401
    r = client.get("/api/v1/auth/me", cookies=r.cookies)
    assert r.status_code == 401
