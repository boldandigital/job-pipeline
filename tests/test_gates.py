"""
Tests for the Phase 1.7 4-gate progressive approval endpoint.

POST /api/jobs/{id}/gate — {stage: cv|anschreiben|motivation, value: bool}

Rules enforced by the server (mirrors the dashboard CSS):
  - Stage 02 (Anschreiben) may only become true if Stage 01 (CV) is true.
  - Stage 03 (Motivation) may only become true if Stage 02 (Anschreiben) is true.
  - Rolling a gate back re-locks every gate after it.
  - The final Approve button is only usable when all 3 gates are true.
"""
import base64
import sqlite3
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.web.auth import COOKIE_NAME, make_session_cookie


@pytest.fixture
def temp_db(tmp_path):
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
            created_at TEXT, updated_at TEXT
        );
        INSERT INTO jobs VALUES (1, 'T', 'TestCo', 'Berlin',
            'https://x.test', '', 'xing', 'desc', 500, 'new', '', '',
            0, 0, 0, '',
            '2026-10-03T00:00:00+00:00', '');
        INSERT INTO jobs VALUES (2, 'T2', 'TestCo2', 'Berlin',
            'https://x.test', '', 'xing', 'desc', 500, 'new', '', '',
            0, 0, 0, '',
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


def _session():
    return {COOKIE_NAME: make_session_cookie("lars")}


# ----- happy path: progressive gate approval ------------------------------

def test_gate_cv_marks_first_stage(client, temp_db):
    r = client.post("/api/jobs/1/gate", cookies=_session(), json={"stage": "cv", "value": True})
    assert r.status_code == 200
    d = r.json()
    assert d["cv_ok"] is True
    assert d["anschreiben_ok"] is False
    assert d["motivation_ok"] is False
    assert d["all_gates_ok"] is False


def test_gate_progression_cv_then_anschreiben_then_motivation(client):
    s1 = client.post("/api/jobs/1/gate", cookies=_session(), json={"stage": "cv", "value": True})
    assert s1.status_code == 200
    s2 = client.post("/api/jobs/1/gate", cookies=_session(), json={"stage": "anschreiben", "value": True})
    assert s2.status_code == 200
    assert s2.json()["anschreiben_ok"] is True
    s3 = client.post("/api/jobs/1/gate", cookies=_session(), json={"stage": "motivation", "value": True})
    assert s3.status_code == 200
    d = s3.json()
    assert d["cv_ok"] and d["anschreiben_ok"] and d["motivation_ok"]
    assert d["all_gates_ok"] is True


def test_all_gates_ok_unlocks_final_approve_button_state(client, temp_db):
    """Once all 3 gates are ✓, the API returns all_gates_ok=True so the UI can enable Approve."""
    for stage in ("cv", "anschreiben", "motivation"):
        r = client.post("/api/jobs/1/gate", cookies=_session(), json={"stage": stage, "value": True})
        assert r.json()["all_gates_ok"] is True if stage == "motivation" else r.json()["all_gates_ok"] is False


# ----- progression rule ---------------------------------------------------

def test_anschreiben_blocked_when_cv_not_ok(client):
    """Server refuses to mark Anschreiben ✓ if CV is not ✓."""
    r = client.post("/api/jobs/1/gate", cookies=_session(), json={"stage": "anschreiben", "value": True})
    assert r.status_code == 409
    assert "Gate 01" in r.json()["detail"] or "CV" in r.json()["detail"]


def test_motivation_blocked_when_anschreiben_not_ok(client):
    """Server refuses to mark Motivation ✓ if Anschreiben is not ✓."""
    # Stage CV is fine
    client.post("/api/jobs/1/gate", cookies=_session(), json={"stage": "cv", "value": True})
    # Skip Anschreiben and try Motivation
    r = client.post("/api/jobs/1/gate", cookies=_session(), json={"stage": "motivation", "value": True})
    assert r.status_code == 409
    assert "Gate 02" in r.json()["detail"] or "Anschreiben" in r.json()["detail"]


def test_rolling_back_cv_re_locks_everything_after(client, temp_db):
    """Uncheck CV → Anschreiben + Motivation must revert to False."""
    # Approve all 3
    for stage in ("cv", "anschreiben", "motivation"):
        client.post("/api/jobs/1/gate", cookies=_session(), json={"stage": stage, "value": True})
    # Verify all true
    r = client.get("/api/jobs/1", cookies=_session())
    j = r.json()
    assert j["cv_ok"] and j["anschreiben_ok"] and j["motivation_ok"]
    # Roll back CV
    r = client.post("/api/jobs/1/gate", cookies=_session(), json={"stage": "cv", "value": False})
    assert r.status_code == 200
    d = r.json()
    assert d["cv_ok"] is False
    assert d["anschreiben_ok"] is False  # re-locked
    assert d["motivation_ok"] is False  # re-locked
    assert d["all_gates_ok"] is False


def test_rolling_back_anschreiben_re_locks_motivation_only(client, temp_db):
    """Uncheck Anschreiben → only Motivation reverts to False (CV stays true)."""
    for stage in ("cv", "anschreiben", "motivation"):
        client.post("/api/jobs/1/gate", cookies=_session(), json={"stage": stage, "value": True})
    r = client.post("/api/jobs/1/gate", cookies=_session(), json={"stage": "anschreiben", "value": False})
    d = r.json()
    assert d["cv_ok"] is True  # stays
    assert d["anschreiben_ok"] is False
    assert d["motivation_ok"] is False  # re-locked


def test_uncheck_motivation_only_unchecks_motivation(client, temp_db):
    for stage in ("cv", "anschreiben", "motivation"):
        client.post("/api/jobs/1/gate", cookies=_session(), json={"stage": stage, "value": True})
    r = client.post("/api/jobs/1/gate", cookies=_session(), json={"stage": "motivation", "value": False})
    d = r.json()
    assert d["cv_ok"] and d["anschreiben_ok"]
    assert d["motivation_ok"] is False


# ----- persistence -------------------------------------------------------

def test_gate_state_persists_in_db(client, temp_db):
    """Verify the gate columns land in SQLite."""
    for stage in ("cv", "anschreiben", "motivation"):
        client.post("/api/jobs/1/gate", cookies=_session(), json={"stage": stage, "value": True})
    conn = sqlite3.connect(str(temp_db))
    row = conn.execute("SELECT cv_ok, anschreiben_ok, motivation_ok FROM jobs WHERE id=1").fetchone()
    conn.close()
    assert row[0] == 1 and row[1] == 1 and row[2] == 1


def test_gate_state_visible_in_job_fetch(client, temp_db):
    """GET /api/jobs/{id} returns the gate booleans."""
    client.post("/api/jobs/1/gate", cookies=_session(), json={"stage": "cv", "value": True})
    client.post("/api/jobs/1/gate", cookies=_session(), json={"stage": "anschreiben", "value": True})
    r = client.get("/api/jobs/1", cookies=_session())
    d = r.json()
    assert d["cv_ok"] is True
    assert d["anschreiben_ok"] is True
    assert d["motivation_ok"] is False
    assert d["approved_at"] == ""


# ----- error paths --------------------------------------------------------

def test_gate_invalid_stage_400(client):
    r = client.post("/api/jobs/1/gate", cookies=_session(), json={"stage": "totally_invalid", "value": True})
    assert r.status_code == 400
    assert "invalid stage" in r.json()["detail"]


def test_gate_unknown_job_404(client):
    r = client.post("/api/jobs/9999/gate", cookies=_session(), json={"stage": "cv", "value": True})
    assert r.status_code == 404


def test_gate_requires_auth(client):
    r = client.post("/api/jobs/1/gate", json={"stage": "cv", "value": True})
    assert r.status_code == 401


def test_gate_multi_job_isolation(client, temp_db):
    """Approving one job's gates does not affect another job."""
    client.post("/api/jobs/1/gate", cookies=_session(), json={"stage": "cv", "value": True})
    r = client.get("/api/jobs/2", cookies=_session())
    assert r.json()["cv_ok"] is False
    assert r.json()["anschreiben_ok"] is False
    assert r.json()["motivation_ok"] is False


# ----- integration with existing flow -------------------------------------

def test_gates_then_approve_still_flips_status_to_approved(client, temp_db):
    """The 3 gates + the existing /approve endpoint compose cleanly."""
    for stage in ("cv", "anschreiben", "motivation"):
        client.post("/api/jobs/1/gate", cookies=_session(), json={"stage": stage, "value": True})
    # Now the legacy /approve should still work (status flips to 'approved')
    r = client.post("/api/jobs/1/approve", cookies=_session())
    assert r.status_code == 200
    assert r.json()["status"] == "approved"
    # And the gate columns remain set
    j = client.get("/api/jobs/1", cookies=_session()).json()
    assert j["cv_ok"] and j["anschreiben_ok"] and j["motivation_ok"]
    assert j["status"] == "approved"