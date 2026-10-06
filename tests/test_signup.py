"""Tests for the Phase 1.2 signup + verify + forgot/reset flow.

Covers:
  - signup happy path (returns user_id + verify_url)
  - signup bad email  → 400 invalid_email
  - signup weak password → 200 with warnings list
  - signup duplicate email → 409 email_taken
  - signup rate limit (5 per IP per hour → 429)
  - verify happy path (returns session cookie)
  - verify wrong code (max 5 attempts → invalidated)
  - verify expired code → 400 expired
  - verify already verified (idempotent 200)
  - forgot always returns 200 (no enumeration)
  - reset happy path (token cleared after use)
  - reset expired token → 400
  - reset bad token → 400
  - reset weak password → 400
  - login with real user after verify
  - login with unverified user → 403
  - login still works for bootstrap admin "lars" (unchanged)

Each test resets the rate-limit bucket and points users_db at a temp file
so we don't pollute the real data/users.db.
"""
from __future__ import annotations

import base64
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_users_db(tmp_path, monkeypatch):
    """Point users_db at a throwaway file. Reset before AND after each test."""
    from src.db import users_db

    db = tmp_path / "users.db"
    monkeypatch.setattr(users_db, "USERS_DB", db)
    # Re-create schema on the new file
    users_db.connect_users_db().close()  # creates file + schema
    yield db


@pytest.fixture
def reset_rate_limit():
    """Clear the in-memory rate-limit bucket."""
    from src.web import rate_limit
    rate_limit._reset_for_tests()
    yield
    rate_limit._reset_for_tests()


@pytest.fixture
def temp_jobs_db(tmp_path):
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
def app(tmp_path, temp_jobs_db, temp_users_db, monkeypatch):
    from src.web import app as app_mod
    monkeypatch.setattr(app_mod, "DB_PATH", temp_jobs_db)
    empty_batches = tmp_path / "batches"
    empty_batches.mkdir()
    monkeypatch.setattr(app_mod, "BATCHES_DIR", empty_batches)
    return app_mod.app


@pytest.fixture
def client(app):
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _signup(client: TestClient, email: str = "alice@example.com",
            password: str = "supersecret123", name: str = "Alice"):
    return client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": password, "name": name},
    )


def _peek_verify_code(db_path: Path, email: str) -> str:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT verify_code FROM users WHERE email = ?", (email,)
    ).fetchone()
    conn.close()
    assert row is not None, f"no user row for {email}"
    return row["verify_code"]


def _expire_verify_code(db_path: Path, email: str) -> None:
    """Backdate the verify_expires_at so the code is expired."""
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE users SET verify_expires_at = ? WHERE email = ?",
        (past, email),
    )
    conn.commit()
    conn.close()


def _peek_reset_token(db_path: Path, email: str) -> str:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT reset_token FROM users WHERE email = ?", (email,)
    ).fetchone()
    conn.close()
    return row["reset_token"]


def _expire_reset_token(db_path: Path, email: str) -> None:
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE users SET reset_expires_at = ? WHERE email = ?",
        (past, email),
    )
    conn.commit()
    conn.close()


# ===========================================================================
# signup
# ===========================================================================

