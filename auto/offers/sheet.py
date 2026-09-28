"""
auto/offers/sheet.py — Write + update the dedicated "Offers" tab.

Two operations:

  * ``append_offer(sheet_id, offer: dict, *, tab_name="Offers")`` — append a
    one-row offer record. Used by the MAIL-1 detector when an outcome
    transitions to "offer".

  * ``update_decision(sheet_id, job_id, kind: DecisionKind, notes: str)`` —
    used by the Discord bot after parsing a ``Decision``. Updates the row's
    ``Decision`` and ``Notes`` columns + applies the matching background
    color (per spec §3 auto-color).

Best-effort: missing credentials / gspread / sheet → return False and log.
Never raises — the bot and mail watcher treat sheet writes as side-effects.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

log = logging.getLogger("auto.offers.sheet")

# Header row per spec §3 (canonical column order).
OFFERS_HEADERS: list[str] = [
    "id",
    "company",
    "role",
    "base_salary",
    "bonus",
    "equity",
    "total_comp",
    "location",
    "remote_days",
    "start_date",
    "decision",
    "notes",
]

# Decision emoji → short string used in the Decision column. Kept stable so
# downstream filter views (e.g. Sheets filter by color) can group on it.
DECISION_DISPLAY: dict[str, str] = {
    "accept": "⭐ accept",
    "negotiate": "🟡 negotiate",
    "decline": "🔴 decline",
    "pending": "❓ pending",
}


@dataclass
class OfferRow:
    """Lightweight schema for an offer record. Validator + row-builder in one."""

    id: Union[int, str]
    company: str
    role: str = ""
    base_salary: Any = ""
    bonus: Any = ""
    equity: Any = ""
    total_comp: Any = ""
    location: str = ""
    remote_days: Any = ""
    start_date: str = ""
    decision: str = "❓ pending"
    notes: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "OfferRow":
        # Tolerate both spec column names and slightly-loose variations
        # ("job_title" → "role", "salary_base" → "base_salary", etc.)
        aliases = {
            "role": ("role", "title", "job_title", "position"),
            "base_salary": ("base_salary", "salary_base", "base"),
            "start_date": ("start_date", "start"),
        }
        kwargs: dict[str, Any] = {
            "id": d.get("id", d.get("job_id", "")),
            "company": d.get("company", ""),
        }
        for spec_key, options in aliases.items():
            for opt in options:
                if opt in d and d[opt] not in (None, ""):
                    kwargs[spec_key] = d[opt]
                    break
            else:
                kwargs.setdefault(spec_key, "")
        # Direct passthroughs
        for k in ("bonus", "equity", "total_comp", "location",
                  "remote_days", "start_date", "decision", "notes"):
            if k in d:
                kwargs[k] = d[k]
        return cls(**kwargs)

    def as_row(self) -> list[str]:
        return [
            str(self.id),
            str(self.company),
            str(self.role),
            str(self.base_salary),
            str(self.bonus),
            str(self.equity),
            str(self.total_comp),
            str(self.location),
            str(self.remote_days),
            str(self.start_date),
            str(self.decision) if self.decision in DECISION_DISPLAY.values()
                else DECISION_DISPLAY.get(self.decision, "❓ pending"),
            str(self.notes),
        ]


# ---------------------------------------------------------------------------
# Sheet helpers — reuse auto.sheet.google_auth, never reimplement auth
# ---------------------------------------------------------------------------


def _open_sheet(sheet_id: str):
    """Return a gspread client + opened sheet, or ``None`` on any failure.

    Avoids duplicating the credential dance that ``auto/sheet/google_auth.py``
    already implements. Returns the gspread ``Spreadsheet`` object (which
    exposes ``worksheet``).
    """
    from auto.sheet.google_auth import (  # type: ignore
        get_sheets_service,
        resolve_credentials_path,
    )

    try:
        resolve_credentials_path()
    except FileNotFoundError as exc:
        log.debug("credentials missing: %s", exc)
        return None

    try:
        gc = get_sheets_service()
        # ``get_sheets_service`` returns a high-level gspread client when
        # gspread is installed; otherwise the low-level googleapiclient
        # resource. We branch — high-level first.
        if hasattr(gc, "open_by_key"):
            return gc.open_by_key(sheet_id)
        # Low-level fallback: caller uses gspread-style API via shim.
        log.debug("gspread unavailable — sheet writes will no-op")
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to open sheet %s: %s", sheet_id, exc)
        return None


def _ensure_tab(sh, tab_name: str, n_cols: int):
    """Return the tab by name, creating it if missing.

    Lazy-imports ``gspread`` so the module is importable (and tests runnable)
    in environments without gspread installed — the exception class is only
    referenced on the missing-tab path, never on the hot path.
    """
    try:
        return sh.worksheet(tab_name)
    except Exception as exc:
        # Only treat the gspread-specific "not found" as a creation trigger;
        # anything else (auth, transport, mocked-side-effect) bubbles up so
        # append_offer's broad except can log + no-op cleanly.
        try:
            import gspread  # type: ignore
        except ImportError:
            # gspread missing → re-raise so callers fall through to no-op.
            raise
        if isinstance(exc, gspread.WorksheetNotFound):
            return sh.add_worksheet(title=tab_name, rows=1, cols=n_cols)
        raise


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def append_offer(
    sheet_id: str,
    offer: dict[str, Any],
    *,
    tab_name: str = "Offers",
) -> Optional[str]:
    """Append one offer row to the Offers tab. Returns the tab URL on success,
    ``None`` if the write can't be done. Never raises.
    """
    sh = _open_sheet(sheet_id)
    if sh is None:
        return None

    try:
        from auto.offers.decision import bg_color_for, DecisionKind
        row = OfferRow.from_dict(offer)

        ws = _ensure_tab(sh, tab_name, n_cols=len(OFFERS_HEADERS))

        # Initialize header row if this is a fresh tab
        existing = ws.get_all_values()
        if not existing:
            ws.append_row(OFFERS_HEADERS)

        values = row.as_row()
        ws.append_row(values)

        # Auto-color the row by decision kind
        kind = _kind_from_decision_label(row.decision)
        color = bg_color_for(kind)
        if color:
            row_idx = len(ws.get_all_values())
            ws.format(f"A{row_idx}:L{row_idx}", {
                "backgroundColor": color,
            })

        return f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit#gid={ws.id}"
    except Exception as exc:  # noqa: BLE001
        log.warning("append_offer failed: %s", exc)
        return None


def update_decision(
    sheet_id: str,
    job_id: Union[int, str],
    kind: str,
    notes: str = "",
    *,
    tab_name: str = "Offers",
) -> bool:
    """Update an existing row's Decision + Notes by id. Returns True on success.

    ``kind`` may be a DecisionKind value, a string like ``"accept"``, or the
    emoji label like ``"⭐ accept"``.
    """
    from auto.offers.decision import DecisionKind, bg_color_for

    sh = _open_sheet(sheet_id)
    if sh is None:
        return False

    try:
        ws = sh.worksheet(tab_name)
    except Exception as exc:  # noqa: BLE001
        log.warning("Tab %r not found: %s", tab_name, exc)
        return False

    try:
        rows = ws.get_all_values()
        if not rows:
            return False
        # Find header positions
        headers = rows[0]
        try:
            id_col = headers.index("id")
            dec_col = headers.index("decision")
            notes_col = headers.index("notes")
        except ValueError as exc:
            log.warning("Headers missing expected columns: %s", exc)
            return False

        target_id = str(job_id)
        target_row: Optional[int] = None
        for i, row in enumerate(rows[1:], start=2):  # 1-based sheet rows
            if id_col < len(row) and row[id_col].strip() == target_id:
                target_row = i
                break
        if target_row is None:
            log.warning("No row with id=%s in tab %r", job_id, tab_name)
            return False

        # Map kind → display label + color
        if isinstance(kind, DecisionKind):
            kind_value = kind.value
        else:
            kind_value = str(kind).lower()
        display = DECISION_DISPLAY.get(kind_value, "❓ pending")

        ws.update_cell(target_row, dec_col + 1, display)
        ws.update_cell(target_row, notes_col + 1, notes or "")

        # Re-color the whole row by the new decision
        bg = bg_color_for(DecisionKind(kind_value))
        if bg:
            ws.format(f"A{target_row}:L{target_row}", {
                "backgroundColor": bg,
            })
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("update_decision failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _kind_from_decision_label(label: str):
    """Inverse of DECISION_DISPLAY — converts ``"⭐ accept"`` → DECISION kind."""
    from auto.offers.decision import DecisionKind

    label = label.strip()
    for key, disp in DECISION_DISPLAY.items():
        if disp == label:
            return DecisionKind(key)
    return DecisionKind.UNKNOWN
