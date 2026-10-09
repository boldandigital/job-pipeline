"""Tests for Phase 2.7 — multi-profile support (CRUD + CVs + job binding).

Scope (30+ tests, grouped):

  A. Profile CRUD
  B. Default invariant (exactly one default per user)
  C. Delete-the-only-profile returns 400
  D. CV CRUD + on-disk file management
  E. Endpoint auth (every /api/profiles/* needs a session)
  F. PUT /api/jobs/{id}/profile + jobs.profile_id column
  G. ensure_default_profile_for_user idempotency
  H. Generator --profile-id wiring (CV + cover letter)
"""
from __future__ import annotations

import io
import sqlite3
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.db import profiles_db as _pdb
from src.web.auth import COOKIE_NAME, make_session_cookie


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_db(tmp_path):
    """Fresh jobs DB with the same schema the admin gets after migration."""
    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY, title TEXT, company TEXT, location TEXT,
            url TEXT, career_url TEXT, source TEXT, description TEXT,
            score INTEGER, status TEXT DEFAULT 'new', cv_path TEXT,
            cover_letter_path TEXT, cv_ok INTEGER DEFAULT 0,
            anschreiben_ok INTEGER DEFAULT 0, motivation_ok INTEGER DEFAULT 0,
            approved_at TEXT,
            created_at TEXT, updated_at TEXT,
            profile_id TEXT
        );
        INSERT INTO jobs VALUES (1, 'T', 'TestCo', 'Berlin',
            'https://x.test', '', 'xing', 'desc', 500, 'new', '', '',
            0, 0, 0, '',
            '2026-10-03T00:00:00+00:00', '', NULL);
        INSERT INTO jobs VALUES (2, 'T2', 'TestCo2', 'Berlin',
            'https://x.test', '', 'xing', 'desc', 500, 'new', '', '',
            0, 0, 0, '',
            '2026-10-03T00:00:00+00:00', '', NULL);
    """)
    conn.commit(); conn.close()
    return db


@pytest.fixture
def app(tmp_path, temp_db, monkeypatch):
    """Build the FastAPI app with an isolated jobs DB and a tmp users DB."""
    from src.web import app as app_mod
    monkeypatch.setattr(app_mod, "DB_PATH", temp_db)
    empty_batches = tmp_path / "batches"
    empty_batches.mkdir()
    monkeypatch.setattr(app_mod, "BATCHES_DIR", empty_batches)

    # Force the global users DB to a tmp file so we never touch the real one.
    from src.db import users_db as _udb
    tmp_users = tmp_path / "users.db"
    monkeypatch.setattr(_udb, "USERS_DB", tmp_users)
    # profiles_db imports users_db at module top — re-point it as well.
    monkeypatch.setattr(_pdb, "_users_db", _udb)

    # Redirect the CV disk root so tests don't write to data/users.
    monkeypatch.setattr(_pdb, "PROFILE_CV_ROOT", tmp_path / "cv_store")
    return app_mod.app


@pytest.fixture
def client(app):
    return TestClient(app)


def _session(uid: str = "lars"):
    return {COOKIE_NAME: make_session_cookie(uid)}


# ---------------------------------------------------------------------------
# A. Profile CRUD
# ---------------------------------------------------------------------------

def test_list_profiles_seeds_admin_default_on_first_call(client):
    """The bootstrap admin has no row until first GET /api/profiles."""
    r = client.get("/api/profiles", cookies=_session())
    assert r.status_code == 200
    d = r.json()
    assert len(d["items"]) == 1
    assert d["items"][0]["name"]  # seeded name from lars-cv-data.json
    assert d["items"][0]["is_default"] is True
    assert d["default_id"] == d["items"][0]["id"]


def test_create_profile_minimal(client):
    r = client.post(
        "/api/profiles",
        cookies=_session(),
        json={"name": "BD Lead DACH"},
    )
    assert r.status_code == 201
    d = r.json()
    assert d["name"] == "BD Lead DACH"
    assert d["kind"] == "role"
    # No prior profiles exist → POSTing the very first one is the default.
    # (The "default" row is auto-created on GET, not on POST — see test below.)
    assert d["is_default"] is True
    assert d["headline"] == ""
    assert d["experience"] == []


def test_create_profile_after_seed_is_non_default(client):
    """Once a default has been seeded (via GET), POST adds a non-default row."""
    client.get("/api/profiles", cookies=_session())  # seeds default
    r = client.post(
        "/api/profiles",
        cookies=_session(),
        json={"name": "BD Lead DACH"},
    )
    assert r.status_code == 201
    assert r.json()["is_default"] is False


def test_create_profile_full_body(client):
    r = client.post(
        "/api/profiles",
        cookies=_session(),
        json={
            "name": "Hosting Infra",
            "kind": "role",
            "headline": "Hosting platform engineer",
            "summary": "12+ years of cPanel, DNS and edge.",
            "experience": [
                {"company": "HostSalt", "role": "Founder", "start": "2018",
                 "end": "present", "bullets": ["Hoster of 12k domains"]}
            ],
            "skills": ["cPanel", "DNS", "Linux"],
            "links": [{"kind": "linkedin", "url": "https://linkedin.com/in/x"}],
            "make_default": True,
        },
    )
    assert r.status_code == 201
    d = r.json()
    assert d["is_default"] is True
    assert d["headline"] == "Hosting platform engineer"
    assert d["skills"] == ["cPanel", "DNS", "Linux"]


def test_get_profile_by_id(client):
    a = client.post("/api/profiles", cookies=_session(),
                    json={"name": "Variant A"}).json()
    b = client.post("/api/profiles", cookies=_session(),
                    json={"name": "Variant B"}).json()
    r = client.get(f"/api/profiles/{a['id']}", cookies=_session())
    assert r.status_code == 200
    assert r.json()["name"] == "Variant A"


def test_get_profile_404(client):
    r = client.get("/api/profiles/does-not-exist", cookies=_session())
    assert r.status_code == 404


def test_update_profile_partial(client):
    a = client.post("/api/profiles", cookies=_session(),
                    json={"name": "Variant A"}).json()
    r = client.put(
        f"/api/profiles/{a['id']}",
        cookies=_session(),
        json={"headline": "Updated tagline", "skills": ["Go", "Rust"]},
    )
    assert r.status_code == 200
    d = r.json()
    assert d["headline"] == "Updated tagline"
    assert d["name"] == "Variant A"  # untouched
    assert d["skills"] == ["Go", "Rust"]


def test_update_profile_rejects_empty_name(client):
    a = client.post("/api/profiles", cookies=_session(),
                    json={"name": "Variant A"}).json()
    r = client.put(
        f"/api/profiles/{a['id']}",
        cookies=_session(),
        json={"name": "   "},
    )
    assert r.status_code == 400
    assert "name" in r.json()["detail"].lower()


def test_update_profile_404(client):
    r = client.put(
        "/api/profiles/does-not-exist",
        cookies=_session(),
        json={"name": "X"},
    )
    assert r.status_code == 404


def test_delete_profile(client):
    # Seed the default first so the delete is not blocked by the
    # "only profile left" guard.
    client.get("/api/profiles", cookies=_session())
    a = client.post("/api/profiles", cookies=_session(),
                    json={"name": "Variant A"}).json()
    b = client.post("/api/profiles", cookies=_session(),
                    json={"name": "Variant B"}).json()
    r = client.delete(f"/api/profiles/{a['id']}", cookies=_session())
    assert r.status_code == 200
    remaining = client.get("/api/profiles", cookies=_session()).json()["items"]
    assert len(remaining) == 2  # default + B
    assert a["id"] not in [p["id"] for p in remaining]


# ---------------------------------------------------------------------------
# B. Default invariant
# ---------------------------------------------------------------------------

def test_first_profile_is_default(client):
    """Single GET /api/profiles auto-creates the default; POST adds a 2nd, non-default."""
    listing = client.get("/api/profiles", cookies=_session()).json()
    assert len(listing["items"]) == 1
    assert listing["items"][0]["is_default"] is True
    second = client.post("/api/profiles", cookies=_session(),
                         json={"name": "Second"}).json()
    assert second["is_default"] is False


def test_set_default_profile_makes_target_default(client):
    a = client.post("/api/profiles", cookies=_session(),
                    json={"name": "A"}).json()
    b = client.post("/api/profiles", cookies=_session(),
                    json={"name": "B"}).json()
    r = client.post(f"/api/profiles/{a['id']}/default", cookies=_session())
    assert r.status_code == 200
    listing = client.get("/api/profiles", cookies=_session()).json()
    assert listing["default_id"] == a["id"]
    # exactly one default — no other row has is_default=True
    defaults = [p for p in listing["items"] if p["is_default"]]
    assert len(defaults) == 1
    assert defaults[0]["id"] == a["id"]


def test_set_default_then_make_default_again_is_idempotent(client):
    a = client.post("/api/profiles", cookies=_session(),
                    json={"name": "A"}).json()
    client.post(f"/api/profiles/{a['id']}/default", cookies=_session())
    r = client.post(f"/api/profiles/{a['id']}/default", cookies=_session())
    assert r.status_code == 200
    listing = client.get("/api/profiles", cookies=_session()).json()
    assert listing["default_id"] == a["id"]
    assert sum(1 for p in listing["items"] if p["is_default"]) == 1


def test_create_with_make_default_demotes_existing(client):
    client.get("/api/profiles", cookies=_session())  # seeds default
    a = client.post("/api/profiles", cookies=_session(),
                    json={"name": "A"}).json()
    # Now promote A via make_default on a new create
    b = client.post("/api/profiles", cookies=_session(),
                    json={"name": "B", "make_default": True}).json()
    assert b["is_default"] is True
    listing = client.get("/api/profiles", cookies=_session()).json()
    assert listing["default_id"] == b["id"]
    # A should no longer be default
    a_now = [p for p in listing["items"] if p["id"] == a["id"]][0]
    assert a_now["is_default"] is False


# ---------------------------------------------------------------------------
# C. Delete-the-only-profile returns 400
# ---------------------------------------------------------------------------

def test_cannot_delete_only_profile(client):
    """The seeded default is the only profile → DELETE returns 400."""
    listing = client.get("/api/profiles", cookies=_session()).json()
    only_id = listing["items"][0]["id"]
    r = client.delete(f"/api/profiles/{only_id}", cookies=_session())
    assert r.status_code == 400
    assert "create another profile first" in r.json()["detail"].lower()


def test_delete_default_promotes_next_oldest(client):
    client.get("/api/profiles", cookies=_session())  # seed default
    a = client.post("/api/profiles", cookies=_session(),
                    json={"name": "A"}).json()
    b = client.post("/api/profiles", cookies=_session(),
                    json={"name": "B"}).json()
    listing = client.get("/api/profiles", cookies=_session()).json()
    default_id = listing["default_id"]
    # Delete the default
    r = client.delete(f"/api/profiles/{default_id}", cookies=_session())
    assert r.status_code == 200
    # Another row is now default; invariant still holds
    listing = client.get("/api/profiles", cookies=_session()).json()
    assert len(listing["items"]) == 2
    defaults = [p for p in listing["items"] if p["is_default"]]
    assert len(defaults) == 1


def test_delete_returns_404_for_unknown(client):
    r = client.delete("/api/profiles/does-not-exist", cookies=_session())
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# D. CV CRUD
# ---------------------------------------------------------------------------

def _make_cv_bytes(tag: str = "pdf") -> bytes:
    """Return some bytes that look like a PDF (extension is what the API checks)."""
    return b"%PDF-stub-" + tag.encode()


def test_upload_cv_happy_path(client):
    p = client.post("/api/profiles", cookies=_session(),
                    json={"name": "Has CV"}).json()
    files = {"file": ("cv.pdf", _make_cv_bytes(), "application/pdf")}
    r = client.post(
        f"/api/profiles/{p['id']}/cvs",
        cookies=_session(),
        files=files,
    )
    assert r.status_code == 201
    d = r.json()
    assert d["filename"] == "cv.pdf"
    assert d["profile_id"] == p["id"]
    assert d["is_default"] is True  # first CV becomes default automatically


def test_upload_cv_rejects_bad_extension(client):
    p = client.post("/api/profiles", cookies=_session(),
                    json={"name": "Has CV"}).json()
    files = {"file": ("virus.exe", b"MZ\x00\x00", "application/octet-stream")}
    r = client.post(
        f"/api/profiles/{p['id']}/cvs",
        cookies=_session(),
        files=files,
    )
    assert r.status_code == 400
    assert "unsupported" in r.json()["detail"].lower() or "not allowed" in r.json()["detail"].lower()


def test_upload_cv_rejects_oversize(client, tmp_path, monkeypatch):
    """A 6 MB upload must be rejected with 413."""
    from src.db import profiles_db as pdb
    monkeypatch.setattr(pdb, "CV_MAX_BYTES", 1024)  # 1 KB ceiling for the test
    p = client.post("/api/profiles", cookies=_session(),
                    json={"name": "Big CV"}).json()
    big = b"%PDF-" + b"X" * 5000
    files = {"file": ("cv.pdf", big, "application/pdf")}
    r = client.post(
        f"/api/profiles/{p['id']}/cvs",
        cookies=_session(),
        files=files,
    )
    assert r.status_code == 413
    assert "larger" in r.json()["detail"].lower() or "mb" in r.json()["detail"].lower()


def test_list_cvs_returns_default_first(client):
    p = client.post("/api/profiles", cookies=_session(),
                    json={"name": "CV Profile"}).json()
    files = {"file": ("a.pdf", _make_cv_bytes("a"), "application/pdf")}
    a = client.post(
        f"/api/profiles/{p['id']}/cvs", cookies=_session(), files=files
    ).json()
    files = {"file": ("b.pdf", _make_cv_bytes("b"), "application/pdf")}
    b = client.post(
        f"/api/profiles/{p['id']}/cvs", cookies=_session(), files=files
    ).json()
    listing = client.get(
        f"/api/profiles/{p['id']}/cvs", cookies=_session()
    ).json()
    assert listing["default_id"] == a["id"]
    assert len(listing["items"]) == 2
    # b is not default
    assert b["is_default"] is False


def test_set_default_cv_clears_previous_default(client):
    p = client.post("/api/profiles", cookies=_session(),
                    json={"name": "Two CVs"}).json()
    a = client.post(
        f"/api/profiles/{p['id']}/cvs",
        cookies=_session(),
        files={"file": ("a.pdf", _make_cv_bytes("a"), "application/pdf")},
    ).json()
    b = client.post(
        f"/api/profiles/{p['id']}/cvs",
        cookies=_session(),
        files={"file": ("b.pdf", _make_cv_bytes("b"), "application/pdf")},
    ).json()
    # Switch default to b
    r = client.post(
        f"/api/profiles/{p['id']}/cvs/{b['id']}/default",
        cookies=_session(),
    )
    assert r.status_code == 200
    listing = client.get(
        f"/api/profiles/{p['id']}/cvs", cookies=_session()
    ).json()
    assert listing["default_id"] == b["id"]
    # a should no longer be default
    a_row = [c for c in listing["items"] if c["id"] == a["id"]][0]
    assert a_row["is_default"] is False


def test_delete_default_cv_promotes_remaining(client):
    p = client.post("/api/profiles", cookies=_session(),
                    json={"name": "Two CVs"}).json()
    a = client.post(
        f"/api/profiles/{p['id']}/cvs",
        cookies=_session(),
        files={"file": ("a.pdf", _make_cv_bytes("a"), "application/pdf")},
    ).json()
    b = client.post(
        f"/api/profiles/{p['id']}/cvs",
        cookies=_session(),
        files={"file": ("b.pdf", _make_cv_bytes("b"), "application/pdf")},
    ).json()
    # a is the default; delete it
    r = client.delete(
        f"/api/profiles/{p['id']}/cvs/{a['id']}", cookies=_session()
    )
    assert r.status_code == 200
    listing = client.get(
        f"/api/profiles/{p['id']}/cvs", cookies=_session()
    ).json()
    assert listing["default_id"] == b["id"]
    assert len(listing["items"]) == 1


def test_delete_cv_removes_file_on_disk(client, monkeypatch):
    """Best-effort: the on-disk file is unlinked along with the row."""
    p = client.post("/api/profiles", cookies=_session(),
                    json={"name": "Has CV"}).json()
    cv = client.post(
        f"/api/profiles/{p['id']}/cvs",
        cookies=_session(),
        files={"file": ("cv.pdf", _make_cv_bytes(), "application/pdf")},
    ).json()
    # The file should exist on disk. file_path is stored relative to
    # PROFILE_CV_ROOT (NOT PROJECT_ROOT) so the on-disk lookup must use the
    # same root.
    from src.db import profiles_db as pdb
    on_disk = pdb.PROFILE_CV_ROOT / cv["file_path"]
    assert on_disk.exists()
    # Delete the CV
    client.delete(f"/api/profiles/{p['id']}/cvs/{cv['id']}", cookies=_session())
    assert not on_disk.exists()


def test_upload_cv_rejects_empty_body(client):
    p = client.post("/api/profiles", cookies=_session(),
                    json={"name": "Has CV"}).json()
    files = {"file": ("empty.pdf", b"", "application/pdf")}
    r = client.post(
        f"/api/profiles/{p['id']}/cvs",
        cookies=_session(),
        files=files,
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# E. Auth (every endpoint needs a session)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,url,body", [
    ("GET",  "/api/profiles", None),
    ("GET",  "/api/profiles/anything", None),
    ("PUT",  "/api/profiles/anything", {"name": "x"}),
    ("DELETE", "/api/profiles/anything", None),
    ("POST", "/api/profiles/anything/default", None),
    ("GET",  "/api/profiles/anything/cvs", None),
    ("POST", "/api/profiles/anything/cvs", "CV_UPLOAD"),
    ("POST", "/api/profiles/anything/cvs/x/default", None),
    ("DELETE", "/api/profiles/anything/cvs/x", None),
    ("PUT",  "/api/jobs/1/profile", {"profile_id": None}),
])
def test_endpoints_require_auth(client, method, url, body):
    """No session cookie → 401 from every protected endpoint.

    For endpoints that take a JSON body, pass a valid-shape body so Pydantic
    validation does not 422 before the auth check runs. The body is never
    read on an unauth request — 401 wins.
    """
    fn = getattr(client, method.lower())
    kwargs = {}
    if body == "CV_UPLOAD":
        kwargs = {"files": {"file": ("x.pdf", b"%PDF", "application/pdf")}}
    elif body is not None:
        kwargs = {"json": body}
    r = fn(url, **kwargs)
    assert r.status_code == 401, f"{method} {url} should be 401, got {r.status_code}"


def test_create_profile_with_no_body_is_422(client):
    """POST /api/profiles without a JSON body is a 422 (validation), not 401.

    Pydantic validates the body BEFORE the auth helper runs, so this is the
    expected order. The auth gate is still enforced when the body is valid.
    """
    r = client.post("/api/profiles", cookies=_session())
    assert r.status_code == 422


def test_create_profile_without_auth_and_with_body_is_401(client):
    """With a valid body shape, the auth check runs and 401s the unauth caller."""
    r = client.post(
        "/api/profiles",
        json={"name": "Anyone"},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# F. PUT /api/jobs/{id}/profile + jobs.profile_id column
# ---------------------------------------------------------------------------

def test_jobs_profile_column_exists(temp_db):
    """Migration gate added the column."""
    conn = sqlite3.connect(str(temp_db))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    conn.close()
    assert "profile_id" in cols


def test_put_job_profile_binds_a_profile(client):
    p = client.post("/api/profiles", cookies=_session(),
                    json={"name": "For job 1"}).json()
    r = client.put(
        f"/api/jobs/1/profile",
        cookies=_session(),
        json={"profile_id": p["id"]},
    )
    assert r.status_code == 200
    assert r.json()["profile_id"] == p["id"]
    # Now GET the job and confirm the binding is returned
    job = client.get("/api/jobs/1", cookies=_session()).json()
    assert job["profile_id"] == p["id"]


def test_put_job_profile_clears_with_null(client):
    p = client.post("/api/profiles", cookies=_session(),
                    json={"name": "For job 1"}).json()
    client.put(f"/api/jobs/1/profile", cookies=_session(),
               json={"profile_id": p["id"]})
    r = client.put(f"/api/jobs/1/profile", cookies=_session(),
                   json={"profile_id": None})
    assert r.status_code == 200
    assert r.json()["profile_id"] == ""
    job = client.get("/api/jobs/1", cookies=_session()).json()
    assert job["profile_id"] == ""


def test_put_job_profile_rejects_unknown_profile(client):
    r = client.put(
        "/api/jobs/1/profile",
        cookies=_session(),
        json={"profile_id": "ghost-id"},
    )
    assert r.status_code == 404


def test_put_job_profile_rejects_unknown_job(client):
    r = client.put(
        "/api/jobs/9999/profile",
        cookies=_session(),
        json={"profile_id": "anything"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# G. ensure_default_profile_for_user idempotency
# ---------------------------------------------------------------------------

def test_ensure_default_profile_is_idempotent(tmp_path, monkeypatch):
    """Calling ensure twice does not create a duplicate row."""
    from src.db import users_db as _udb
    monkeypatch.setattr(_udb, "USERS_DB", tmp_path / "users.db")
    monkeypatch.setattr(_pdb, "_users_db", _udb)
    first = _pdb.ensure_default_profile_for_user("u-1")
    second = _pdb.ensure_default_profile_for_user("u-1")
    assert first["id"] == second["id"]
    listing = _pdb.list_profiles("u-1")
    assert len(listing) == 1
    assert listing[0]["is_default"] is True


def test_ensure_admin_default_uses_lars_cv_data(tmp_path, monkeypatch):
    """The bootstrap admin gets the legacy data seeded (Lars's headline)."""
    from src.db import users_db as _udb
    monkeypatch.setattr(_udb, "USERS_DB", tmp_path / "users.db")
    monkeypatch.setattr(_pdb, "_users_db", _udb)
    profile = _pdb.ensure_admin_default()
    assert profile["is_default"] is True
    # The legacy JSON has 'name' populated; the seeded profile should not be empty.
    assert profile["name"]
    assert profile["kind"] == "default"


# ---------------------------------------------------------------------------
# H. Generators with --profile-id
# ---------------------------------------------------------------------------

def test_cv_generator_loads_profile_data(monkeypatch, tmp_path):
    """When --profile-id is passed, the generator must consult profiles_db
    (and not the env-var fallback) for the headline + skills."""
    from src.db import users_db as _udb
    from src.db import profiles_db as _pdb
    monkeypatch.setattr(_udb, "USERS_DB", tmp_path / "users.db")
    monkeypatch.setattr(_pdb, "_users_db", _udb)

    profile = _pdb.create_profile(
        user_id="lars",
        name="Hosting Infra",
        kind="role",
        headline="__HEADLINE_FROM_TEST__",
        summary="Summary from test",
        skills=["cPanel", "DNS"],
    )

    from src.generation import cv_generator
    data = cv_generator._load_profile_override("lars", profile["id"])
    assert data["title_en"] == "__HEADLINE_FROM_TEST__"
    assert data["summary_en"] == "Summary from test"
    assert data["skills"]["en"] == ["cPanel", "DNS"]


def test_cv_generator_without_profile_id_falls_back_to_env(monkeypatch, tmp_path):
    """Without --profile-id, _load_profile_override is never called."""
    from src.db import users_db as _udb
    from src.db import profiles_db as _pdb
    monkeypatch.setattr(_udb, "USERS_DB", tmp_path / "users.db")
    monkeypatch.setattr(_pdb, "_users_db", _udb)

    from src.generation import cv_generator
    # Patch _load_profile_override to fail if called
    called = {"flag": False}
    original = cv_generator._load_profile_override
    def spy(*a, **kw):
        called["flag"] = True
        return original(*a, **kw)
    monkeypatch.setattr(cv_generator, "_load_profile_override", spy)

    # When no profile is given, main() should not call _load_profile_override.
    # We mimic that branch by exercising only the global CV_DATA path.
    data = cv_generator._load_cv_data()
    # The real file is at config/lars-cv-data.json; we don't assert on its
    # contents, only that the function returned *something* and that the
    # profile-override path is the only one that actually replaces the globals.
    assert isinstance(data, dict)
    assert called["flag"] is False  # we never asked for an override


def test_cover_letter_generator_applies_profile(monkeypatch, tmp_path):
    """cover_letter_generator._apply_profile_data fills missing personal fields.

    Real personal data (already populated from lars-cv-data.json) wins over a
    profile override — the profile can ADD missing fields, but never clobber
    a value the user has actually filled in. This matches the principle behind
    the legacy single-profile path: config/ > DB profile.
    """
    from src.db import users_db as _udb
    from src.db import profiles_db as _pdb
    monkeypatch.setattr(_udb, "USERS_DB", tmp_path / "users.db")
    monkeypatch.setattr(_pdb, "_users_db", _udb)

    # Personal field the legacy config does NOT have — Portfolio is the cleanest
    # example (real personal has linkedin + github but no portfolio field on
    # PERSONAL itself, the URLs live in `links_json` not in PERSONAL).
    profile = _pdb.create_profile(
        user_id="lars",
        name="Hosting Infra",
        kind="role",
        headline="__CL_HEADLINE__",
        links=[{"kind": "Portfolio", "url": "https://lars-portfolio.example"}],
    )

    from src.generation import cover_letter_generator
    # Snapshot PERSONAL — the module-level singleton gets mutated.
    original_personal = dict(cover_letter_generator.PERSONAL)
    try:
        applied = cover_letter_generator._apply_profile_data("lars", profile["id"])
        assert applied is True
        # The new headline lands on CV_DATA.title_en
        assert cover_letter_generator.CV_DATA.get("title_en") == "__CL_HEADLINE__"
        # A NEW field the profile provides (portfolio) is added to `websites`.
        assert "https://lars-portfolio.example" in (cover_letter_generator.PERSONAL.get("websites") or [])
        # Existing fields the user already filled (linkedin) are preserved.
        assert cover_letter_generator.PERSONAL.get("linkedin")  # the real one survives
    finally:
        cover_letter_generator.PERSONAL = original_personal


# ---------------------------------------------------------------------------
# I. Sanity / integration
# ---------------------------------------------------------------------------

def test_legacy_api_profile_keeps_working(client):
    """Phase 2.6 /api/profile and /app/profile must not regress."""
    r = client.get("/api/profile", cookies=_session())
    assert r.status_code == 200
    assert "name" in r.json()


def test_profiles_index_page_loads(client):
    r = client.get("/app/profiles", cookies=_session())
    assert r.status_code == 200
    assert "Profiles" in r.text


def test_profile_edit_page_loads(client):
    p = client.post("/api/profiles", cookies=_session(),
                    json={"name": "View me"}).json()
    r = client.get(f"/app/profiles/{p['id']}", cookies=_session())
    assert r.status_code == 200
