#!/usr/bin/env python3
"""
MAIL-2 — outcome enum + Sheets data validation tests.

Covers the contracts that ``apply_outcome_dropdown`` and
``apply_outcome_colors`` depend on:

  1. ``OUTCOMES`` contains exactly the 7 spec values, in priority order.
  2. ``VALID_OUTCOMES`` is a frozenset superset of those values (used for
     defensive validation in ``lifecycle_cells_from_job``).
  3. ``LIFECYCLE_COLOR_HEX`` maps each outcome to either a 6-digit hex color
     or None (pending).
  4. ``apply_outcome_dropdown`` builds a valid Sheets batchUpdate request:
     ONE_OF_LIST with all 7 user-entered values + range anchored to column Q.
  5. ``apply_outcome_colors`` builds a valid Sheets batchUpdate request:
     one TEXT_EQ boolean rule per non-None outcome, each with a
     backgroundColor in [0, 1] floats.
  6. ``_hex_to_rgb01`` parses "#RRGGBB" and "RRGGBB" alike.

Run:
    .venv/bin/python -m pytest tests/test_outcome_dropdown.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.sheet.google_writer import (  # noqa: E402
    DEFAULT_HEADERS_EN,
    LIFECYCLE_COLOR_HEX,
    OUTCOME_COLUMN_INDEX,
    OUTCOME_COLUMN_LETTER,
    OUTCOMES,
    VALID_OUTCOMES,
    _hex_to_rgb01,
    apply_outcome_colors,
    apply_outcome_dropdown,
    get_service,
)


# ----------------------------------------------------------------------
# 1. Enum coverage
# ----------------------------------------------------------------------

EXPECTED_OUTCOMES = [
    "pending",
    "received",
    "interview",
    "offer",
    "accepted",
    "rejected",
    "declined",
]


def test_outcomes_enum_matches_spec():
    """OUTCOMES is exactly the 7 MAIL-2 spec values, in priority order."""
    assert OUTCOMES == EXPECTED_OUTCOMES


def test_valid_outcomes_covers_enum():
    """VALID_OUTCOMES is the frozen set form of OUTCOMES."""
    assert VALID_OUTCOMES == frozenset(EXPECTED_OUTCOMES)
    assert isinstance(VALID_OUTCOMES, frozenset)


def test_no_extra_outcomes_in_color_map():
    """LIFECYCLE_COLOR_HEX must have entries for every OUTCOMES key,
    and no entries that aren't in OUTCOMES."""
    assert set(LIFECYCLE_COLOR_HEX.keys()) == set(OUTCOMES)


# ----------------------------------------------------------------------
# 2. Color map — pending is None; everything else is a 6-digit hex string
# ----------------------------------------------------------------------

def test_pending_outcome_has_no_color():
    """pending must NOT have a color — default banding applies."""
    assert LIFECYCLE_COLOR_HEX["pending"] is None


@pytest.mark.parametrize("outcome", [o for o in EXPECTED_OUTCOMES if o != "pending"])
def test_non_pending_outcome_has_valid_hex(outcome):
    """All non-pending outcomes must have a 7-char hex color (with #) or 6-char without."""
    color = LIFECYCLE_COLOR_HEX[outcome]
    assert color is not None
    s = color.lstrip("#")
    assert len(s) == 6
    # Parseable as hex (catches accidental alpha channel / 3-char shortcut)
    int(s, 16)


# ----------------------------------------------------------------------
# 3. _hex_to_rgb01 — parser correctness
# ----------------------------------------------------------------------

def test_hex_to_rgb01_with_hash():
    r, g, b = _hex_to_rgb01("#E3F2FD")
    # E3=227, F2=242, FD=253 → /255
    assert (r, g, b) == pytest.approx((227/255, 242/255, 253/255))


def test_hex_to_rgb01_without_hash():
    r, g, b = _hex_to_rgb01("FAEAEA")
    assert (r, g, b) == pytest.approx((250/255, 234/255, 234/255))


def test_hex_to_rgb01_returns_in_unit_interval():
    for hex_color in [c for c in LIFECYCLE_COLOR_HEX.values() if c]:
        r, g, b = _hex_to_rgb01(hex_color)
        for channel in (r, g, b):
            assert 0.0 <= channel <= 1.0


# ----------------------------------------------------------------------
# 4. Outcome column index points at column Q (column 17 = index 16 0-based)
# ----------------------------------------------------------------------

def test_outcome_column_index_matches_default_headers():
    """The constant OUTCOME_COLUMN_INDEX must match DEFAULT_HEADERS_EN."""
    assert OUTCOME_COLUMN_INDEX == DEFAULT_HEADERS_EN.index("outcome") + 1
    assert OUTCOME_COLUMN_LETTER == "Q"


# ----------------------------------------------------------------------
# 5. apply_outcome_dropdown — Sheets batchUpdate request shape
# ----------------------------------------------------------------------

