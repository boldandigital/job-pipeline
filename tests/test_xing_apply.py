"""
Tests for the XING Apply Assistant (Phase 1.7).

Covers:
  - GET  /app/xing/<job_id>                 → 200 + contains the expected fields
  - GET  /app/xing/<job_id>                 → 302 redirect when not signed in
  - GET  /app/xing/<missing_id>             → 404
  - GET  /api/v1/xing/fill/<job_id>         → JSON with the right field values
  - POST /api/v1/xing/ready/<job_id>        → flips status to 'queued' in DB
  - The HTML page contains both PDF filenames + the message draft filename
  - Auth: GET /app/xing/<job_id> without session → 302 (not 200)

The fixtures follow the same shape as tests/test_web_app.py: a temp SQLite
with 3 sample jobs and the batches/ tree populated for one of them.
"""
from __future__ import annotations

import base64
import sqlite3
from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient


# ----- fixtures -----------------------------------------------------------

@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    """Build a fresh SQLite with 3 sample jobs (1 XING approved)."""
    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
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
           'We are hiring a Chief of Staff.',
           '2026-10-03T00:00:00+00:00', ''),
          (63, 'Founding Marketer (m/f/x)', 'xHeron Solutions', 'Berlin',
           'https://www.xing.com/jobs/berlin-founding-marketer-157766376',
           'https://xheron.com',
           1810, 'xing', 'approved',
           'xHeron is the first AI + Human-Service-Agency for STR.',
           '2026-10-03T00:00:00+00:00', ''),
          (76, 'Chief Operating Officer (m/w/d)', 'businessangels.de', 'Berlin',
           'https://www.xing.com/jobs/berlin-chief-operating-officer-157922614',
           'https://businessangels.de',
           1500, 'xing', 'approved',
           'businessangels.de marketplace role.',
           '2026-10-03T00:00:00+00:00', '');
        """
    )
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def batches_dir(tmp_path: Path) -> Path:
    """Create a fake batches/2026-10-02/xHeron_Solutions/ tree with assets."""
    company = tmp_path / "batches" / "2026-10-02" / "xHeron_Solutions"
    company.mkdir(parents=True)
    # Real-ish PDF magic bytes
    (company / "CV_xHeron_Solutions.pdf").write_bytes(b"%PDF-1.4 stub cv\n")
    (company / "CL_xHeron_Solutions.pdf").write_bytes(b"%PDF-1.4 stub cl\n")
    (company / "xing_application_draft.txt").write_text(
        "Pre-composed XING Easy Apply message for xHeron Solutions\n"
        "---------------------------------------------------------------------\n"
        "Anschreiben / Cover Letter (paste into XING \"Nachricht\" field)\n"
        "---------------------------------------------------------------------\n"
        "\nSehr geehrte Damen und Herren,\n\n"
        "this is the actual message body that should land in the Nachricht field.\n"
        "It is multi-line and includes accented characters: ä ö ü ß.\n",
        encoding="utf-8",
    )
    return tmp_path / "batches"


@pytest.fixture
def app(tmp_path: Path, temp_db: Path, batches_dir: Path, monkeypatch):
    """FastAPI app with DB + batches pointing at temp paths."""
    from src.web import app as app_mod
    monkeypatch.setattr(app_mod, "DB_PATH", temp_db)
    monkeypatch.setattr(app_mod, "BATCHES_DIR", batches_dir)
    return app_mod.app


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def auth_headers() -> Dict[str, str]:
    raw = base64.b64encode(b"lars:captain").decode()
    return {"Authorization": f"Basic {raw}"}


# ----- tests --------------------------------------------------------------

def test_page_renders_for_approved_job(client, auth_headers):
    """GET /app/xing/63 returns 200 + the page contains the job title."""
    r = client.get("/app/xing/63", headers=auth_headers, follow_redirects=False)
    assert r.status_code == 200
    assert "html" in r.headers["content-type"]
    body = r.text
    assert "Founding Marketer" in body
    assert "xHeron" in body


def test_page_redirects_when_unauthenticated(client):
    """GET /app/xing/63 without a session cookie → 302 to the marketing page."""
    r = client.get("/app/xing/63", follow_redirects=False)
    # The page route redirects unauthenticated users; only the /api/* endpoints
    # return 401. (The HTML endpoint can't 401 because the browser would
    # just show a blank page instead of going to login.)
    assert r.status_code == 302
    assert r.headers.get("location", "").startswith("/")


def test_page_returns_404_for_unknown_job(client, auth_headers):
    r = client.get("/app/xing/9999", headers=auth_headers, follow_redirects=False)
    assert r.status_code == 404


def test_fill_endpoint_returns_field_values(client, auth_headers):
    """GET /api/v1/xing/fill/63 returns the personal data from lars-cv-data.json."""
    r = client.get("/api/v1/xing/fill/63", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()

    # Job header
    assert data["job"]["id"] == 63
    assert data["job"]["company"] == "xHeron Solutions"

    # Field values come from config/lars-cv-data.json
    fields = data["fields"]
    assert fields["name"] == "Lars Zimmermann"
    assert fields["email"] == "lars.z@icloud.com"
    assert fields["phone"] == "+32 456 97 01 66"
    assert "Aarschot" in fields["location"]
    assert "100.000" in fields["salary"]
    assert "Ab sofort" in fields["start_date"]


def test_ready_endpoint_flips_status_to_queued(client, auth_headers, temp_db):
    """POST /api/v1/xing/ready/63 sets status='queued' in the DB."""
    r = client.post("/api/v1/xing/ready/63", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "queued"

    # Verify DB state directly
    conn = sqlite3.connect(str(temp_db))
    row = conn.execute("SELECT status FROM jobs WHERE id=63").fetchone()
    conn.close()
    assert row[0] == "queued"


def test_page_contains_pdf_filenames(client, auth_headers):
    """The HTML page lists the CV and CL PDF filenames for the user to attach."""
    r = client.get("/app/xing/63", headers=auth_headers)
    assert r.status_code == 200
    body = r.text
    assert "CV_xHeron_Solutions.pdf" in body
    assert "CL_xHeron_Solutions.pdf" in body


def test_page_contains_message_draft_filename(client, auth_headers):
    """The HTML page lists the xing_application_draft.txt as the message body."""
    r = client.get("/app/xing/63", headers=auth_headers)
    assert r.status_code == 200
    assert "xing_application_draft.txt" in r.text


def test_page_contains_message_body(client, auth_headers):
    """The HTML page inlines the Nachricht body extracted from the draft."""
    r = client.get("/app/xing/63", headers=auth_headers)
    assert r.status_code == 200
    body = r.text
    # The body of the seeded draft contains this signature line
    assert "Sehr geehrte" in body
    # Accented characters should be preserved
    assert "ä ö ü ß" in body


def test_fill_endpoint_requires_auth(client):
    """GET /api/v1/xing/fill/63 without auth → 401."""
    r = client.get("/api/v1/xing/fill/63")
    assert r.status_code == 401


def test_ready_endpoint_requires_auth(client):
    """POST /api/v1/xing/ready/63 without auth → 401."""
    r = client.post("/api/v1/xing/ready/63")
    assert r.status_code == 401
