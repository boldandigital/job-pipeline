"""
Tests for the Phase 2.6 Profile page + CV upload + guided tour + LinkedIn
connector surface area.

Covers:
  - GET /api/profile returns the admin's env-driven identity (read-only)
  - PUT /api/profile for admin → 403
  - GET /api/profile for a real user → returns their row, default empty fields
  - PUT /api/profile for a real user → updates scalar + JSON fields
  - GET /api/profile/cv/meta → has_cv=False when no upload yet
  - POST /api/profile/cv → real multipart upload, ext validation, size limit
  - GET /api/profile/cv/meta after upload → metadata reflected
  - DELETE /api/profile/cv → file removed, metadata cleared
  - POST /api/profile/onboarding → flag flipped, step recorded
  - GET /app/profile → serves the profile HTML
  - GET /api/profile (no auth) → 401
  - GET /api/connectors → all 4 platforms listed
  - GET /api/connectors for admin → admin is excluded from vault lookups
  - POST /api/connectors/{platform}/connect → spawns capture job (501 if binary missing)
  - /app/profile HTML contains the 3 required sections + repeater markup
  - Dashboard HTML contains the tour overlay + a script that hits /api/profile
  - users_db._apply_migrations is idempotent
  - users_db.get_profile(admin) returns is_admin=True, onboarding_complete=1
  - users_db.set_profile(admin) is a no-op
  - CV upload rejects unsupported extensions
  - CV upload rejects oversize files
  - End-to-end: create user → upload CV → set profile → mark onboarded →
    re-fetch profile shows all state
"""
from __future__ import annotations

import base64
import io
import os
import sqlite3
import tempfile
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
    users_db.connect_users_db().close()  # creates file + schema
    yield db


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
    conn.commit()
    conn.close()
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


@pytest.fixture
def auth_headers():
    raw = base64.b64encode(b"lars:captain").decode()
    return {"Authorization": f"Basic {raw}"}


def _create_verified_user(client, email="alice@example.com", password="hunter22long", name="Alice"):
    """Create + verify a signup user; return (uid, session_cookie_value)."""
    from src.db import users_db
    from src.web.auth import hash_password, make_session_cookie
    ph = hash_password(password)
    uid = users_db.create_user(email, ph, name=name)
    users_db.mark_verified(uid)
    return uid, make_session_cookie(uid)


# ---------------------------------------------------------------------------
# 1. /api/profile — admin path
# ---------------------------------------------------------------------------

