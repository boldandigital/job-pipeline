"""
Tests for auto.offers.sheet — Offers tab writer.

Most tests mock the gspread client so they run offline. The pure-validator
tests use OfferRow.from_dict / as_row to verify the canonical row schema.
"""

from __future__ import annotations
import os
from unittest import mock

import pytest

from auto.offers.decision import DecisionKind
from auto.offers.sheet import (
    DECISION_DISPLAY,
    OFFERS_HEADERS,
    OfferRow,
    append_offer,
    update_decision,
)


OFFERS_COL_COUNT = len(OFFERS_HEADERS)


# ---------------------------------------------------------------------------
# OfferRow validator + row serializer
# ---------------------------------------------------------------------------


def test_offerrow_from_dict_strict():
    offer = {
        "id": 48,
        "company": "BrandForward",
        "role": "Head of Digital",
        "base_salary": 95000,
        "bonus": 12000,
        "equity": "0.05%",
        "total_comp": 107000,
        "location": "Cologne",
        "remote_days": 2,
        "start_date": "2027-01-15",
        "decision": "❓ pending",
        "notes": "",
    }
    row = OfferRow.from_dict(offer)
    assert row.id == 48
    assert row.company == "BrandForward"
    assert row.role == "Head of Digital"
    assert row.base_salary == 95000
    assert row.decision == "❓ pending"
    assert OFFERS_HEADERS == [
        "id", "company", "role", "base_salary", "bonus", "equity",
        "total_comp", "location", "remote_days", "start_date",
        "decision", "notes",
    ]


def test_offerrow_from_dict_aliases():
    """Spec column names vs loose variations (title → role, etc.)."""
    offer = {
        "id": 1,
        "company": "Acme",
        "title": "Director of Engineering",
        "salary_base": 100000,
    }
    row = OfferRow.from_dict(offer)
    assert row.role == "Director of Engineering"
    assert row.base_salary == 100000


def test_offerrow_as_row_length():
    offer = {"id": 1, "company": "x"}
    row = OfferRow.from_dict(offer)
    cells = row.as_row()
    assert len(cells) == len(OFFERS_HEADERS)


def test_offerrow_decision_label_normalised():
    offer = {"id": 1, "company": "x", "decision": "accept"}
    row = OfferRow.from_dict(offer)
    cells = row.as_row()  # as_row applies emoji label normalisation
    # decision is the 11th column (index 10)
    assert "⭐" in cells[10]


def test_offerrow_decision_pending_default():
    offer = {"id": 1, "company": "x"}
    row = OfferRow.from_dict(offer)
    cells = row.as_row()
    assert "❓" in cells[10]


# ---------------------------------------------------------------------------
# append_offer — mocked gspread
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_sheet():
    """A mock gspread Spreadsheet with a `Offers` worksheet."""
    ws = mock.MagicMock()
    ws.id = 999
    ws.get_all_values.return_value = []   # empty → triggers header write

    sh = mock.MagicMock()
    sh.worksheet.return_value = ws
    # _ensure_tab always returns the same ws (we're not testing creation here)
    return sh, ws


def test_append_offer_writes_header_and_row(fake_sheet, monkeypatch):
    sh, ws = fake_sheet
    with mock.patch("auto.offers.sheet._open_sheet", return_value=sh):
        url = append_offer("SHEET-ID", {
            "id": 48, "company": "BrandForward", "role": "Head of Digital",
        })
    assert url and "SHEET-ID" in url
    ws.append_row.assert_called()
    # Header row uses OFFERS_HEADERS in order
    first_call = ws.append_row.call_args_list[0]
    assert first_call.args[0] == OFFERS_HEADERS


def test_append_offer_skips_when_no_credentials(monkeypatch):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "")
    with mock.patch(
        "auto.offers.sheet._open_sheet", return_value=None,
    ) as m:
        url = append_offer("SHEET-ID", {"id": 1, "company": "x"})
    assert url is None


def test_append_offer_preserves_existing_header(fake_sheet):
    sh, ws = fake_sheet
    # Simulate a tab that already has the header → don't rewrite it
    ws.get_all_values.return_value = [OFFERS_HEADERS]
    with mock.patch("auto.offers.sheet._open_sheet", return_value=sh):
        append_offer("ID", {"id": 1, "company": "x"})
    # Only one append_row call (the data row), not the header
    assert ws.append_row.call_count == 1
    data_args = ws.append_row.call_args_list[0].args[0]
    assert data_args[0] == "1"  # id
    assert data_args[1] == "x"  # company


