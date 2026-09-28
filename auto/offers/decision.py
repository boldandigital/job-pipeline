"""
auto/offers/decision.py — Parse operator replies in Discord.

Commands (case-insensitive, surrounding whitespace ignored):

    accept <id>              → set outcome="accepted"
    decline <id>             → set outcome="declined"
    negotiate <id> [notes…]  → keep outcome="offer", append notes
    offer info <id>          → request the offer summary PDF (no state change)

Return value is a ``Decision`` dataclass with ``kind``, ``job_id``, and
``notes`` (for negotiate). Tests assert against ``Decision.kind``:

    from auto.offers.decision import parse_reply, DecisionKind
    cmd = parse_reply("accept 48")
    assert cmd.kind == DecisionKind.ACCEPT
    assert cmd.job_id == 48

Unknown replies return ``Decision(kind=DecisionKind.UNKNOWN, raw=...)`` so the
Discord bot can post "did not understand" without crashing.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Union

log = logging.getLogger("auto.offers.decision")


class DecisionKind(str, Enum):
    ACCEPT = "accept"
    DECLINE = "decline"
    NEGOTIATE = "negotiate"
    INFO = "info"
    PENDING = "pending"
    UNKNOWN = "unknown"


@dataclass
class Decision:
    """Parsed operator reply."""

    kind: DecisionKind
    job_id: Optional[Union[int, str]] = None
    notes: str = ""
    raw: str = ""

    @property
    def is_actionable(self) -> bool:
        return self.kind in (
            DecisionKind.ACCEPT,
            DecisionKind.DECLINE,
            DecisionKind.NEGOTIATE,
        )


# Patterns — anchored, case-insensitive, allow surrounding whitespace.
# Each pattern captures: (1) the job id as a string of digits.
_RE_ACCEPT = re.compile(r"^\s*accept\s+(\d+)\s*$", re.IGNORECASE)
_RE_DECLINE = re.compile(r"^\s*decline\s+(\d+)\s*$", re.IGNORECASE)
_RE_NEGOTIATE = re.compile(
    r"^\s*negotiate\s+(\d+)\b\s*(.*?)\s*$", re.IGNORECASE | re.DOTALL
)
_RE_INFO = re.compile(r"^\s*offer\s+info\s+(\d+)\s*$", re.IGNORECASE)

# Help / fallback aliases
_RE_HELP = re.compile(r"^\s*(help|what|commands?)\s*\??\s*$", re.IGNORECASE)


def parse_reply(raw: str) -> Decision:
    """Parse a single Discord reply line into a ``Decision``.

    Multi-line messages are allowed: we use only the first non-empty line so
    the operator can paste a thread. Returns ``UNKNOWN`` for anything else.
    """
    if not isinstance(raw, str):
        return Decision(kind=DecisionKind.UNKNOWN, raw=str(raw))

    text = raw.strip()
    if not text:
        return Decision(kind=DecisionKind.UNKNOWN, raw=raw)

    # Help?
    if _RE_HELP.match(text):
        return Decision(kind=DecisionKind.UNKNOWN, raw=raw)

    # ACCEPT
    m = _RE_ACCEPT.match(text)
    if m:
        return Decision(
            kind=DecisionKind.ACCEPT,
            job_id=_coerce_job_id(m.group(1)),
            raw=raw,
        )

    # DECLINE
    m = _RE_DECLINE.match(text)
    if m:
        return Decision(
            kind=DecisionKind.DECLINE,
            job_id=_coerce_job_id(m.group(1)),
            raw=raw,
        )

    # NEGOTIATE — optional notes after the id
    m = _RE_NEGOTIATE.match(text)
    if m:
        return Decision(
            kind=DecisionKind.NEGOTIATE,
            job_id=_coerce_job_id(m.group(1)),
            notes=m.group(2).strip(),
            raw=raw,
        )

    # INFO
    m = _RE_INFO.match(text)
    if m:
        return Decision(
            kind=DecisionKind.INFO,
            job_id=_coerce_job_id(m.group(1)),
            raw=raw,
        )

    return Decision(kind=DecisionKind.UNKNOWN, raw=raw)


def _coerce_job_id(s: str) -> Union[int, str]:
    """Job ids are numeric in the sheet, but allow alphanumeric fallback
    (some pipelines slugify them). Keep the str when non-numeric."""
    try:
        return int(s)
    except ValueError:
        return s


# ---------------------------------------------------------------------------
# Sheet-side mutation helpers (consumed by the Discord bot handler — NOT
# this module's responsibility to call the sheet directly; that lives in
# auto.offers.sheet. We expose a thin mapping here so the bot can wire them
# without an extra import.)
# ---------------------------------------------------------------------------


# Map DecisionKind → outcome string written to the sheet's Outcome column.
# This is the canonical mapping; the bot should NOT improvise its own.
DECISION_TO_OUTCOME: dict[DecisionKind, str] = {
    DecisionKind.ACCEPT: "accepted",
    DecisionKind.DECLINE: "declined",
    DecisionKind.NEGOTIATE: "offer",  # stays in offer state, with notes
    DecisionKind.INFO: "offer",       # unchanged; just asks for a PDF
    DecisionKind.UNKNOWN: "offer",
}


# Auto-coloring palette for the Offers tab — spec §3.
DECISION_BG_COLOR: dict[DecisionKind, Optional[dict[str, float]]] = {
    DecisionKind.ACCEPT: {"red": 0.78, "green": 0.95, "blue": 0.78},   # green
    DecisionKind.NEGOTIATE: {"red": 1.0, "green": 0.95, "blue": 0.78}, # yellow
    DecisionKind.DECLINE: {"red": 1.0, "green": 0.82, "blue": 0.85},   # pink
    DecisionKind.INFO: {"red": 1.0, "green": 1.0, "blue": 1.0},        # white
    DecisionKind.PENDING: {"red": 1.0, "green": 1.0, "blue": 1.0},     # white
    DecisionKind.UNKNOWN: {"red": 1.0, "green": 1.0, "blue": 1.0},     # white
}

# Helper kept here so it's discoverable from the bot; sheet.py reads it.
def bg_color_for(kind: DecisionKind) -> Optional[dict[str, float]]:
    return DECISION_BG_COLOR.get(kind)