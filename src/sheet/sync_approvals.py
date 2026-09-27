"""Sheet→DB approval sync (ADOPT-11).

The write side of the Sheet round-trip is :mod:`src.sheet.google_writer`
(ADOPT-10). It writes a 12-column row per job into a per-date tab. Lars
opens the Sheet in his browser, sorts by score, and fills in two columns:

    status           — free text: "approved", "rejected", or left blank
    rejection_reason — enum key from :data:`REJECTION_REASONS` (or free text)

This module reads those columns back and applies them to SQLite ``jobs``
rows. The match key is ``(title, company)`` (the table's UNIQUE constraint)
— robust against Lars deleting rows or reordering the tab.

Three entry points:

    pull_approvals(svc, sheet_id, tab_name) -> list[dict]
    pull_rejections(svc, sheet_id, tab_name) -> list[dict]
    apply_to_db(approvals, rejections, db_path) -> SyncResult
    sync_from_tab(...) -> SyncResult    # convenience: pull + apply

The SyncResult dataclass is intentionally JSON-serializable so the wrapper
script can pipe it straight into the Discord summary line.

Idempotency
-----------
``apply_to_db`` is idempotent on the (title, company) key:

    - If a job was already 'approved' and is still 'approved' in the Sheet,
      ``approved_at`` is NOT overwritten.
    - If a job was 'rejected' and is now 'approved' in the Sheet, the status
      flips AND ``rejection_reason`` is cleared.
    - Free-text rejection notes are stored verbatim in ``rejection_note``.

This means calling ``sync_from_tab`` twice in a row produces the same DB
state. That's the contract.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

# Reuse the writer's constants — single source of truth for the row schema.
from src.sheet.google_writer import (
    DEFAULT_HEADERS_EN,
    get_rows,
    get_service,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sheet.sync")

# ---------------------------------------------------------------------------
# Schema contract — must match google_writer.DEFAULT_HEADERS_EN
# ---------------------------------------------------------------------------

# Column indices into DEFAULT_HEADERS_EN. Computed once at import time so
# the writer can renumber freely without breaking the reader.
_IDX_DATE_ADDED = DEFAULT_HEADERS_EN.index("date_added")
_IDX_SOURCE = DEFAULT_HEADERS_EN.index("source")
_IDX_TITLE = DEFAULT_HEADERS_EN.index("title")
_IDX_COMPANY = DEFAULT_HEADERS_EN.index("company")
_IDX_LOCATION = DEFAULT_HEADERS_EN.index("location")
_IDX_SCORE = DEFAULT_HEADERS_EN.index("score")
_IDX_URL = DEFAULT_HEADERS_EN.index("url")
_IDX_STATUS = DEFAULT_HEADERS_EN.index("status")
_IDX_REJECTION_REASON = DEFAULT_HEADERS_EN.index("rejection_reason")

SHEET_SCHEMA = {
    "headers": DEFAULT_HEADERS_EN,
    "status_column": "status",
    "rejection_reason_column": "rejection_reason",
    "match_key": ("title", "company"),
    "approval_values": {"approved", "approve", "yes", "y", "true", "1", "★"},
    "rejection_values": {"rejected", "reject", "no", "n", "false", "0", "✗", "x"},
}

# ---------------------------------------------------------------------------
# Rejection reason enum
# ---------------------------------------------------------------------------

# Display strings can move to a follow-up card (bilingual EN+DE). The KEYS
# stay stable so analytics in src/analytics/rejection_dashboard.py can group
# counts across runs without text matching.
REJECTION_REASONS: dict[str, str] = {
    "too_junior":        "Too junior for the role",
    "too_senior":        "Overqualified / too senior",
    "wrong_location":    "Wrong location / not remote-friendly",
    "wrong_sector":      "Wrong sector / industry",
    "salary_too_low":    "Salary below floor",
    "language_mismatch": "Required language not a fit",
    "not_interested":    "Not interested (misc)",
    "duplicate":         "Already applied / duplicate",
    "company_blacklist": "Company blacklisted",
    "other":             "Other (see rejection_note)",
}

VALID_REJECTION_KEYS: frozenset[str] = frozenset(REJECTION_REASONS.keys())

# ---------------------------------------------------------------------------
# Result dataclass — JSON-serializable for the Discord summary line
# ---------------------------------------------------------------------------

@dataclass
class SyncResult:
    """Outcome of one ``apply_to_db`` call."""
    tab_name: str
    approved_applied: int = 0
    rejected_applied: int = 0
    # No-op count: rows already in their target state (approved OR rejected)
    # on entry. The name keeps backward compat with the first cut of ADOPT-11.
    skipped_already_approved: int = 0
    skipped_no_match: int = 0
    errors: list[str] = field(default_factory=list)
    unknown_rejection_keys: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total_applied(self) -> int:
        return self.approved_applied + self.rejected_applied

    @property
    def skipped_total(self) -> int:
        """Total rows that were intentionally skipped (idempotent + no match)."""
        return self.skipped_already_approved + self.skipped_no_match

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Pull side — read the Sheet
# ---------------------------------------------------------------------------

def _row_to_record(row: list[str]) -> dict:
    """Project a raw Sheet row into a dict keyed by column name.

    Rows are sparse — Google Sheets omits trailing empty cells. Pad to the
    expected column count so index access doesn't IndexError.
    """
    padded = list(row) + [""] * (len(DEFAULT_HEADERS_EN) - len(row))
    return {
        "date_added":        padded[_IDX_DATE_ADDED],
        "source":            padded[_IDX_SOURCE],
        "title":             padded[_IDX_TITLE],
        "company":           padded[_IDX_COMPANY],
        "location":          padded[_IDX_LOCATION],
        "score":             padded[_IDX_SCORE],
        "url":               padded[_IDX_URL],
        "status":            padded[_IDX_STATUS].strip().lower(),
        "rejection_reason":  padded[_IDX_REJECTION_REASON].strip().lower(),
    }


def _is_approved(record: dict) -> bool:
    return record["status"] in SHEET_SCHEMA["approval_values"]


def _is_rejected(record: dict) -> bool:
    return record["status"] in SHEET_SCHEMA["rejection_values"]


def _pull_rows(svc, sheet_id: str, tab_name: str) -> list[dict]:
    """Fetch all rows from ``tab_name`` as a list of dicts.

    Skips the header row. Returns [] on API failure or empty tab.
    """
    raw = get_rows(svc, sheet_id, tab_name)
    if not raw:
        return []
    header = [str(h).strip() for h in raw[0]]
    # Defensive: if the writer's headers drifted, log and skip silently.
    if header != DEFAULT_HEADERS_EN:
        log.warning(
            "Tab %r header drift — expected %d cols, got %d. Proceeding with positional index.",
            tab_name, len(DEFAULT_HEADERS_EN), len(header),
        )
    records = [_row_to_record(r) for r in raw[1:]]
    # Filter out completely empty rows (Sheet pads rows with single empty cell).
    return [r for r in records if any(r.values())]


def pull_approvals(svc, sheet_id: str, tab_name: str) -> list[dict]:
    """Return rows where ``status`` indicates approval."""
    return [r for r in _pull_rows(svc, sheet_id, tab_name) if _is_approved(r)]


def pull_rejections(svc, sheet_id: str, tab_name: str) -> list[dict]:
    """Return rows where ``status`` indicates rejection.

    Includes the raw rejection_reason field (free text or enum key). Unknown
    enum keys are passed through — the apply step decides whether to map them
    to ``other`` or store verbatim.
    """
    return [r for r in _pull_rows(svc, sheet_id, tab_name) if _is_rejected(r)]


# ---------------------------------------------------------------------------
# Apply side — write to SQLite (idempotent)
# ---------------------------------------------------------------------------

def _open_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _resolve_job_id(conn: sqlite3.Connection, title: str, company: str) -> Optional[int]:
    """Return the SQLite job id for (title, company), or None if not found.

    The match is case-insensitive on both columns — Lars sometimes edits the
    Sheet with different casing than the scraper wrote.
    """
    row = conn.execute(
        """
        SELECT id FROM jobs
         WHERE LOWER(title)   = LOWER(?)
           AND LOWER(company) = LOWER(?)
         LIMIT 1
        """,
        (title, company),
    ).fetchone()
    return row["id"] if row else None


def _normalize_rejection(raw_reason: str) -> tuple[str, str | None]:
    """Map a raw rejection_reason string to (enum_key, free_text_note).

    Rules:
      - Empty/None -> ("other", None). Caller will store "other" + blank note.
      - Already a known enum key -> (key, None).
      - Anything else -> ("other", raw_reason verbatim).
    """
    if not raw_reason:
        return ("other", None)
    key = raw_reason.strip().lower()
    if key in VALID_REJECTION_KEYS:
        return (key, None)
    return ("other", raw_reason.strip())


def apply_to_db(
    approvals: Iterable[dict],
    rejections: Iterable[dict],
    db_path: str,
    *,
    now: Optional[datetime] = None,
) -> SyncResult:
    """Apply Sheet decisions to SQLite ``jobs``. Idempotent.

    Args:
        approvals:    Records from ``pull_approvals``.
        rejections:   Records from ``pull_rejections``.
        db_path:      Path to the SQLite jobs database.
        now:          Override the timestamp used for ``approved_at`` —
                      tests pass a fixed datetime for determinism.

    Returns:
        SyncResult with counts and any errors. Errors don't abort the sync —
        one bad row doesn't lose the rest of Lars's decisions.
    """
    result = SyncResult(tab_name="(in-memory)")
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    now_iso = now.isoformat(sep=" ")

    if not os.path.exists(db_path):
        result.errors.append(f"DB not found at {db_path}")
        return result

    conn = _open_db(db_path)
    try:
        # ── Approvals ────────────────────────────────────────────────────
        for record in approvals:
            title = record.get("title", "")
            company = record.get("company", "")
            if not title or not company:
                result.errors.append(f"approval row missing title/company: {record!r}")
                continue
            job_id = _resolve_job_id(conn, title, company)
            if job_id is None:
                result.skipped_no_match += 1
                log.info("approval: no DB match for (%r, %r) — skipping", title, company)
                continue

            # Idempotency: if already approved, leave approved_at alone.
            row = conn.execute(
                "SELECT status, approved_at FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row["status"] == "approved" and row["approved_at"]:
                result.skipped_already_approved += 1
                continue

            conn.execute(
                """
                UPDATE jobs
                   SET status           = 'approved',
                       approved_at      = ?,
                       rejection_reason = NULL,
                       rejection_note   = NULL,
                       updated_at       = CURRENT_TIMESTAMP
                 WHERE id = ?
                """,
                (now_iso, job_id),
            )
            result.approved_applied += 1
            log.info("approval applied: id=%s (%s @ %s)", job_id, title, company)

        # ── Rejections ───────────────────────────────────────────────────
        for record in rejections:
            title = record.get("title", "")
            company = record.get("company", "")
            if not title or not company:
                result.errors.append(f"rejection row missing title/company: {record!r}")
                continue

            raw_reason = record.get("rejection_reason", "") or ""
            # Track unknown reasons EARLY (before the no-match skip) — the
            # whole point is to surface raw text that doesn't match any
            # enum key, regardless of whether we found the SQLite row.
            if (
                raw_reason.strip()
                and raw_reason.strip().lower() not in VALID_REJECTION_KEYS
            ):
                result.unknown_rejection_keys.append((title, raw_reason))

            job_id = _resolve_job_id(conn, title, company)
            if job_id is None:
                result.skipped_no_match += 1
                log.info("rejection: no DB match for (%r, %r) — skipping", title, company)
                continue

            enum_key, note = _normalize_rejection(raw_reason)
            # _normalize_rejection already routed unknown reasons to
            # ("other", raw_reason), so no further enum-key clobbering
            # needed here.

            # Idempotency: if the row is already 'rejected' with the same
            # reason and note, skip the UPDATE so re-running the sync is
            # a no-op (same contract as approvals).
            row = conn.execute(
                "SELECT status, rejection_reason, rejection_note FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if (
                row["status"] == "rejected"
                and (row["rejection_reason"] or "") == enum_key
                and (row["rejection_note"] or "") == (note or "")
            ):
                result.skipped_already_approved += 1  # see field doc
                continue

            conn.execute(
                """
                UPDATE jobs
                   SET status           = 'rejected',
                       rejection_reason = ?,
                       rejection_note   = ?,
                       approved_at      = NULL,
                       updated_at       = CURRENT_TIMESTAMP
                 WHERE id = ?
                """,
                (enum_key, note, job_id),
            )
            result.rejected_applied += 1
            log.info(
                "rejection applied: id=%s (%s @ %s) reason=%s",
                job_id, title, company, enum_key,
            )

        conn.commit()
    except sqlite3.Error as e:
        result.errors.append(f"SQLite error: {e}")
        log.exception("apply_to_db failed")
    finally:
        conn.close()
    return result


# ---------------------------------------------------------------------------
# One-call convenience
# ---------------------------------------------------------------------------

def sync_from_tab(
    tab_name: str,
    sheet_id: Optional[str] = None,
    db_path: Optional[str] = None,
    *,
    svc=None,
) -> SyncResult:
    """Read ``tab_name`` and apply approvals/rejections to SQLite.

    Args:
        tab_name:  YYYY-MM-DD tab to read.
        sheet_id:  Override GOOGLE_SPREADSHEET_ID.
        db_path:   Path to the SQLite jobs database (default: $DB_PATH or
                   ./data/jobs.db).
        svc:       Optional pre-built Sheets service (for tests).

    Returns:
        SyncResult.
    """
    sheet_id = sheet_id or os.getenv("GOOGLE_SPREADSHEET_ID", "")
    db_path = db_path or os.getenv("DB_PATH", "./data/jobs.db")

    if not sheet_id:
        result = SyncResult(tab_name=tab_name)
        result.errors.append("GOOGLE_SPREADSHEET_ID not set")
        return result

    svc = svc or _get_writer_service(sheet_id)
    if svc is None:
        result = SyncResult(tab_name=tab_name)
        result.errors.append("Sheets API unavailable (creds missing or library not installed)")
        return result

    approvals = pull_approvals(svc, sheet_id, tab_name)
    rejections = pull_rejections(svc, sheet_id, tab_name)
    log.info(
        "sync_from_tab(%s): pulled %d approvals, %d rejections",
        tab_name, len(approvals), len(rejections),
    )
    result = apply_to_db(approvals, rejections, db_path)
    result.tab_name = tab_name
    return result


def _get_writer_service(spreadsheet_id: str):
    """Bridge to google_writer.get_service — its real signature is positional.

    The sync_approvals layer never needs to *write* to the Sheet, but the
    reader's auth contract is the same (scoped read). This wrapper lets the
    rest of sync_approvals stay decoupled from the writer's API surface so
    we can change either without breaking the other.
    """
    try:
        # google_writer.get_service(sa_path: str | None, scopes: list[str] | None)
        # — first positional is the SA path; we accept whatever the env says.
        return get_service()
    except Exception as e:
        log.warning("get_service() failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="ADOPT-11 Sheet→DB approval sync",
    )
    p.add_argument("tab_name", help="Sheet tab name (typically YYYY-MM-DD)")
    p.add_argument(
        "--sheet-id", dest="sheet_id",
        default=os.getenv("GOOGLE_SPREADSHEET_ID", ""),
    )
    p.add_argument(
        "--db", dest="db_path",
        default=os.getenv("DB_PATH", "./data/jobs.db"),
    )
    p.add_argument("--dry-run", action="store_true",
                   help="Print counts without writing to SQLite")
    p.add_argument("--json", action="store_true",
                   help="Output SyncResult as JSON (for piping into Discord)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.dry_run:
        try:
            svc = get_service()
        except Exception as e:
            print(json.dumps({"tab_name": args.tab_name, "error": f"Sheets API unavailable: {e}"}))
            return 2
        if svc is None:
            print(json.dumps({"tab_name": args.tab_name, "error": "Sheets API unavailable (creds missing)"}))
            return 2
        approvals = pull_approvals(svc, args.sheet_id, args.tab_name)
        rejections = pull_rejections(svc, args.sheet_id, args.tab_name)
        print(json.dumps({
            "tab_name": args.tab_name,
            "approvals": len(approvals),
            "rejections": len(rejections),
            "approval_examples": [f"{r['title']} @ {r['company']}" for r in approvals[:5]],
            "rejection_examples": [
                f"{r['title']} @ {r['company']} ({r['rejection_reason']})"
                for r in rejections[:5]
            ],
        }, indent=2, ensure_ascii=False))
        return 0

    result = sync_from_tab(args.tab_name, args.sheet_id, args.db_path)
    if args.json:
        print(result.to_json())
    else:
        print(
            f"⚓ {result.tab_name}: +{result.approved_applied} approved, "
            f"+{result.rejected_applied} rejected "
            f"({result.skipped_already_approved} already-approved, "
            f"{result.skipped_no_match} unmatched)"
        )
        if result.errors:
            print(f"   errors: {len(result.errors)} — {result.errors[:3]}")
    return 0 if not result.errors else 1


if __name__ == "__main__":
    sys.exit(main())
