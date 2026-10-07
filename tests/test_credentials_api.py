"""
Integration tests for the FastAPI credential routes.

Verifies the HTTP surface added in t_f3d28603:
  - GET  /api/v1/auth/credentials               — secret-free status
  - GET  /api/v1/auth/credentials/platforms     — public catalog
  - POST /api/v1/auth/credentials               — store a cookie jar
  - DELETE /api/v1/auth/credentials/{platform}  — remove one platform
  - Cross-user access is 403, not 200
  - ATS platforms are rejected, not silently stored
  - The response body NEVER contains the cookie value

The /login route is skipped because it requires a real Playwright browser.
"""
from __future__ import annotations

import base64
import sqlite3
from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient


SAMPLE_COOKIES = [
    {"name": "li_at", "value": "PLAIN-MARKER-SECRET-XYZ",
     "domain": ".linkedin.com", "path": "/", "httpOnly": True, "secure": True,
     "sameSite": "None"},
    {"name": "JSESSIONID", "value": "ajax:9999", "domain": ".linkedin.com"},
]


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY, title TEXT, company TEXT, location TEXT,
            url TEXT, career_url TEXT, source TEXT, description TEXT,
            score INTEGER, status TEXT DEFAULT 'new', cv_path TEXT,
            cover_letter_path TEXT, created_at TEXT, updated_at TEXT
        );
    """)
    conn.commit(); conn.close()
    return db


@pytest.fixture
def app(tmp_path: Path, temp_db: Path, tmp_auth_dir, monkeypatch):
    from src.web import app as app_mod
    monkeypatch.setattr(app_mod, "DB_PATH", temp_db)
    monkeypatch.setattr(app_mod, "BATCHES_DIR", tmp_path / "batches")
    return app_mod.app


@pytest.fixture
def tmp_auth_dir(tmp_path, monkeypatch):
    """Per-test auth.json so tests don't pollute the user's real store."""
    d = tmp_path / "auth"
    d.mkdir()
    monkeypatch.setenv("CAPTAIN_AUTH_DIR", str(d))
    monkeypatch.setenv("CAPTAIN_AUTH_KEYFILE", str(d / "master.key"))
    return d


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def auth_headers() -> Dict[str, str]:
    raw = base64.b64encode(b"lars:captain").decode()
    return {"Authorization": f"Basic {raw}"}


# ---------------------------------------------------------------------------
# Catalog (no auth needed)
# ---------------------------------------------------------------------------

def test_platforms_catalog_is_public(client):
    """`/platforms` is the only public route — it exposes the schema, no
    secrets, and lets the UI render the supported-platforms list before the
    user is signed in."""
    r = client.get("/api/v1/auth/credentials/platforms")
    assert r.status_code == 200
    data = r.json()
    names = {p["name"] for p in data["platforms"]}
    assert names == {"linkedin", "xing", "indeed", "workday",
                     "greenhouse", "lever", "ashby"}
    # Greenhouse/Lever/Ashby are flagged as not needing auth.
    for p in data["platforms"]:
        if p["name"] in ("greenhouse", "lever", "ashby"):
            assert p["auth_required"] is False


# ---------------------------------------------------------------------------
# Auth gates
# ---------------------------------------------------------------------------

def test_unauthenticated_get_is_401(client):
    r = client.get("/api/v1/auth/credentials")
    assert r.status_code == 401


def test_unauthenticated_post_is_401(client):
    r = client.post("/api/v1/auth/credentials",
                    json={"platform": "xing", "cookies": []})
    assert r.status_code == 401


def test_unauthenticated_delete_is_401(client):
    r = client.delete("/api/v1/auth/credentials/xing")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Status round-trip + secret redaction
# ---------------------------------------------------------------------------

def test_status_does_not_leak_secret(client, auth_headers):
    r = client.post(
        "/api/v1/auth/credentials",
        headers=auth_headers,
        json={"platform": "linkedin", "cookies": SAMPLE_COOKIES},
    )
    assert r.status_code == 200, r.text
    body = r.text
    # The unique secret marker must not appear anywhere in the response.
    assert "PLAIN-MARKER-SECRET-XYZ" not in body
    assert "ajax:9999" not in body
    # But the structural shape is preserved.
    data = r.json()
    assert data["profile_exists"] is True
    assert data["platform_count"] == 1
    entry = data["platforms"][0]
    assert entry["platform"] == "linkedin"
    assert entry["has_cookies"] is True
    assert entry["cookie_count"] == 2
    # A short fingerprint is exposed — that's the only identity we offer.
    assert len(entry["fingerprint"]) == 8


# ---------------------------------------------------------------------------
# ATS rejection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("platform", ["greenhouse", "lever", "ashby"])
def test_ats_platforms_refuse_credential_storage(client, auth_headers, platform):
    r = client.post(
        "/api/v1/auth/credentials",
        headers=auth_headers,
        json={"platform": platform, "cookies": SAMPLE_COOKIES},
    )
    assert r.status_code == 400, r.text
    assert platform in r.json()["detail"]


def test_unknown_platform_is_400(client, auth_headers):
    r = client.post(
        "/api/v1/auth/credentials",
        headers=auth_headers,
        json={"platform": "nope-this-doesnt-exist",
              "cookies": SAMPLE_COOKIES},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------------

def test_cannot_act_on_another_user(client, auth_headers):
    """Per-user isolation: even a logged-in lars cannot read alice's
    credentials by passing ``?user_id=alice`` — that is the difference
    between per-user and per-instance isolation."""
    r = client.get(
        "/api/v1/auth/credentials",
        headers=auth_headers,
        params={"user_id": "alice"},
    )
    assert r.status_code == 403, r.text
    assert "alice" in r.json()["detail"]
    assert "lars" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Delete round-trip
# ---------------------------------------------------------------------------

def test_delete_removes_platform(client, auth_headers):
    client.post(
        "/api/v1/auth/credentials",
        headers=auth_headers,
        json={"platform": "xing",
              "cookies": [{"name": "sess", "value": "X", "domain": ".xing.com"}]},
    )
    r = client.delete("/api/v1/auth/credentials/xing", headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "user_id": "lars", "deleted": "xing"}
    # And the next GET shows the platform count dropped.
    r = client.get("/api/v1/auth/credentials", headers=auth_headers)
    assert r.json()["platform_count"] == 0


def test_delete_unknown_platform_is_400(client, auth_headers):
    r = client.delete("/api/v1/auth/credentials/totally-made-up",
                      headers=auth_headers)
    assert r.status_code == 400


def test_delete_missing_credential_is_404(client, auth_headers):
    r = client.delete("/api/v1/auth/credentials/xing", headers=auth_headers)
    assert r.status_code == 404