def test_append_offer_applies_color(fake_sheet):
    sh, ws = fake_sheet
    ws.get_all_values.return_value = [OFFERS_HEADERS]  # header pre-exists
    # After append, length of get_all_values() should be 2 → formatting row 2
    ws.get_all_values.side_effect = lambda: [[OFFERS_HEADERS], ["1", "x", "", "", "", "", "", "", "", "", "❓ pending", ""]]
    with mock.patch("auto.offers.sheet._open_sheet", return_value=sh):
        append_offer("ID", {"id": 1, "company": "x"})
    ws.format.assert_called()
    a1_arg = ws.format.call_args.args[0]
    assert a1_arg.startswith("A") and ":L" in a1_arg   # A:L range


# ---------------------------------------------------------------------------
# update_decision — mocked gspread
# ---------------------------------------------------------------------------


def test_update_decision_writes_decision_and_notes(monkeypatch):
    ws = mock.MagicMock()
    ws.get_all_values.return_value = [
        OFFERS_HEADERS,
        ["48", "BrandForward", "", "", "", "", "", "", "", "", "❓ pending", ""],
    ]
    sh = mock.MagicMock()
    sh.worksheet.return_value = ws
    with mock.patch("auto.offers.sheet._open_sheet", return_value=sh):
        ok = update_decision("ID", "48", "accept", notes="signing bonus plz")
    assert ok is True
    # update_cell called twice (decision + notes)
    assert ws.update_cell.call_count == 2
    # First call should set the decision column
    first_call = ws.update_cell.call_args_list[0]
    assert "⭐" in first_call.args[2] or "accept" in first_call.args[2]


def test_update_decision_missing_row(monkeypatch):
    ws = mock.MagicMock()
    ws.get_all_values.return_value = [OFFERS_HEADERS]
    sh = mock.MagicMock()
    sh.worksheet.return_value = ws
    with mock.patch("auto.offers.sheet._open_sheet", return_value=sh):
        ok = update_decision("ID", "999", "accept")
    assert ok is False
    ws.update_cell.assert_not_called()


def test_update_decision_handles_enum(monkeypatch):
    ws = mock.MagicMock()
    ws.get_all_values.return_value = [
        OFFERS_HEADERS,
        ["48", "BrandForward", "", "", "", "", "", "", "", "", "❓ pending", ""],
    ]
    sh = mock.MagicMock()
    sh.worksheet.return_value = ws
    with mock.patch("auto.offers.sheet._open_sheet", return_value=sh):
        ok = update_decision("ID", "48", DecisionKind.DECLINE)
    assert ok is True
    decision_value = ws.update_cell.call_args_list[0].args[2]
    assert "decline" in decision_value or "🔴" in decision_value


def test_update_decision_no_credentials(monkeypatch):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "")
    with mock.patch("auto.offers.sheet._open_sheet", return_value=None):
        ok = update_decision("ID", "48", "accept")
    assert ok is False


def test_update_decision_missing_tab(monkeypatch):
    sh = mock.MagicMock()
    sh.worksheet.side_effect = Exception("not found")
    with mock.patch("auto.offers.sheet._open_sheet", return_value=sh):
        ok = update_decision("ID", "48", "accept")
    assert ok is False


def test_update_decision_no_headers_row(monkeypatch):
    ws = mock.MagicMock()
    ws.get_all_values.return_value = []  # empty tab
    sh = mock.MagicMock()
    sh.worksheet.return_value = ws
    with mock.patch("auto.offers.sheet._open_sheet", return_value=sh):
        ok = update_decision("ID", "48", "accept")
    assert ok is False


def test_update_decision_recolors_row(monkeypatch):
    ws = mock.MagicMock()
    ws.get_all_values.return_value = [
        OFFERS_HEADERS,
        ["48", "x", "", "", "", "", "", "", "", "", "❓ pending", ""],
    ]
    sh = mock.MagicMock()
    sh.worksheet.return_value = ws
    with mock.patch("auto.offers.sheet._open_sheet", return_value=sh):
        update_decision("ID", "48", "decline")
    # Format must be called for the row
    ws.format.assert_called()