def test_signup_happy_path(client, temp_users_db, reset_rate_limit):
    r = _signup(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert "user_id" in body
    assert body["verify_url"].startswith("/app/verify?user_id=")
    assert body["warnings"] == []  # strong password

    # User row exists, unverified
    conn = sqlite3.connect(str(temp_users_db))
    row = conn.execute(
        "SELECT email, verified, password_hash FROM users WHERE id = ?",
        (body["user_id"],),
    ).fetchone()
    conn.close()
    assert row[0] == "alice@example.com"
    assert row[1] == 0
    assert row[2].startswith("pbkdf2$")


def test_signup_bad_email_400(client, reset_rate_limit):
    for bad in ["", "no-at-sign", "two@@signs.com", "user@", "@no-local.com",
                "spaces in@email.com"]:
        r = client.post(
            "/api/v1/auth/signup",
            json={"email": bad, "password": "supersecret123"},
        )
        assert r.status_code == 400, f"{bad!r} should be rejected"
        assert r.json()["error"] == "invalid_email"


def test_signup_email_normalised_to_lowercase(client, temp_users_db, reset_rate_limit):
    r = _signup(client, email="Alice@Example.COM")
    assert r.status_code == 200
    conn = sqlite3.connect(str(temp_users_db))
    row = conn.execute("SELECT email FROM users").fetchone()
    conn.close()
    assert row[0] == "alice@example.com"


def test_signup_weak_password_warns_but_succeeds(client, temp_users_db, reset_rate_limit):
    r = _signup(client, password="short")
    assert r.status_code == 200
    assert "password_too_short" in r.json()["warnings"]


def test_signup_duplicate_email_409(client, temp_users_db, reset_rate_limit):
    r1 = _signup(client, email="dup@example.com")
    assert r1.status_code == 200
    r2 = _signup(client, email="dup@example.com")
    assert r2.status_code == 409
    assert r2.json()["error"] == "email_taken"

    # Case-insensitive duplicate detection
    r3 = _signup(client, email="DUP@example.COM")
    assert r3.status_code == 409


def test_signup_rate_limit_429(client, reset_rate_limit):
    """5 signups OK; 6th must hit the bucket."""
    for i in range(5):
        r = _signup(client, email=f"u{i}@example.com")
        assert r.status_code == 200, f"signup {i+1} should succeed: {r.text}"
    r6 = _signup(client, email="u6@example.com")
    assert r6.status_code == 429
    body = r6.json()
    assert body["error"] == "rate_limited"
    assert body["retry_after"] > 0
    assert "retry-after" in {k.lower() for k in r6.headers.keys()}


def test_signup_does_not_count_failed_signups_against_bucket(client, reset_rate_limit):
    """Failed signups (bad email / duplicate) should not consume a slot."""
    for _ in range(10):
        client.post(
            "/api/v1/auth/signup",
            json={"email": "nope", "password": "okpasswordok"},
        )
    # Now do 5 successful signups — should still be allowed.
    for i in range(5):
        r = _signup(client, email=f"keep{i}@example.com")
        assert r.status_code == 200


# ===========================================================================
# verify
# ===========================================================================

def test_verify_happy_path_sets_session(client, temp_users_db, reset_rate_limit):
    r = _signup(client)
    uid = r.json()["user_id"]
    code = _peek_verify_code(temp_users_db, "alice@example.com")

    r = client.post(
        "/api/v1/auth/verify",
        json={"user_id": uid, "code": code},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert r.json()["user"]["email"] == "alice@example.com"
    assert r.json()["user"]["verified"] is True

    # Session cookie is set
    from src.web.auth import COOKIE_NAME
    assert COOKIE_NAME in r.cookies

    # /me now resolves to the new user
    r2 = client.get("/api/v1/auth/me", cookies=r.cookies)
    assert r2.status_code == 200
    assert r2.json()["email"] == "alice@example.com"


def test_verify_wrong_code_increments_attempts(client, temp_users_db, reset_rate_limit):
    _signup(client)
    uid = _signup(client, email="bob@example.com").json()["user_id"]

    r = client.post("/api/v1/auth/verify", json={"user_id": uid, "code": "000000"})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_code"

    # Attempts column bumped to 1
    conn = sqlite3.connect(str(temp_users_db))
    row = conn.execute(
        "SELECT verify_attempts, verify_invalidated FROM users WHERE id = ?",
        (uid,),
    ).fetchone()
    conn.close()
    assert row[0] == 1
    assert row[1] == 0


def test_verify_max_attempts_invalidates_code(client, temp_users_db, reset_rate_limit):
    uid = _signup(client, email="carol@example.com").json()["user_id"]
    # 5 wrong tries
    for i in range(5):
        r = client.post("/api/v1/auth/verify", json={"user_id": uid, "code": "000000"})
        assert r.status_code == 400
    # 5th attempt flips verify_invalidated=1
    conn = sqlite3.connect(str(temp_users_db))
    row = conn.execute(
        "SELECT verify_invalidated FROM users WHERE id = ?", (uid,),
    ).fetchone()
    conn.close()
    assert row[0] == 1

    # Even the RIGHT code now fails
    code = _peek_verify_code(temp_users_db, "carol@example.com")
    r = client.post("/api/v1/auth/verify", json={"user_id": uid, "code": code})
    assert r.status_code == 400
    assert r.json()["error"] == "too_many_attempts"


def test_verify_expired_code_400(client, temp_users_db, reset_rate_limit):
    r = _signup(client, email="dave@example.com")
    uid = r.json()["user_id"]
    code = _peek_verify_code(temp_users_db, "dave@example.com")
    _expire_verify_code(temp_users_db, "dave@example.com")

    r = client.post("/api/v1/auth/verify", json={"user_id": uid, "code": code})
    assert r.status_code == 400
    assert r.json()["error"] == "expired"


def test_verify_unknown_user_400(client, temp_users_db, reset_rate_limit):
    r = client.post(
        "/api/v1/auth/verify",
        json={"user_id": "00000000-0000-0000-0000-000000000000", "code": "123456"},
    )
    assert r.status_code == 400
    assert r.json()["error"] == "no_pending_verification"


def test_verify_already_verified_is_idempotent(client, temp_users_db, reset_rate_limit):
    r = _signup(client, email="erin@example.com")
    uid = r.json()["user_id"]
    code = _peek_verify_code(temp_users_db, "erin@example.com")
    # First verify → success
    client.post("/api/v1/auth/verify", json={"user_id": uid, "code": code})
    # Second verify with wrong code on the now-clean slot → 200 idempotent
    r = client.post("/api/v1/auth/verify", json={"user_id": uid, "code": "000000"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["already_verified"] is True


# ===========================================================================
# forgot / reset
# ===========================================================================

def test_forgot_existing_email_issues_token(client, temp_users_db, reset_rate_limit):
    _signup(client, email="frank@example.com")

    r = client.post("/api/v1/auth/forgot", json={"email": "frank@example.com"})
    assert r.status_code == 200
    assert r.json()["ok"] is True

    token = _peek_reset_token(temp_users_db, "frank@example.com")
    assert token and len(token) > 20  # url-safe token_urlsafe(32)


def test_forgot_unknown_email_returns_200_no_token(client, temp_users_db, reset_rate_limit):
    """No enumeration — unknown email still returns 200 with no token row."""
    r = client.post("/api/v1/auth/forgot", json={"email": "ghost@nowhere.com"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    # No user row exists, no token either
    conn = sqlite3.connect(str(temp_users_db))
    n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    assert n == 0


def test_reset_happy_path_clears_token(client, temp_users_db, reset_rate_limit):
    _signup(client, email="gina@example.com")
    # Verify so login works after the reset
    code = _peek_verify_code(temp_users_db, "gina@example.com")
    conn = sqlite3.connect(str(temp_users_db))
    uid = conn.execute(
        "SELECT id FROM users WHERE email = ?", ("gina@example.com",)
    ).fetchone()[0]
    conn.close()
    client.post("/api/v1/auth/verify", json={"user_id": uid, "code": code})

    client.post("/api/v1/auth/forgot", json={"email": "gina@example.com"})
    token = _peek_reset_token(temp_users_db, "gina@example.com")

    r = client.post(
        "/api/v1/auth/reset",
        json={"token": token, "new_password": "evenmoresecret123"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # Token is single-use: cleared from the row
    assert _peek_reset_token(temp_users_db, "gina@example.com") is None

    # New password works for login
    r = client.post(
        "/api/v1/auth/login",
        json={"user_id": "gina@example.com", "password": "evenmoresecret123"},
    )
    assert r.status_code == 200


def test_reset_token_one_time_use(client, temp_users_db, reset_rate_limit):
    """Second use of the same token must fail (token was cleared)."""
    _signup(client, email="henry@example.com")
    client.post("/api/v1/auth/forgot", json={"email": "henry@example.com"})
    token = _peek_reset_token(temp_users_db, "henry@example.com")

    r1 = client.post(
        "/api/v1/auth/reset",
        json={"token": token, "new_password": "firstnewpassword"},
    )
    assert r1.status_code == 200
    r2 = client.post(
        "/api/v1/auth/reset",
        json={"token": token, "new_password": "secondnewpassword"},
    )
    assert r2.status_code == 400
    assert r2.json()["error"] == "invalid_or_expired_token"


def test_reset_expired_token_400(client, temp_users_db, reset_rate_limit):
    _signup(client, email="ivy@example.com")
    client.post("/api/v1/auth/forgot", json={"email": "ivy@example.com"})
    token = _peek_reset_token(temp_users_db, "ivy@example.com")
    _expire_reset_token(temp_users_db, "ivy@example.com")

    r = client.post(
        "/api/v1/auth/reset",
        json={"token": token, "new_password": "freshpasswordfresh"},
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_or_expired_token"


def test_reset_bad_token_400(client, reset_rate_limit):
    r = client.post(
        "/api/v1/auth/reset",
        json={"token": "this-is-not-a-real-token", "new_password": "freshpasswordfresh"},
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_or_expired_token"


def test_reset_weak_password_400(client, temp_users_db, reset_rate_limit):
    _signup(client, email="jane@example.com")
    client.post("/api/v1/auth/forgot", json={"email": "jane@example.com"})
    token = _peek_reset_token(temp_users_db, "jane@example.com")

    r = client.post(
        "/api/v1/auth/reset",
        json={"token": token, "new_password": "short"},
    )
    assert r.status_code == 400
    assert r.json()["error"] == "password_too_short"


# ===========================================================================
# login with real users (after verify)
# ===========================================================================

def test_login_with_unverified_real_user_403(client, temp_users_db, reset_rate_limit):
    """User signs up but never verifies → login is blocked."""
    _signup(client, email="kim@example.com")
    r = client.post(
        "/api/v1/auth/login",
        json={"user_id": "kim@example.com", "password": "supersecret123"},
    )
    assert r.status_code == 403
    # The detail string should mention verification
    assert "verif" in r.json()["detail"].lower()


def test_login_with_verified_real_user_200(client, temp_users_db, reset_rate_limit):
    _signup(client, email="leo@example.com")
    uid = None
    # We need the uid from a fresh signup since the first one is consumed
    # for the unverified test above — but signup with same email is 409, so
    # create a second account for this test.
    r = _signup(client, email="leo2@example.com")
    uid = r.json()["user_id"]
    code = _peek_verify_code(temp_users_db, "leo2@example.com")
    client.post("/api/v1/auth/verify", json={"user_id": uid, "code": code})

    r = client.post(
        "/api/v1/auth/login",
        json={"user_id": "leo2@example.com", "password": "supersecret123"},
    )
    assert r.status_code == 200
    assert r.json()["user"]["email"] == "leo2@example.com"
    # Session cookie is set
    from src.web.auth import COOKIE_NAME
    assert COOKIE_NAME in r.cookies


def test_login_wrong_password_for_real_user_401(client, temp_users_db, reset_rate_limit):
    _signup(client, email="mia@example.com")
    uid = _signup(client, email="mia2@example.com").json()["user_id"]
    code = _peek_verify_code(temp_users_db, "mia2@example.com")
    client.post("/api/v1/auth/verify", json={"user_id": uid, "code": code})

    r = client.post(
        "/api/v1/auth/login",
        json={"user_id": "mia2@example.com", "password": "wrong"},
    )
    assert r.status_code == 401


def test_login_bootstrap_admin_still_works(client, reset_rate_limit):
    """Phase 1.1 backwards compat: 'lars' admin still logs in via env password."""
    r = client.post(
        "/api/v1/auth/login",
        json={"user_id": "lars", "password": "captain"},
    )
    assert r.status_code == 200
    assert r.json()["user"]["id"] == "lars"
    assert r.json()["user"]["plan"] == "admin"


# ===========================================================================
# End-to-end: signup → verify → /me → logout
# ===========================================================================

def test_full_signup_verify_flow(client, temp_users_db, reset_rate_limit):
    # 1. Signup
    r = _signup(client, email="nina@example.com", name="Nina")
    assert r.status_code == 200
    uid = r.json()["user_id"]

    # 2. Wrong code → 400
    r = client.post("/api/v1/auth/verify", json={"user_id": uid, "code": "000000"})
    assert r.status_code == 400

    # 3. Right code → 200 + cookie
    code = _peek_verify_code(temp_users_db, "nina@example.com")
    r = client.post("/api/v1/auth/verify", json={"user_id": uid, "code": code})
    assert r.status_code == 200
    cookies = r.cookies

    # 4. /me works
    r = client.get("/api/v1/auth/me", cookies=cookies)
    assert r.status_code == 200
    assert r.json()["email"] == "nina@example.com"
    assert r.json()["name"] == "Nina"

    # 5. logout
    r = client.post("/api/v1/auth/logout", cookies=cookies)
    assert r.status_code == 200

    # 6. /me without cookies → 401
    r = client.get("/api/v1/auth/me")
    assert r.status_code == 401


def test_forgot_reset_login_flow(client, temp_users_db, reset_rate_limit):
    # 1. Signup + verify
    uid = _signup(client, email="olive@example.com").json()["user_id"]
    code = _peek_verify_code(temp_users_db, "olive@example.com")
    client.post("/api/v1/auth/verify", json={"user_id": uid, "code": code})

    # 2. Forgot
    r = client.post("/api/v1/auth/forgot", json={"email": "olive@example.com"})
    assert r.status_code == 200
    token = _peek_reset_token(temp_users_db, "olive@example.com")

    # 3. Reset with weak password → 400
    r = client.post(
        "/api/v1/auth/reset",
        json={"token": token, "new_password": "short"},
    )
    assert r.status_code == 400

    # 4. Reset with strong password → 200
    r = client.post(
        "/api/v1/auth/reset",
        json={"token": token, "new_password": "newSecret12!!"},
    )
    assert r.status_code == 200

    # 5. Login with new password
    r = client.post(
        "/api/v1/auth/login",
        json={"user_id": "olive@example.com", "password": "newSecret12!!"},
    )
    assert r.status_code == 200

    # 6. Old password no longer works
    r = client.post(
        "/api/v1/auth/login",
        json={"user_id": "olive@example.com", "password": "supersecret123"},
    )
    assert r.status_code == 401