def test_admin_get_profile_returns_env_identity(client, auth_headers):
    """Admin profile is env-driven; should return is_admin=True."""
    r = client.get("/api/profile", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["is_admin"] is True
    assert body["id"] == "lars"
    assert body["name"]  # non-empty (env LARS_NAME or default)
    assert body["email"]  # non-empty
    # The admin is treated as already onboarded so the tour never fires
    assert body["onboarding_complete"] == 1
    assert body["tour_pending"] is False
    assert body["links"] == []
    assert body["experience"] == []
    assert body["skills"] == []
    # CV fields are cv_upload_path / cv_uploaded_at
    assert body["cv_upload_path"] == ""
    assert body["cv_uploaded_at"] == ""


def test_admin_put_profile_is_403(client, auth_headers):
    """Admins are read-only — the dashboard always treats them that way."""
    r = client.put(
        "/api/profile",
        json={"name": "Hacked", "skills": ["evil"]},
        headers=auth_headers,
    )
    assert r.status_code == 403
    assert "read-only" in r.json()["detail"].lower()


def test_admin_get_profile_via_basic_auth(client, auth_headers):
    """The basic-auth admin path works (no LARS_USER_ID env needed)."""
    r = client.get("/api/profile", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["is_admin"] is True


def test_get_profile_unauthenticated_401(client):
    """No cookie, no env var, no Basic → 401 (not silent admin fallback)."""
    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("LARS_USER_ID", None)
        r = client.get("/api/profile")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# 2. /api/profile — signup user
# ---------------------------------------------------------------------------

def test_user_get_profile_returns_defaults(client):
    """A real user with no profile data yet → all empty fields + onboarding=0."""
    uid, cookie = _create_verified_user(client)
    r = client.get("/api/profile", cookies={"captain_session": cookie})
    assert r.status_code == 200
    body = r.json()
    assert body["is_admin"] is False
    assert body["id"] == uid
    assert body["email"] == "alice@example.com"
    assert body["name"] == "Alice"
    assert body["phone"] == ""
    assert body["headline"] == ""
    assert body["summary"] == ""
    assert body["links"] == []
    assert body["experience"] == []
    assert body["skills"] == []
    assert body["cv_upload_path"] == ""
    assert body["onboarding_complete"] == 0
    assert body["tour_pending"] is True


def test_user_put_profile_updates_scalar_and_json(client):
    """PUT writes both scalar fields and the JSON-encoded list fields."""
    uid, cookie = _create_verified_user(client)
    payload = {
        "name": "Alice Updated",
        "phone": "+32 470 000 000",
        "location": "Brussels, BE",
        "headline": "Senior Engineer",
        "summary": "Three sentences about me.",
        "links": [
            {"kind": "linkedin", "url": "https://linkedin.com/in/alice"},
        ],
        "experience": [
            {"company": "Acme", "role": "Staff", "start": "2020",
             "end": "present", "bullets": "Did things"},
        ],
        "skills": ["Python", "SQL", "Kubernetes"],
    }
    r = client.put("/api/profile", json=payload,
                   cookies={"captain_session": cookie})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Alice Updated"
    assert body["phone"] == "+32 470 000 000"
    assert body["headline"] == "Senior Engineer"
    # Links are normalised to {kind, url} (label is replaced by kind)
    assert body["links"] == [{"kind": "linkedin", "url": "https://linkedin.com/in/alice"}]
    assert body["experience"][0]["company"] == "Acme"
    assert body["experience"][0]["bullets"] == ["Did things"]
    assert body["skills"] == ["Python", "SQL", "Kubernetes"]


def test_user_put_profile_persists_across_gets(client):
    """The values written by PUT are returned by subsequent GET."""
    uid, cookie = _create_verified_user(client)
    client.put(
        "/api/profile",
        json={"headline": "Captain", "skills": ["Go"]},
        cookies={"captain_session": cookie},
    )
    r = client.get("/api/profile", cookies={"captain_session": cookie})
    assert r.json()["headline"] == "Captain"
    assert r.json()["skills"] == ["Go"]


def test_user_put_profile_rejects_link_without_http(client):
    """normalise_links() drops entries whose URL doesn't start with http(s)."""
    uid, cookie = _create_verified_user(client)
    r = client.put(
        "/api/profile",
        json={"links": [
            {"kind": "github", "url": "ftp://nope.example"},
            {"kind": "github", "url": "https://github.com/alice"},
        ]},
        cookies={"captain_session": cookie},
    )
    assert r.status_code == 200
    # Only the http(s) one survives
    urls = [l["url"] for l in r.json()["links"]]
    assert "https://github.com/alice" in urls
    assert "ftp://nope.example" not in urls


# ---------------------------------------------------------------------------
# 3. /api/profile/cv — upload, metadata, delete
# ---------------------------------------------------------------------------

def test_cv_get_returns_404_when_no_cv(client):
    """A fresh user has no CV on file → /api/profile/cv returns 404."""
    uid, cookie = _create_verified_user(client)
    r = client.get("/api/profile/cv", cookies={"captain_session": cookie})
    assert r.status_code == 404


def test_cv_meta_returns_no_cv_initially(client):
    """Metadata endpoint reports has_cv=False before any upload."""
    uid, cookie = _create_verified_user(client)
    r = client.get("/api/profile/cv/meta", cookies={"captain_session": cookie})
    assert r.status_code == 200
    body = r.json()
    assert body["has_cv"] is False
    assert body["filename"] == ""
    assert body["path"] == ""
    assert body["uploaded_at"] == ""


def test_cv_upload_writes_real_file(client, tmp_path):
    """Multipart upload writes a file at data/users/<id>/cv/cv.<ext>."""
    uid, cookie = _create_verified_user(client)
    fake_pdf = b"%PDF-1.4\n%fake content for testing\n%%EOF"
    r = client.post(
        "/api/profile/cv",
        files={"file": ("resume.pdf", io.BytesIO(fake_pdf), "application/pdf")},
        cookies={"captain_session": cookie},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["filename"] == "resume.pdf"
    assert body["path"].endswith("/cv.pdf")
    assert body["size"] == len(fake_pdf)
    # File actually on disk
    full = Path(body["path"])
    assert full.is_file()
    assert full.read_bytes() == fake_pdf


def test_cv_meta_after_upload_returns_metadata(client):
    uid, cookie = _create_verified_user(client)
    client.post(
        "/api/profile/cv",
        files={"file": ("my.docx", io.BytesIO(b"PK\x03\x04 docx-fake"),
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        cookies={"captain_session": cookie},
    )
    r = client.get("/api/profile/cv/meta", cookies={"captain_session": cookie})
    assert r.status_code == 200
    body = r.json()
    assert body["has_cv"] is True
    assert body["filename"] == "cv.docx"  # stored filename is always cv.<ext>
    assert body["path"].endswith("/cv.docx")
    assert body["uploaded_at"]
    assert body["size"] > 0


def test_cv_get_returns_file_bytes_after_upload(client):
    """GET /api/profile/cv (not the meta variant) returns the file as a download."""
    uid, cookie = _create_verified_user(client)
    client.post(
        "/api/profile/cv",
        files={"file": ("a.pdf", io.BytesIO(b"%PDF-fake"), "application/pdf")},
        cookies={"captain_session": cookie},
    )
    r = client.get("/api/profile/cv", cookies={"captain_session": cookie})
    assert r.status_code == 200
    assert r.content == b"%PDF-fake"


def test_cv_upload_rejects_unsupported_extension(client):
    """Phase 2.6 spec: only PDF + DOCX allowed (not .doc, not .exe)."""
    uid, cookie = _create_verified_user(client)
    r = client.post(
        "/api/profile/cv",
        files={"file": ("evil.exe", io.BytesIO(b"x"), "application/octet-stream")},
        cookies={"captain_session": cookie},
    )
    assert r.status_code == 400
    assert "unsupported" in r.json()["detail"].lower() or "extension" in r.json()["detail"].lower()


def test_cv_upload_rejects_oversize_file(client):
    """5 MB cap is enforced server-side."""
    uid, cookie = _create_verified_user(client)
    # 5 MB + 1 byte payload
    big = b"x" * (5 * 1024 * 1024 + 1)
    r = client.post(
        "/api/profile/cv",
        files={"file": ("big.pdf", io.BytesIO(big), "application/pdf")},
        cookies={"captain_session": cookie},
    )
    assert r.status_code == 413
    # The message uses "larger" rather than "too large" — just check for the limit
    assert "limit" in r.json()["detail"].lower() or "large" in r.json()["detail"].lower()


def test_cv_upload_rejects_empty_file(client):
    uid, cookie = _create_verified_user(client)
    r = client.post(
        "/api/profile/cv",
        files={"file": ("empty.pdf", io.BytesIO(b""), "application/pdf")},
        cookies={"captain_session": cookie},
    )
    assert r.status_code == 400
    assert "empty" in r.json()["detail"].lower()


def test_cv_delete_removes_file_and_metadata(client):
    uid, cookie = _create_verified_user(client)
    client.post(
        "/api/profile/cv",
        files={"file": ("me.pdf", io.BytesIO(b"%PDF-x"), "application/pdf")},
        cookies={"captain_session": cookie},
    )
    r = client.delete("/api/profile/cv", cookies={"captain_session": cookie})
    assert r.status_code == 200
    assert "cv.pdf" in r.json()["removed"]
    # And the metadata is gone
    r = client.get("/api/profile/cv/meta", cookies={"captain_session": cookie})
    assert r.json()["has_cv"] is False


def test_cv_upload_replaces_old_file(client):
    """Uploading twice with different extensions only keeps the latest."""
    uid, cookie = _create_verified_user(client)
    client.post(
        "/api/profile/cv",
        files={"file": ("a.pdf", io.BytesIO(b"%PDF-a"), "application/pdf")},
        cookies={"captain_session": cookie},
    )
    client.post(
        "/api/profile/cv",
        files={"file": ("b.docx", io.BytesIO(b"PKdocx"), "application/octet-stream")},
        cookies={"captain_session": cookie},
    )
    r = client.get("/api/profile/cv/meta", cookies={"captain_session": cookie})
    body = r.json()
    assert body["filename"] == "cv.docx"
    assert body["path"].endswith("/cv.docx")
    # Old pdf is gone
    cv_dir = Path(body["path"]).parent
    assert not (cv_dir / "cv.pdf").exists()


def test_admin_cannot_upload_cv(client, auth_headers):
    """Admin's CV lives in config/ — POST is rejected with 403."""
    r = client.post(
        "/api/profile/cv",
        files={"file": ("x.pdf", io.BytesIO(b"%PDF"), "application/pdf")},
        headers=auth_headers,
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# 4. /api/profile/onboarding
# ---------------------------------------------------------------------------

def test_onboarding_post_flips_flag(client):
    uid, cookie = _create_verified_user(client)
    r = client.post(
        "/api/profile/onboarding",
        json={"complete": True, "step": 5},
        cookies={"captain_session": cookie},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    # Now the profile shows complete=1, tour_pending=False
    r = client.get("/api/profile", cookies={"captain_session": cookie})
    assert r.json()["onboarding_complete"] == 1
    assert r.json()["tour_pending"] is False
    assert r.json()["onboarding_step"] == 5


def test_onboarding_post_without_step_uses_default(client):
    uid, cookie = _create_verified_user(client)
    r = client.post(
        "/api/profile/onboarding",
        json={"complete": True},
        cookies={"captain_session": cookie},
    )
    assert r.status_code == 200
    r = client.get("/api/profile", cookies={"captain_session": cookie})
    assert r.json()["onboarding_complete"] == 1
    # Omitting `step` on a complete=true call defaults to TOUR_TOTAL_STEPS
    # (the user reached the end). The endpoint preserves where they got to
    # rather than erasing it back to 0.
    assert r.json()["onboarding_step"] == 5


def test_onboarding_step_is_clamped(client):
    """A crafted step > TOUR_TOTAL_STEPS is clamped server-side."""
    uid, cookie = _create_verified_user(client)
    r = client.post(
        "/api/profile/onboarding",
        json={"complete": False, "step": 99},
        cookies={"captain_session": cookie},
    )
    assert r.status_code == 200
    r = client.get("/api/profile", cookies={"captain_session": cookie})
    # 99 is clamped to 5 (the TOUR_TOTAL_STEPS ceiling)
    assert r.json()["onboarding_step"] == 5


# ---------------------------------------------------------------------------
# 5. /app/profile — page route
# ---------------------------------------------------------------------------

def test_app_profile_returns_html(client, auth_headers):
    r = client.get("/app/profile", headers=auth_headers)
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text
    # Has its own <style> + <script> (no shared CSS with the dashboard)
    assert "<style>" in body
    assert "<script>" in body
    # And the three required sections are present
    assert "Personal" in body
    assert "Experience" in body
    assert "Plan" in body
    assert "CV" in body
    # The form fields are real
    assert 'id="name"' in body
    assert 'id="phone"' in body
    assert 'id="headline"' in body
    assert 'id="summary"' in body
    # Repeater markup
    assert 'id="links-repeater"' in body
    assert 'id="exp-repeater"' in body
    # Skills chip input
    assert 'id="skills-input"' in body
    # CV drop zone
    assert 'id="cv-zone"' in body
    assert 'id="cv-file"' in body
    # Connector grid
    assert 'id="connectors-grid"' in body


# ---------------------------------------------------------------------------
# 6. /api/connectors
# ---------------------------------------------------------------------------

def test_connectors_list_returns_four_platforms(client, auth_headers):
    r = client.get("/api/connectors", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    platforms = [c["platform"] for c in body["connectors"]]
    assert platforms == ["linkedin", "xing", "indeed", "workday"]
    # All show "disconnected" in a clean test env
    statuses = [c["status"] for c in body["connectors"]]
    assert all(s == "disconnected" for s in statuses)


def test_connector_connect_returns_501_when_binary_missing(client, auth_headers, monkeypatch):
    """POST /api/connectors/linkedin/connect returns 501 if the capture
    binary isn't installed (this is the standard test env)."""
    from src.web import app as appmod
    # Force the script to look non-existent
    fake = Path("/nonexistent/captainauth_capture.py")
    monkeypatch.setattr(appmod, "_CAPTURE_SCRIPT", fake)
    r = client.post("/api/connectors/linkedin/connect", headers=auth_headers)
    assert r.status_code == 501


def test_connector_connect_unknown_platform_400(client, auth_headers):
    r = client.post("/api/connectors/facebook/connect", headers=auth_headers)
    assert r.status_code == 400


def test_connectors_requires_auth(client):
    """No auth → 401 (matches the profile endpoint contract)."""
    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("LARS_USER_ID", None)
        r = client.get("/api/connectors")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# 7. Dashboard tour overlay (in index.html)
# ---------------------------------------------------------------------------

def test_dashboard_html_contains_tour_overlay_and_script():
    """The dashboard HTML must include the tour overlay element + a script
    block that fetches /api/profile (the gate that fires the tour)."""
    index_path = Path(__file__).resolve().parents[1] / "web" / "static" / "index.html"
    html = index_path.read_text(encoding="utf-8")
    # The overlay element
    assert 'id="tour-overlay"' in html
    assert 'id="tour-card"' in html
    assert 'id="tour-skip"' in html
    assert 'id="tour-next"' in html
    # The script block that hits /api/profile
    assert "/api/profile" in html
    # The tour has 5 steps defined
    assert "Welcome" in html


def test_dashboard_tour_uses_correct_endpoints():
    """The tour script must POST /api/profile/onboarding to dismiss."""
    index_path = Path(__file__).resolve().parents[1] / "web" / "static" / "index.html"
    html = index_path.read_text(encoding="utf-8")
    # The onboarding POST is what makes the tour not re-fire
    assert "/api/profile/onboarding" in html
    # The tour has a Skip control
    assert "Skip tour" in html or "Skip" in html


# ---------------------------------------------------------------------------
# 8. users_db schema helpers
# ---------------------------------------------------------------------------

def test_apply_migrations_is_idempotent():
    """Running _apply_migrations twice must not raise."""
    from src.db import users_db
    conn = users_db.connect_users_db()
    try:
        users_db._apply_migrations(conn)
        users_db._apply_migrations(conn)  # 2nd run is a no-op
        cols = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        for col, _ in users_db._PROFILE_MIGRATIONS:
            assert col in cols
    finally:
        conn.close()


def test_set_profile_admin_is_noop():
    """set_profile("lars", ...) must NOT touch the DB row."""
    from src.db import users_db
    wrote = users_db.set_profile("lars", {"name": "hax", "skills": ["x"]})
    assert wrote is False
    # And the admin profile is still the env-driven one
    prof = users_db.get_profile("lars")
    assert prof["is_admin"] is True
    # And the hax values did not leak
    assert prof["name"] != "hax"


def test_set_cv_upload_then_clear_roundtrip(tmp_path, monkeypatch):
    """set_cv_upload records a CV, clear_cv_upload wipes it."""
    from src.db import users_db
    from src.web.auth import hash_password
    # Point users_db at a throwaway file so we don't pollute the real DB
    db = tmp_path / "users_rt.db"
    monkeypatch.setattr(users_db, "USERS_DB", db)
    users_db.connect_users_db().close()
    uid = users_db.create_user("bob@x.test", hash_password("hunter22long"),
                                name="Bob")
    users_db.mark_verified(uid)
    # Upload
    ts = users_db.set_cv_upload(uid, "data/users/x/cv/cv.pdf")
    assert ts is not None
    prof = users_db.get_profile(uid)
    assert prof["cv_upload_path"] == "data/users/x/cv/cv.pdf"
    assert prof["cv_uploaded_at"]
    # Clear
    users_db.clear_cv_upload(uid)
    prof = users_db.get_profile(uid)
    assert prof["cv_upload_path"] == ""
    assert prof["cv_uploaded_at"] == ""


def test_set_onboarding_admin_is_noop():
    """set_onboarding("lars", True) must not raise and must not write."""
    from src.db import users_db
    wrote = users_db.set_onboarding("lars", True, step=3)
    assert wrote is False
    prof = users_db.get_profile("lars")
    assert prof["is_admin"] is True
    # step is still the env-driven default
    assert prof["onboarding_complete"] == 1


# ---------------------------------------------------------------------------
# 9. End-to-end smoke
# ---------------------------------------------------------------------------

def test_end_to_end_user_flow(client):
    """One full flow: create → upload CV → set profile → mark onboarded →
    re-fetch shows all the state."""
    uid, cookie = _create_verified_user(client)
    cookies = {"captain_session": cookie}
    # 1. Upload CV
    r = client.post(
        "/api/profile/cv",
        files={"file": ("me.pdf", io.BytesIO(b"%PDF-me"), "application/pdf")},
        cookies=cookies,
    )
    assert r.status_code == 200
    # 2. Set profile
    r = client.put(
        "/api/profile",
        json={"headline": "Captain", "skills": ["Python", "Rust"],
              "links": [{"kind": "github", "url": "https://github.com/me"}]},
        cookies=cookies,
    )
    assert r.status_code == 200
    # 3. Mark onboarded
    r = client.post(
        "/api/profile/onboarding",
        json={"complete": True, "step": 5},
        cookies=cookies,
    )
    assert r.status_code == 200
    # 4. Re-fetch the full profile + CV metadata
    prof = client.get("/api/profile", cookies=cookies).json()
    cv = client.get("/api/profile/cv/meta", cookies=cookies).json()
    assert prof["headline"] == "Captain"
    assert prof["skills"] == ["Python", "Rust"]
    assert prof["links"][0]["url"] == "https://github.com/me"
    assert prof["onboarding_complete"] == 1
    assert prof["tour_pending"] is False
    assert cv["has_cv"] is True
    assert cv["filename"] == "cv.pdf"


# ---------------------------------------------------------------------------
# 10. Auth boundary — the regression guards for this surface
# ---------------------------------------------------------------------------
#
# These are the tests that matter most. The profile endpoints write a
# person's PII and accept file uploads, so "which credential got you in"
# has to be asserted explicitly rather than assumed.

def test_env_var_alone_is_not_a_credential(client, monkeypatch):
    """LARS_USER_ID must never unlock a write.

    Regression guard: _profile_user_id() used to fall back to the env var
    with no credential check, so with that var set every profile write,
    CV upload and tour flag was reachable with NO cookie and NO Basic
    header — an unauthenticated write to another user's record.
    """
    from src.db import users_db
    uid, cookie = _create_verified_user(client)
    monkeypatch.setenv("LARS_USER_ID", uid)

    assert client.get("/api/profile").status_code == 401
    assert client.put("/api/profile", json={"headline": "hijacked"}).status_code == 401
    assert client.post(
        "/api/profile/cv",
        files={"file": ("cv.pdf", io.BytesIO(b"%PDF-x"), "application/pdf")},
    ).status_code == 401
    assert client.post("/api/profile/onboarding", json={"complete": True}).status_code == 401
    assert client.get("/api/connectors").status_code == 401
    assert client.delete("/api/profile/cv").status_code == 401

    # Nothing was written.
    assert users_db.get_profile(uid)["headline"] == ""
    assert users_db.get_profile(uid)["cv_upload_path"] == ""
    assert users_db.get_profile(uid)["onboarding_complete"] == 0


def test_put_profile_cannot_change_auth_columns(client):
    """email / plan are owned by the auth and billing flows, not this form."""
    from src.db import users_db
    uid, cookie = _create_verified_user(client)
    cookies = {"captain_session": cookie}
    r = client.put(
        "/api/profile",
        json={"name": "Still Alice", "email": "attacker@evil.test", "plan": "pro"},
        cookies=cookies,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "alice@example.com", "email must come from signup only"
    assert body["plan"] == "free", "plan is set by billing webhooks only"
    # And the stored row agrees.
    row = users_db.get_user_by_id(uid)
    assert row["email"] == "alice@example.com"
    assert row["plan"] == "free"


def test_cv_serving_never_escapes_the_users_own_cv_dir(client):
    """A tampered cv_upload_path must not be able to read another file."""
    from pathlib import Path as _P
    from src.db import users_db
    uid, cookie = _create_verified_user(client)
    cookies = {"captain_session": cookie}

    outside = users_db.PROJECT_ROOT / "data" / "users" / f"{uid}-secret.txt"
    outside.write_text("not this user's CV")
    conn = users_db.connect_users_db()
    conn.execute(
        "UPDATE users SET cv_upload_path = ? WHERE id = ?",
        (str(outside.relative_to(users_db.PROJECT_ROOT)), uid),
    )
    conn.commit()
    conn.close()
    try:
        assert client.get("/api/profile/cv", cookies=cookies).status_code == 404
        assert client.get(
            "/api/profile/cv/meta", cookies=cookies
        ).json()["has_cv"] is False
    finally:
        outside.unlink(missing_ok=True)


def test_tour_step_cannot_be_parked_out_of_range(client):
    """A crafted step must not create a step that does not exist."""
    from src.db import users_db
    uid, cookie = _create_verified_user(client)
    cookies = {"captain_session": cookie}
    r = client.post(
        "/api/profile/onboarding", json={"complete": False, "step": 9999},
        cookies=cookies,
    )
    assert r.status_code == 200
    assert r.json()["onboarding_step"] == users_db.TOUR_TOTAL_STEPS
    assert users_db.get_profile(uid)["onboarding_step"] == users_db.TOUR_TOTAL_STEPS


def test_connector_status_reflects_the_real_credential_vault(client, tmp_path, monkeypatch):
    """Connected must mean cookies are actually stored — not a sidecar file.

    The earlier implementation reported "connected" whenever a
    data/users/<id>/<platform>.json file existed, and the Connect endpoint
    wrote that file itself, so the UI claimed a stored LinkedIn session that
    did not exist. Status is now derived from CredentialStore.
    """
    from src.auth.credentials import CredentialStore
    from src.db import users_db
    monkeypatch.setenv("CAPTAIN_AUTH_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("CAPTAIN_AUTH_KEYFILE", str(tmp_path / "vault" / "master.key"))

    uid, cookie = _create_verified_user(client)
    cookies = {"captain_session": cookie}

    # A decoy sidecar file must NOT be enough to look connected.
    decoy = users_db.PROJECT_ROOT / "data" / "users" / uid / "linkedin.json"
    decoy.parent.mkdir(parents=True, exist_ok=True)
    decoy.write_text('{"stub": true}')
    try:
        body = client.get("/api/connectors", cookies=cookies).json()
        linkedin = next(c for c in body["connectors"] if c["platform"] == "linkedin")
        assert linkedin["status"] == "disconnected", \
            "a fabricated sidecar file must not read as a real credential"
    finally:
        decoy.unlink(missing_ok=True)

    # Storing a genuine credential flips it.
    CredentialStore().put(
        uid, "linkedin",
        cookies=[{"name": "li_at", "value": "abc", "domain": ".linkedin.com"}],
    )
    body = client.get("/api/connectors", cookies=cookies).json()
    linkedin = next(c for c in body["connectors"] if c["platform"] == "linkedin")
    assert linkedin is not None and linkedin["status"] == "connected"
    # One platform's login must not authenticate another.
    xing = next(c for c in body["connectors"] if c["platform"] == "xing")
    assert xing["status"] == "disconnected"


def test_pre_26_users_db_is_migrated_without_data_loss(tmp_path, monkeypatch):
    """A DB written before Phase 2.6 upgrades in place and keeps its rows."""
    from src.db import users_db
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(legacy))
    conn.executescript("""
        CREATE TABLE users (
            id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, name TEXT, plan TEXT DEFAULT 'free',
            verified INTEGER DEFAULT 0, verify_code TEXT, verify_expires_at TEXT,
            verify_attempts INTEGER DEFAULT 0, verify_invalidated INTEGER DEFAULT 0,
            reset_token TEXT, reset_expires_at TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        INSERT INTO users VALUES
          ('old1','old@example.com','h','Old Row','free',1,NULL,NULL,0,0,NULL,NULL,
           '2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00');
    """)
    conn.commit()
    conn.close()

    monkeypatch.setattr(users_db, "USERS_DB", legacy)
    profile = users_db.get_profile("old1")
    assert profile["name"] == "Old Row", "existing row must survive the migration"
    assert profile["email"] == "old@example.com"

    cols = {r[1] for r in sqlite3.connect(str(legacy)).execute("PRAGMA table_info(users)")}
    for col, _ddl in users_db._PROFILE_MIGRATIONS:
        assert col in cols, f"{col} missing after migration"

    # Idempotent.
    conn2 = sqlite3.connect(str(legacy))
    users_db._apply_migrations(conn2)
    users_db._apply_migrations(conn2)
    conn2.close()


def test_card_spec_column_names_are_the_ones_we_write():
    """The card names these columns; a silent rename would orphan data."""
    from src.db import users_db
    assert {c for c, _ in users_db._PROFILE_MIGRATIONS} == {
        "phone", "location", "headline", "summary", "experience_json",
        "skills_json", "links_json", "cv_upload_path", "cv_uploaded_at",
        "onboarding_complete", "onboarding_step",
    }


def test_dashboard_scripts_only_call_helpers_they_define():
    """Guard against undefined-helper crashes in the shipped dashboard JS.

    Regression: the Settings modal called `esc(...)` while that script block
    only defines `escapeHTML(...)`. The ReferenceError fired inside
    openSettings(), so the entire Settings modal — Connectors included —
    silently failed to open. Static scanning catches the whole class cheaply.
    """
    import re
    from pathlib import Path as _P
    html = _P(__file__).resolve().parents[1].joinpath(
        "web/static/index.html").read_text(encoding="utf-8")

    # Real globals the scripts may reference. Anything outside this set and
    # outside the block's own definitions is a bug.
    GLOBALS = {
        "console", "document", "window", "fetch", "location", "history",
        "setTimeout", "clearTimeout", "setInterval", "clearInterval",
        "Array", "Object", "JSON", "Math", "String", "Number", "Boolean",
        "Promise", "FormData", "URL", "Date", "Error", "Map", "Set",
        "MutationObserver", "ResizeObserver", "parseInt", "parseFloat",
        "isNaN", "confirm", "alert", "encodeURIComponent",
        "decodeURIComponent", "require", "async",
    }
    # Control-flow keywords the call-position regex inevitably matches.
    KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "typeof",
                "function", "do", "else", "new", "delete", "in", "of"}

    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert scripts, "index.html should carry inline scripts"
    for idx, body in enumerate(scripts):
        # Strip comments and string literals: prose inside them ("clicking
        # Done on the last step") otherwise reads as function calls.
        code = re.sub(r"/\*.*?\*/", " ", body, flags=re.S)
        code = re.sub(r"//[^\n]*", " ", code)
        code = re.sub(r"'[^'\n]*'|\"[^\"\n]*\"|`[^`]*`", " ", code)

        defined = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", code))
        defined |= set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*[=\(:]", code))
        called = set(re.findall(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(", code))
        missing = sorted(called - defined - GLOBALS - KEYWORDS)
        assert not missing, (
            f"script block {idx} calls undefined helper(s): {missing}"
        )
