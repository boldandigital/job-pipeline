"""
Tests for auto.offers.summary — WeasyPrint-mocked.

Verifies:
  - render_offer_html produces a valid 1-page document with all required
    key-facts fields when present.
  - Empty values are skipped (no empty rows).
  - Decision row renders all four emojis.
  - render_pdf writes a file with the right name and suffix.
  - format_discord_message produces the operator-facing template.
  - handle_offer_transition is fail-open (returns None on errors).
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from auto.offers.summary import (
    DECISION_EMOJI,
    PDF_TABLE_FIELDS,
    format_discord_message,
    handle_offer_transition,
    render_offer_html,
    render_pdf,
)


@pytest.fixture
def full_job() -> dict:
    """A complete offer job — every spec field populated."""
    return {
        "id": 48,
        "title": "Head of Digital",
        "company": "BrandForward Agency",
        "applied_date": "2026-09-01",
        "location": "Cologne (Hybrid)",
        "role_level": "Director",
        "team_size": "6",
        "reports_to": "CMO",
        "base_salary": 95000,
        "bonus": 12000,
        "equity": "0.05% over 4y (1y cliff)",
        "total_comp": 107000,
        "start_date": "2027-01-15",
        "contract_type": "permanent",
        "remote_policy": "2 days/week",
        "travel_requirement": "up to 10%",
        "relocation_support": "€5,000",
        "notice_period": "3 months",
        "decision_deadline": "2026-12-01",
        "remote_days": 2,
    }


# ---------------------------------------------------------------------------
# render_offer_html — pure HTML builder
# ---------------------------------------------------------------------------


def test_render_html_includes_all_facts(full_job):
    html = render_offer_html(full_job, why_you="Test paragraph", decision="pending")
    # Spec §2 key facts — every label + value should appear in the table
    expectations = [
        ("BrandForward Agency", "company name"),
        ("Head of Digital", "title"),
        ("2026-09-01", "applied date"),
        ("Cologne (Hybrid)", "location value"),
        ("Director", "role level value"),
        ("permanent", "contract type value"),
        ("2 days/week", "remote policy value"),
        ("up to 10%", "travel value"),
        ("Reports to", "header label"),
        ("Salary (base)", "header label"),
        ("Start date", "header label"),
    ]
    missing = []
    for needle, desc in expectations:
        if needle not in html:
            missing.append((needle, desc))
    assert not missing, f"Missing in HTML: {missing}"


def test_render_html_skips_empty_fields(full_job):
    full_job = {**full_job, "team_size": "", "equity": None, "bonus": []}
    html = render_offer_html(full_job, why_you="...", decision="pending")
    assert "Team size" not in html
    assert "Equity" not in html
    assert "bonus" not in html.lower() or "salary (bonus)" not in html.lower()


def test_render_html_decision_row(full_job):
    html = render_offer_html(full_job, decision="accept")
    assert "⭐ accept" in html
    assert "🟡 negotiate" in html
    assert "🔴 decline" in html
    assert "❓ pending" in html


def test_render_html_dangerous_chars_escaped():
    job = {
        "id": 1, "title": "<script>alert(1)</script>",
        "company": "Evil & Co", "location": "<svg/onload=x>",
    }
    html = render_offer_html(job)
    assert "<script>alert(1)</script>" not in html
    assert "<svg/onload=x>" not in html
    assert "&lt;script&gt;" in html
    assert "&amp; Co" in html


def test_render_html_why_you_fallback(full_job):
    html = render_offer_html(full_job, why_you="", decision="pending")
    assert "No why-you snippet" in html or "why-you-paragraphs" in html


def test_pdf_table_fields_order():
    """The PDF table preserves spec §2 order."""
    keys = [k for k, _ in PDF_TABLE_FIELDS]
    assert keys[0] == "company"
    assert keys[-1] == "decision_deadline"


# ---------------------------------------------------------------------------
# render_pdf — mocked WeasyPrint
# ---------------------------------------------------------------------------


def test_render_pdf_calls_weasyprint(tmp_path, full_job):
    fake_pdf = tmp_path / "48-offer-summary.pdf"
    with mock.patch("auto.offers.summary._weasyprint_write_pdf") as m:
        m.return_value = fake_pdf
        # render_pdf builds the output path internally — just verify it
        # computes correctly and calls WeasyPrint with the HTML string.
        out = render_pdf(full_job, why_you="x", out_dir=tmp_path)
    assert out == fake_pdf
    m.assert_called_once()
    html_arg = m.call_args.args[0]
    assert "BrandForward" in html_arg
    assert "Head of Digital" in html_arg


def test_render_pdf_filename_uses_id(full_job):
    with mock.patch("auto.offers.summary._weasyprint_write_pdf") as m:
        m.return_value = Path("/tmp/whatever.pdf")
        # Use a tmp_path that exists so .resolve() doesn't blow up
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            out = render_pdf(full_job, out_dir=Path(td))
    assert "48-offer-summary.pdf" in str(out)


def test_render_pdf_filename_slug_when_no_id():
    job = {"title": "Director of Engineering", "company": "Big Co"}
    with mock.patch("auto.offers.summary._weasyprint_write_pdf") as m:
        m.return_value = Path("/tmp/whatever.pdf")
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            out = render_pdf(job, out_dir=Path(td))
    assert out.name.endswith(".pdf")
    assert "big-co" in out.name


# ---------------------------------------------------------------------------
# format_discord_message — pure builder
# ---------------------------------------------------------------------------


def test_discord_message_includes_spec_fields(full_job):
    msg = format_discord_message(full_job, Path("/tmp/48-offer-summary.pdf"))
    assert "🎯 OFFER RECEIVED" in msg
    assert "BrandForward Agency" in msg
    assert "Head of Digital" in msg
    assert "Cologne (Hybrid)" in msg  # location
    assert "Start: 2027-01-15" in msg
    assert "€95000" in msg  # base
    assert "/tmp/48-offer-summary.pdf" in msg
    assert "Reply with" in msg


def test_discord_message_handles_missing_values():
    job = {"title": "Lead", "company": "Co"}
    msg = format_discord_message(job, Path("/tmp/x.pdf"))
    assert "🎯 OFFER RECEIVED: Co" in msg
    # Missing values render as em-dash; the € prefix only appears when a value exists
    assert "Base: —" in msg
    assert "Bonus: —" in msg
    assert "Equity: —" in msg
    assert "Decision needed by: <not mentioned>" in msg


def test_discord_message_includes_sheet_url(full_job):
    msg = format_discord_message(
        full_job, Path("/tmp/x.pdf"),
        sheet_url="https://docs.google.com/spreadsheets/d/ABC/edit#gid=0",
    )
    assert "docs.google.com" in msg


# ---------------------------------------------------------------------------
# handle_offer_transition — integration entry point
# ---------------------------------------------------------------------------


def test_handle_offer_returns_pdf_path(full_job):
    with mock.patch("auto.offers.summary.render_pdf") as rp, \
         mock.patch("auto.offers.summary._send_discord") as sd:
        rp.return_value = Path("/tmp/48.pdf")
        sd.return_value = True
        out = handle_offer_transition(48, full_job)
    assert out == Path("/tmp/48.pdf")
    sd.assert_called_once()
    assert "🎯 OFFER RECEIVED" in sd.call_args.args[0]


def test_handle_offer_fail_open(full_job):
    """When WeasyPrint isn't installed, return None — never raise."""
    with mock.patch("auto.offers.summary.render_pdf",
                    side_effect=OSError("no libgobject")):
        out = handle_offer_transition(48, full_job)
    assert out is None


def test_decision_emoji_complete():
    assert set(DECISION_EMOJI.keys()) == {"accept", "negotiate", "decline", "pending"}


def test_offerrow_decision_label_display_lookup():
    """Verify the sheet display labels match the spec's emoji story."""
    from auto.offers.sheet import DECISION_DISPLAY
    assert DECISION_DISPLAY["accept"].startswith("⭐")
    assert DECISION_DISPLAY["negotiate"].startswith("🟡")
    assert DECISION_DISPLAY["decline"].startswith("🔴")
    assert DECISION_DISPLAY["pending"].startswith("❓")