def _fake_dropdown_service(sheet_id_int=42):
    """A MagicMock service that records the batchUpdate body."""
    svc = MagicMock()
    # get(spreadsheetId).execute() returns one tab with sheetId=42
    svc.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"title": "2026-09-28", "sheetId": sheet_id_int, "index": 0}}],
    }
    # batchUpdate(...) -> .execute() returns a canned dict
    svc.spreadsheets.return_value.batchUpdate.return_value.execute.return_value = {
        "replies": [{}],
    }
    return svc


def test_apply_outcome_dropdown_uses_one_of_list_with_all_values():
    svc = _fake_dropdown_service()
    apply_outcome_dropdown(svc, "fake", "2026-09-28")

    batch = svc.spreadsheets.return_value.batchUpdate
    assert batch.call_count == 1
    body = batch.call_args.kwargs["body"]
    requests = body["requests"]
    assert len(requests) == 1
    req = requests[0]
    assert "setDataValidation" in req
    rule = req["setDataValidation"]["rule"]
    cond = rule["condition"]
    assert cond["type"] == "ONE_OF_LIST"
    values = [v["userEnteredValue"] for v in cond["values"]]
    # All 7 outcomes, in priority order, are present.
    assert values == OUTCOMES
    # Strict mode rejects typos, but showCustomUi surfaces Lars-friendly UI.
    assert rule["strict"] is True
    assert rule["showCustomUi"] is True


def test_apply_outcome_dropdown_targets_column_q():
    svc = _fake_dropdown_service(sheet_id_int=99)
    apply_outcome_dropdown(svc, "fake", "2026-09-28")

    body = svc.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]
    req = body["requests"][0]
    rng = req["setDataValidation"]["range"]
    # Column Q is index 16 (0-based) → start/endColumnIndex bracket it
    assert rng["sheetId"] == 99
    assert rng["startColumnIndex"] == OUTCOME_COLUMN_INDEX - 1  # 16
    assert rng["endColumnIndex"] == OUTCOME_COLUMN_INDEX        # 17
    # Header row excluded: data starts at row 2 (0-based index 1)
    assert rng["startRowIndex"] == 1
    assert rng["endRowIndex"] == 5000


# ----------------------------------------------------------------------
# 6. apply_outcome_colors — Sheets conditional-formatting rule shape
# ----------------------------------------------------------------------

def test_apply_outcome_colors_emits_one_per_colored_outcome():
    svc = _fake_dropdown_service()
    apply_outcome_colors(svc, "fake", "2026-09-28")

    body = svc.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]
    requests = body["requests"]
    assert len(requests) == 1
    rules = requests[0]["addConditionalFormatRules"]["rules"]
    # pending has None → skipped; the other 6 outcomes each get one rule.
    colored = [o for o in OUTCOMES if LIFECYCLE_COLOR_HEX[o] is not None]
    assert len(rules) == len(colored)
    emitted_outcomes = [r["booleanRule"]["condition"]["values"][0]["userEnteredValue"] for r in rules]
    assert emitted_outcomes == colored


def test_apply_outcome_colors_use_unit_interval_rgb():
    """Sheets accepts RGB as floats in [0, 1]. All rules must satisfy this."""
    svc = _fake_dropdown_service()
    apply_outcome_colors(svc, "fake", "2026-09-28")

    body = svc.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]
    rules = body["requests"][0]["addConditionalFormatRules"]["rules"]
    for rule in rules:
        bg = rule["booleanRule"]["format"]["backgroundColor"]
        for k in ("red", "green", "blue"):
            assert k in bg, f"missing color channel {k}"
            assert 0.0 <= bg[k] <= 1.0, f"{k}={bg[k]} out of [0, 1]"


def test_apply_outcome_colors_target_column_q():
    svc = _fake_dropdown_service(sheet_id_int=7)
    apply_outcome_colors(svc, "fake", "2026-09-28")

    body = svc.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]
    rules = body["requests"][0]["addConditionalFormatRules"]["rules"]
    for rule in rules:
        rng = rule["ranges"][0]
        assert rng["sheetId"] == 7
        assert rng["startColumnIndex"] == OUTCOME_COLUMN_INDEX - 1
        assert rng["endColumnIndex"] == OUTCOME_COLUMN_INDEX
        assert rng["startRowIndex"] == 1
        assert rng["endRowIndex"] == 5000


def test_apply_outcome_colors_matches_hex_map():
    """Each rule's RGB backgroundColor must round-trip from LIFECYCLE_COLOR_HEX."""
    svc = _fake_dropdown_service()
    apply_outcome_colors(svc, "fake", "2026-09-28")

    body = svc.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]
    rules = body["requests"][0]["addConditionalFormatRules"]["rules"]
    for rule in rules:
        outcome = rule["booleanRule"]["condition"]["values"][0]["userEnteredValue"]
        bg = rule["booleanRule"]["format"]["backgroundColor"]
        r, g, b = _hex_to_rgb01(LIFECYCLE_COLOR_HEX[outcome])
        assert bg["red"] == pytest.approx(r)
        assert bg["green"] == pytest.approx(g)
        assert bg["blue"] == pytest.approx(b)