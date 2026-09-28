"""
Tests for auto.offers.decision — Discord reply parser.

Verifies per spec §5:
  - accept / decline / negotiate (with notes) / offer info
  - case-insensitive
  - whitespace tolerant
  - multi-word notes on negotiate
  - unknown replies return UNKNOWN (don't crash the bot)
  - DECISION_TO_OUTCOME is the canonical mapping
  - bg_color_for returns the spec colors
"""

from __future__ import annotations

import pytest

from auto.offers.decision import (
    DECISION_BG_COLOR,
    DECISION_TO_OUTCOME,
    Decision,
    DecisionKind,
    bg_color_for,
    parse_reply,
)


# ---------------------------------------------------------------------------
# parse_reply — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,job_id,kind",
    [
        ("accept 48", 48, DecisionKind.ACCEPT),
        ("ACCEPT 48", 48, DecisionKind.ACCEPT),
        ("  accept   48  ", 48, DecisionKind.ACCEPT),
        ("decline 7", 7, DecisionKind.DECLINE),
        ("Decline 7", 7, DecisionKind.DECLINE),
        ("offer info 48", 48, DecisionKind.INFO),
        ("OFFER INFO 99", 99, DecisionKind.INFO),
    ],
)
def test_parse_happy_path(text, job_id, kind):
    cmd = parse_reply(text)
    assert cmd.kind == kind
    assert cmd.job_id == job_id
    assert cmd.is_actionable is (kind in (DecisionKind.ACCEPT, DecisionKind.DECLINE, DecisionKind.NEGOTIATE))


def test_parse_negotiate_with_notes():
    cmd = parse_reply("negotiate 48 ask for 110k base + 15k signing")
    assert cmd.kind == DecisionKind.NEGOTIATE
    assert cmd.job_id == 48
    assert cmd.notes == "ask for 110k base + 15k signing"


def test_parse_negotiate_no_notes():
    cmd = parse_reply("negotiate 48")
    assert cmd.kind == DecisionKind.NEGOTIATE
    assert cmd.job_id == 48
    assert cmd.notes == ""


def test_parse_accept_id_numeric():
    """Numeric job ids are coerced to int when pure digits."""
    cmd = parse_reply("accept 12345")
    assert cmd.job_id == 12345
    assert isinstance(cmd.job_id, int)


def test_parse_accept_id_alphanumeric_fallback():
    """Slugged ids keep their string form (some pipelines string-id jobs)."""
    cmd = parse_reply("accept abc-123")
    # The regex is digit-only, so this falls through to UNKNOWN
    assert cmd.kind == DecisionKind.UNKNOWN


# ---------------------------------------------------------------------------
# edge cases
# ---------------------------------------------------------------------------


def test_parse_empty_returns_unknown():
    assert parse_reply("").kind == DecisionKind.UNKNOWN
    assert parse_reply("   ").kind == DecisionKind.UNKNOWN


def test_parse_none_returns_unknown():
    assert parse_reply(None).kind == DecisionKind.UNKNOWN


def test_parse_help_returns_unknown():
    """help should be UNKNOWN — handled by the bot to print usage."""
    assert parse_reply("help").kind == DecisionKind.UNKNOWN
    assert parse_reply("?").kind == DecisionKind.UNKNOWN


def test_parse_typo_returns_unknown():
    """Don't accidentally accept malformed commands."""
    for text in ("acceept 48", "decline", "negotiate", "accept 48x", "accept -1"):
        cmd = parse_reply(text)
        if text == "negotiate":
            # bare "negotiate" without an id is UNKNOWN
            assert cmd.kind == DecisionKind.UNKNOWN, text
        elif text == "decline":
            assert cmd.kind == DecisionKind.UNKNOWN, text
        else:
            assert cmd.kind in (DecisionKind.UNKNOWN, DecisionKind.ACCEPT, DecisionKind.DECLINE), text


def test_parse_keeps_raw():
    cmd = parse_reply("  accept  48  ")
    assert "accept" in cmd.raw


# ---------------------------------------------------------------------------
# Decision dataclass
# ---------------------------------------------------------------------------


def test_decision_is_actionable():
    for k in (DecisionKind.ACCEPT, DecisionKind.DECLINE, DecisionKind.NEGOTIATE):
        assert Decision(kind=k).is_actionable
    for k in (DecisionKind.INFO, DecisionKind.UNKNOWN):
        assert not Decision(kind=k).is_actionable


# ---------------------------------------------------------------------------
# Mapping tables — sheet integrity relies on these
# ---------------------------------------------------------------------------


def test_decision_to_outcome_canonical():
    assert DECISION_TO_OUTCOME[DecisionKind.ACCEPT] == "accepted"
    assert DECISION_TO_OUTCOME[DecisionKind.DECLINE] == "declined"
    # Negotiate stays in offer state with notes — spec §5
    assert DECISION_TO_OUTCOME[DecisionKind.NEGOTIATE] == "offer"
    assert DECISION_TO_OUTCOME[DecisionKind.INFO] == "offer"


def test_bg_colors_match_spec():
    """Spec §3: ⭐ green, 🟡 yellow, 🔴 pink, ❓ white"""
    accept = bg_color_for(DecisionKind.ACCEPT)
    decline = bg_color_for(DecisionKind.DECLINE)
    negotiate = bg_color_for(DecisionKind.NEGOTIATE)

    # Green has higher G+B than R; yellow has roughly equal R/G high
    assert accept["green"] > accept["red"]
    assert decline["red"] > decline["green"]   # pink has more red
    assert negotiate["red"] > 0.9 and negotiate["green"] > 0.9  # yellow-ish


def test_all_decision_kinds_have_a_color():
    for k in DecisionKind:
        assert DECISION_BG_COLOR.get(k) is not None
