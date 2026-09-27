#!/usr/bin/env python3
"""ADOPT-11 — Rejection dashboard tests.

Verify the analytics module produces a readable markdown report from
rejection data, including the right shape of suggestions and totals.

Run:
    .venv/bin/python -m pytest tests/test_rejection_dashboard.py -v
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.analytics import rejection_dashboard as rd  # noqa: E402


def _make_db(tmp_path: Path) -> Path:
    """Canonical jobs schema with a sprinkling of rejections across reasons."""
    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        company TEXT NOT NULL,
        score INTEGER DEFAULT 0,
        source TEXT,
        status TEXT DEFAULT 'new',
        approved_at TIMESTAMP,
        rejection_reason TEXT,
        rejection_note TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(title, company)
    );
    INSERT INTO jobs (title, company, score, source, status, rejection_reason, rejection_note, updated_at) VALUES
        ('Senior PM',      'Acme',  180, 'arbeitsagentur', 'rejected', 'too_junior',        NULL,                        datetime('now', '-1 day')),
        ('Junior Dev',     'Beta',   30, 'stepstone',      'rejected', 'too_junior',        NULL,                        datetime('now', '-2 day')),
        ('CTO',            'Gamma', 220, 'linkedin',       'rejected', 'too_senior',        NULL,                        datetime('now', '-3 day')),
        ('Wrong Loc PM',   'Delta', 150, 'stepstone',      'rejected', 'wrong_location',    NULL,                        datetime('now', '-4 day')),
        ('Saltoo Low',     'Eps',    90, 'arbeitsagentur', 'rejected', 'salary_too_low',    NULL,                        datetime('now', '-5 day')),
        ('Old reject',     'Zeta',  100, 'stepstone',      'rejected', 'too_junior',        NULL,                        datetime('now', '-40 day')),
        ('Approved job',   'Theta', 200, 'arbeitsagentur', 'approved', NULL,                NULL,                        datetime('now', '-1 day')),
        ('New (no state)', 'Iota',  100, 'arbeitsagentur', 'new',      NULL,                NULL,                        datetime('now', '-1 day'));
    """)
    conn.commit()
    conn.close()
    return db


def test_fetch_rejections_excludes_old_and_approved(tmp_path):
    """30-day window should drop the 40-day-old row and the approved/new rows."""
    db = _make_db(tmp_path)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = rd.fetch_rejections(str(db), window_days=30, now=now)
    titles = sorted(r["title"] for r in rows)
    # Old reject (40 days ago) is outside the window.
    assert "Old reject" not in titles
    # Approved job + New job aren't status='rejected'.
    assert "Approved job" not in titles
    assert "New (no state)" not in titles
    # The 5 in-window rejections.
    assert titles == ["CTO", "Junior Dev", "Saltoo Low", "Senior PM", "Wrong Loc PM"]


def test_group_by_reason_counts_match(tmp_path):
    db = _make_db(tmp_path)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = rd.fetch_rejections(str(db), window_days=30, now=now)
    grouped = rd.group_by_reason(rows)
    assert grouped["too_junior"] and len(grouped["too_junior"]) == 2
    assert grouped["too_senior"] and len(grouped["too_senior"]) == 1
    assert grouped["wrong_location"] and len(grouped["wrong_location"]) == 1
    assert grouped["salary_too_low"] and len(grouped["salary_too_low"]) == 1
    assert grouped["not_interested"] == []  # never used in fixture


def test_render_markdown_includes_suggestions(tmp_path):
    db = _make_db(tmp_path)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = rd.fetch_rejections(str(db), window_days=30, now=now)
    body = rd.render_markdown(rows, window_days=30, generated_at=now)

    # Top-level structure.
    assert "# Rejection patterns" in body
    assert "Total rejections" in body
    # Summary table has each reason we created.
    assert "too_junior" in body
    assert "too_senior" in body
    assert "wrong_location" in body
    # Top 3 suggestion bullets — too_junior should lead (2 of 5 = 40%).
    assert "Suggested config tweaks" in body
    # Each suggestion paragraph references a config file (lars.yaml / apply_filter.py).
    assert "config/lars.yaml" in body or "apply_filter.py" in body


def test_render_markdown_handles_empty(tmp_path):
    """When there are no rejections, the report should be short + friendly."""
    db = tmp_path / "empty.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
    CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT, company TEXT,
        score INTEGER, source TEXT, status TEXT, approved_at TIMESTAMP,
        rejection_reason TEXT, rejection_note TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
    """)
    conn.commit(); conn.close()

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    body = rd.render_markdown([], window_days=30, generated_at=now)
    assert "Total rejections" in body
    assert "No rejections" in body


def test_write_report_creates_file(tmp_path):
    db = _make_db(tmp_path)
    out_dir = tmp_path / "research"
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    path = rd.write_report(
        str(db), out_dir=str(out_dir), window_days=30, now=now,
    )
    assert path is not None
    assert path.exists()
    # Filename matches the contract.
    assert path.name == f"rejection-patterns-{now.strftime('%Y-%m')}.md"


def test_cli_json_emits_counts(tmp_path, monkeypatch, capsys):
    db = _make_db(tmp_path)
    monkeypatch.setattr("sys.argv", [
        "rejection_dashboard",
        "--db", str(db),
        "--window-days", "30",
        "--json",
    ])
    rc = rd.main()
    out = capsys.readouterr().out
    import json
    parsed = json.loads(out)
    assert rc == 0
    assert parsed["total"] == 5
    assert parsed["by_reason"]["too_junior"] == 2
    assert parsed["by_reason"]["too_senior"] == 1
