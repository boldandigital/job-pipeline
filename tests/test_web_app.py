"""
Tests for the Job Approval UI web app (FastAPI + SQLite).

Covers:
  - /api/health  → 200 ok
  - /api/stats   → counts
  - /api/jobs    → list jobs, filterable by status
  - /api/jobs/<id>/approve → flips status to 'approved'
  - /api/jobs/<id>/skip    → flips status to 'skipped'
  - /api/batches/<path>    → serves PDF (with token auth)
  - HTTP Basic auth required on all /api/* endpoints
  - Static index.html served at /
"""
import base64
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.web.app import _connect, _row_to_job, _guess_salary_band, _clean_description


# ----- fixtures -----------------------------------------------------------

@pytest.fixture
def temp_db(tmp_path):
    """Build a fresh SQLite with 3 sample jobs."""
    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY,
            title TEXT,
            company TEXT,
            location TEXT,
            url TEXT,
            career_url TEXT,
            score INTEGER,
            source TEXT,
            status TEXT,
            description TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        INSERT INTO jobs VALUES
          (1, 'Chief of Staff (m/w/d)', 'Andercore', 'Berlin',
           'https://xing.test/1', 'https://andercore.com',
           1940, 'xing', 'new',
           'We are hiring a Chief of Staff. €100,000 – €135,000 salary.',
           '2026-10-03T00:00:00+00:00', ''),
          (2, 'Founding Marketer (m/f/x)', 'xHeron', 'Berlin',
           'https://xing.test/2', 'https://xheron.com',
           1810, 'xing', 'new',
           'xHeron is the first AI + Human-Service-Agency for STR. 60k – 75k',
           '2026-10-03T00:00:00+00:00', ''),
          (3, 'Junior Sales Intern', 'Acme Corp', 'Brussels',
           'https://xing.test/3', 'https://acme.com',
           -1800, 'xing', 'new',
           'About this job\n\nLooking for an intern. 6-month contract.',
           '2026-10-03T00:00:00+00:00', '');
    """)
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def app(tmp_path, temp_db, monkeypatch):
    """FastAPI app with DB pointing at temp file."""
    # Need to patch BATCHES_DIR + DB_PATH BEFORE importing
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


# ----- tests --------------------------------------------------------------

def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_stats(client, auth_headers):
    r = client.get("/api/stats", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()
    assert "today_new" in data
    assert "high_fit" in data
    assert "salary_floor" in data
    assert data["salary_floor"] == 65000


def test_list_jobs(client, auth_headers):
    r = client.get("/api/jobs?status=new&limit=10", headers=auth_headers)
    assert r.status_code == 200
    jobs = r.json()
    assert len(jobs) == 3
    # Sorted by score desc
    assert jobs[0]["company"] == "Andercore"
    assert jobs[0]["score"] == 1940


def test_get_one_job(client, auth_headers):
    r = client.get("/api/jobs/1", headers=auth_headers)
    assert r.status_code == 200
    job = r.json()
    assert job["title"] == "Chief of Staff (m/w/d)"
    assert "Berlin" in job["location"]


def test_get_missing_job(client, auth_headers):
    r = client.get("/api/jobs/999", headers=auth_headers)
    assert r.status_code == 404


def test_approve_job(client, auth_headers, temp_db):
    r = client.post("/api/jobs/1/approve", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["status"] == "approved"
    # Verify DB state
    conn = sqlite3.connect(str(temp_db))
    row = conn.execute("SELECT status FROM jobs WHERE id=1").fetchone()
    assert row[0] == "approved"


def test_skip_job(client, auth_headers, temp_db):
    r = client.post("/api/jobs/2/skip", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["status"] == "skipped"


def test_auth_required(client):
    """All /api/* endpoints require Basic auth."""
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/api/stats").status_code == 401
    assert client.get("/api/jobs/1").status_code == 401
    assert client.post("/api/jobs/1/approve").status_code == 401


def test_root_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "html" in r.headers["content-type"]
    assert "Captain" in r.text


def test_salary_band_extraction():
    assert _guess_salary_band("foo", "€100,000 – €135,000") == "€100,000–€135,000"
    assert _guess_salary_band("foo", "60k – 75k") == "€60k–€75k"
    assert _guess_salary_band("foo", "salary: 50-60k") == "€50k–€60k"
    assert _guess_salary_band("foo", "") == "€60k–€75k"


def test_description_cleaner():
    assert _clean_description("About this job\n\nReal content").startswith("Real content")
    assert _clean_description("Real content\n\nÄhnliche Jobs") == "Real content"


def test_pdf_served_with_token(client, tmp_path, auth_headers, monkeypatch):
    """PDF endpoint accepts token= query param for iframe use."""
    pdf_dir = tmp_path / "batches" / "2026-10-02" / "Test_Co"
    pdf_dir.mkdir(parents=True)
    pdf = pdf_dir / "CV_Test_Co.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake\n")

    from src.web import app as app_mod
    monkeypatch.setattr(app_mod, "BATCHES_DIR", tmp_path / "batches")

    token = base64.b64encode(b"lars:captain").decode().rstrip("=")
    r = client.get(f"/api/batches/data/batches/2026-10-02/Test_Co/CV_Test_Co.pdf?token={token}")
    assert r.status_code == 200
    assert r.content.startswith(b"%PDF")


def test_pdf_path_traversal_blocked(client, auth_headers, tmp_path, monkeypatch):
    """PDF endpoint blocks ../ path traversal."""
    from src.web import app as app_mod
    monkeypatch.setattr(app_mod, "BATCHES_DIR", tmp_path / "batches")
    r = client.get(
        "/api/batches/../../../etc/passwd",
        headers=auth_headers,
    )
    # Either 403 or 404 — never serves a forbidden file
    assert r.status_code in (403, 404)