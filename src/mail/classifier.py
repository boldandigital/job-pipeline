"""MAIL-1 — subject → outcome classifier.

Pure-Python (no I/O), easily unit-tested. Maps an email subject (plus optional
body context) to one of five lifecycle outcomes:

    received    — the application was acknowledged
    interview   — they want to schedule a call
    offer       — they've decided to hire
    rejected    — they passed
    declined    — *we* withdrew (matched on our own sent mail)

Plus a sentinel ``UNKNOWN`` for "update last_email_at only". The watcher
treats UNKNOWN specially — it doesn't bump ``outcome``, only
``last_email_at`` + ``last_email_subject``.

Keyword sets cover EN + DE (DACH replies are almost always in German).
Adding a new keyword is one tuple entry; see docs/MAIL-WATCHER.md.

Order matters: the first pattern set that matches wins. Patterns are
case-insensitive substring matches via regex (with ``re.IGNORECASE``), so
"Re: YOUR APPLICATION — we'd love to chat" still hits the interview set.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List, Tuple

OUTCOME_RECEIVED = "received"
OUTCOME_INTERVIEW = "interview"
OUTCOME_OFFER = "offer"
OUTCOME_REJECTED = "rejected"
OUTCOME_DECLINED = "declined"
OUTCOME_UNKNOWN = "unknown"  # sentinel — update last_email_* only

ALL_OUTCOMES = {
    OUTCOME_RECEIVED,
    OUTCOME_INTERVIEW,
    OUTCOME_OFFER,
    OUTCOME_REJECTED,
    OUTCOME_DECLINED,
}


@dataclass(frozen=True)
class Classification:
    """Structured classifier output.

    Attributes:
        outcome: one of OUTCOME_* (always set; UNKNOWN = no state change).
        signal: human-readable label of which pattern set matched (for
            logging + Discord notifications). Empty when outcome is UNKNOWN.
        matched_keyword: the literal keyword that matched (for debugging
            false positives). Empty when outcome is UNKNOWN.
    """

    outcome: str
    signal: str
    matched_keyword: str

    @property
    def is_state_change(self) -> bool:
        return self.outcome != OUTCOME_UNKNOWN


# ---------------------------------------------------------------------------
# Keyword sets. Each entry is (outcome, label, regex). The regex is wrapped
# in a substring search via re.search, so it can be a plain word. Add new
# ones to the appropriate list — they're tried in order within a set.
# ---------------------------------------------------------------------------

# EN: "received" — application acknowledged but no next step.
_RECEIVED_PATTERNS: List[Tuple[str, str, str]] = [
    (OUTCOME_RECEIVED, "received", r"application received"),
    (OUTCOME_RECEIVED, "received", r"thank you for applying"),
    (OUTCOME_RECEIVED, "received", r"thanks for applying"),
    (OUTCOME_RECEIVED, "received", r"we have received your application"),
    (OUTCOME_RECEIVED, "received", r"your application has been received"),
    (OUTCOME_RECEIVED, "received", r"acknowledgment of your application"),
    (OUTCOME_RECEIVED, "received", r"we received your (?:cv|resume)"),
    (OUTCOME_RECEIVED, "received", r"successfully submitted"),
    (OUTCOME_RECEIVED, "received", r"confirmation of (?:your )?application"),
    (OUTCOME_RECEIVED, "received", r"your application was submitted"),
]

# DE: "Eingangsbestätigung" — gleiche Bedeutung, andere Sprache.
_RECEIVED_PATTERNS_DE: List[Tuple[str, str, str]] = [
    (OUTCOME_RECEIVED, "received", r"eingangsbestätigung"),
    (OUTCOME_RECEIVED, "received", r"vielen dank für (?:ihre|deine) bewerbung"),
    (OUTCOME_RECEIVED, "received", r"danke für (?:ihre|deine) bewerbung"),
    (OUTCOME_RECEIVED, "received", r"wir haben (?:ihre|deine) bewerbung erhalten"),
    (OUTCOME_RECEIVED, "received", r"bewerbung (?:ist|wurde) (?:bei uns )?eingegangen"),
    (OUTCOME_RECEIVED, "received", r"bewerbungseingang"),
]

# EN: "interview" — they want to talk.
_INTERVIEW_PATTERNS: List[Tuple[str, str, str]] = [
    (OUTCOME_INTERVIEW, "interview", r"interview"),
    (OUTCOME_INTERVIEW, "interview", r"schedule a call"),
    (OUTCOME_INTERVIEW, "interview", r"would like to invite"),
    (OUTCOME_INTERVIEW, "interview", r"we'd love to (?:schedule|chat|speak|talk)"),
    (OUTCOME_INTERVIEW, "interview", r"phone screen"),
    (OUTCOME_INTERVIEW, "interview", r"video call"),
    (OUTCOME_INTERVIEW, "interview", r"(?:technical|on[-\s]?site|first|second) round"),
    (OUTCOME_INTERVIEW, "interview", r"next steps"),
    (OUTCOME_INTERVIEW, "interview", r"chat with (?:our|the)"),
    (OUTCOME_INTERVIEW, "interview", r"invitation to interview"),
    (OUTCOME_INTERVIEW, "interview", r"set up (?:a|an) (?:interview|call)"),
]

# DE: "Vorstellungsgespräch" — DACH-Standard.
_INTERVIEW_PATTERNS_DE: List[Tuple[str, str, str]] = [
    (OUTCOME_INTERVIEW, "interview", r"vorstellungsgespräch"),
    (OUTCOME_INTERVIEW, "interview", r"gespräch (?:mit|bei)"),
    (OUTCOME_INTERVIEW, "interview", r"laden (?:sie|dich) (?:zum|zur)"),
    (OUTCOME_INTERVIEW, "interview", r"einladung zum gespräch"),
    (OUTCOME_INTERVIEW, "interview", r"wir (?:möchten|wollen) (?:sie|dich) (?:gerne\s+)?(?:kennenlernen|sprechen|treffen)"),
    (OUTCOME_INTERVIEW, "interview", r"(?:möchten|wollen) (?:sie|dich) (?:gerne\s+)?(?:kennenlernen|sprechen)"),
    (OUTCOME_INTERVIEW, "interview", r"telefoninterview"),
    (OUTCOME_INTERVIEW, "interview", r"telefonisches gespräch"),
    (OUTCOME_INTERVIEW, "interview", r"nächste schritte"),
    (OUTCOME_INTERVIEW, "interview", r"terminvorschlag"),
    (OUTCOME_INTERVIEW, "interview", r"persönliches gespräch"),
]

# EN: "offer" — they decided to hire.
_OFFER_PATTERNS: List[Tuple[str, str, str]] = [
    (OUTCOME_OFFER, "offer", r"we are pleased to offer"),
    (OUTCOME_OFFER, "offer", r"pleased to extend an offer"),
    (OUTCOME_OFFER, "offer", r"job offer"),
    (OUTCOME_OFFER, "offer", r"offer letter"),
    (OUTCOME_OFFER, "offer", r"offer of employment"),
    (OUTCOME_OFFER, "offer", r"we'd like to offer you"),
    (OUTCOME_OFFER, "offer", r"congratulations.*offer"),
    (OUTCOME_OFFER, "offer", r"we are excited to offer"),
    (OUTCOME_OFFER, "offer", r"here(?:'s| is) our offer"),
    (OUTCOME_OFFER, "offer", r"our offer (?:to you|for)"),
]

# DE: "Vertragsangebot".
_OFFER_PATTERNS_DE: List[Tuple[str, str, str]] = [
    (OUTCOME_OFFER, "offer", r"vertragsangebot"),
    (OUTCOME_OFFER, "offer", r"stellenangebot"),
    (OUTCOME_OFFER, "offer", r"wir (?:freuen uns|können ihnen)[\s,]+(?:ihnen )?ein angebot"),
    (OUTCOME_OFFER, "offer", r"(?:freuen uns|können ihnen)[\s,]+(?:ihnen )?ein angebot"),
    (OUTCOME_OFFER, "offer", r"angebot (?:für|fur) (?:ihre|deine)"),
    (OUTCOME_OFFER, "offer", r"herzlichen glückwunsch.*angebot"),
]

# EN: "rejected" — they passed.
_REJECTED_PATTERNS: List[Tuple[str, str, str]] = [
    (OUTCOME_REJECTED, "rejected", r"unfortunately"),
    (OUTCOME_REJECTED, "rejected", r"we regret"),
    (OUTCOME_REJECTED, "rejected", r"not moving forward"),
    (OUTCOME_REJECTED, "rejected", r"will not be moving forward"),
    (OUTCOME_REJECTED, "rejected", r"position has been filled"),
    (OUTCOME_REJECTED, "rejected", r"role has been filled"),
    (OUTCOME_REJECTED, "rejected", r"decided to pursue other candidates"),
    (OUTCOME_REJECTED, "rejected", r"not (?:a|the right) match"),
    (OUTCOME_REJECTED, "rejected", r"after careful consideration"),
    (OUTCOME_REJECTED, "rejected", r"we have decided not to"),
    (OUTCOME_REJECTED, "rejected", r"unable to offer you"),
]

# DE: "Absage".
_REJECTED_PATTERNS_DE: List[Tuple[str, str, str]] = [
    (OUTCOME_REJECTED, "rejected", r"absage"),
    (OUTCOME_REJECTED, "rejected", r"leider"),
    (OUTCOME_REJECTED, "rejected", r"bedauerlicherweise"),
    (OUTCOME_REJECTED, "rejected", r"wir (?:müssen|müssen ihnen leider)"),
    (OUTCOME_REJECTED, "rejected", r"position (?:ist|wurde) (?:bereits\s+)?besetzt"),
    (OUTCOME_REJECTED, "rejected", r"stelle (?:ist|wurde) (?:bereits\s+)?besetzt"),
    (OUTCOME_REJECTED, "rejected", r"nicht (?:in|frage) (?:kommt|passt)"),
    (OUTCOME_REJECTED, "rejected", r"keine passende"),
    (OUTCOME_REJECTED, "rejected", r"wurde (?:nicht )?berücksichtigt"),
    (OUTCOME_REJECTED, "rejected", r"andere (?:kandidat|profile)"),
]

# "declined" — *we* sent the withdrawing email.
# Matched by the matcher when the email is in the Sent folder or the sender
# is our own address; classifier only triggers on substring as a belt-and-
# suspenders fallback in case a colleague forwards our draft back.
_DECLINED_PATTERNS: List[Tuple[str, str, str]] = [
    (OUTCOME_DECLINED, "declined", r"withdrawing my application"),
    (OUTCOME_DECLINED, "declined", r"no longer interested"),
    (OUTCOME_DECLINED, "declined", r"withdraw (?:my|our) (?:application|candidature)"),
    (OUTCOME_DECLINED, "declined", r"please accept this as my withdrawal"),
]


# ---------------------------------------------------------------------------
# Combined priority order. Order matters within each outcome (first match
# wins) but outcomes themselves are independent — an "interview" email
# whose subject happens to mention "unfortunately" elsewhere still
# classifies as interview because interview patterns come first in the
# ordered sweep.
# ---------------------------------------------------------------------------
_PRIORITY_ORDER: List[Tuple[str, List[Tuple[str, str, str]]]] = [
    ("interview", _INTERVIEW_PATTERNS + _INTERVIEW_PATTERNS_DE),
    ("offer",     _OFFER_PATTERNS + _OFFER_PATTERNS_DE),
    ("rejected",  _REJECTED_PATTERNS + _REJECTED_PATTERNS_DE),
    ("declined",  _DECLINED_PATTERNS),
    ("received",  _RECEIVED_PATTERNS + _RECEIVED_PATTERNS_DE),
]


def classify(subject: str, body: str = "") -> Classification:
    """Map an email (subject + optional body) to a lifecycle outcome.

    Args:
        subject: the ``Subject:`` header value (decoded). May be empty.
        body:    the email body, used only when subject gives no match —
                 still rare in practice (most recruiting threads live or
                 die by subject line). Empty when not available.

    Returns:
        Classification(outcome, signal, matched_keyword). Always set;
        UNKNOWN when nothing fired.
    """
    haystack = (subject or "") + "\n" + (body or "")
    for _signal_label, patterns in _PRIORITY_ORDER:
        for outcome, signal, regex in patterns:
            if re.search(regex, haystack, re.IGNORECASE):
                return Classification(
                    outcome=outcome,
                    signal=signal,
                    matched_keyword=regex,
                )
    return Classification(
        outcome=OUTCOME_UNKNOWN,
        signal="",
        matched_keyword="",
    )


# ---------------------------------------------------------------------------
# Test-friendly introspection — expose the keyword counts so test_mail_
# classifier.py can assert "at least 10 EN + 10 DE keywords covered".
# ---------------------------------------------------------------------------

def keyword_stats() -> dict:
    """Return per-language per-outcome keyword counts. Used by tests +
    the docs to keep coverage honest."""
    return {
        "received": {
            "en": len(_RECEIVED_PATTERNS),
            "de": len(_RECEIVED_PATTERNS_DE),
        },
        "interview": {
            "en": len(_INTERVIEW_PATTERNS),
            "de": len(_INTERVIEW_PATTERNS_DE),
        },
        "offer": {
            "en": len(_OFFER_PATTERNS),
            "de": len(_OFFER_PATTERNS_DE),
        },
        "rejected": {
            "en": len(_REJECTED_PATTERNS),
            "de": len(_REJECTED_PATTERNS_DE),
        },
        "declined": {
            "en": len(_DECLINED_PATTERNS),
            "de": 0,
        },
    }


def all_keywords() -> Iterable[str]:
    """Flat list of every keyword regex — handy for unit-test fuzzing."""
    out: list[str] = []
    for _, patterns in _PRIORITY_ORDER:
        out.extend(p[2] for p in patterns)
    return out