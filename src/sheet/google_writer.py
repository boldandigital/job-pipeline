#!/usr/bin/env python3
"""
ADOPT-10: Google Sheets delivery for the daily job batch.

Writes the scored job batch into a per-date tab on Lars's "Job Pipeline" Sheet
so he can sort by score, mark ★ Approved rows, and feed rejection reasons
back into config/lars.yaml.

Convention (mirrors gpl-love-scraper/scraper/verify_sheet.py):
    - Direct google-api-python-client (NOT gspread)
    - Service account JSON path via env GOOGLE_APPLICATION_CREDENTIALS
      (defaults to the shared Hermes SA at ~/.hermes/credentials/google-service-account.json)
    - Scope: spreadsheets (read + write)
    - Idempotent: re-running the same date overwrites the tab's data block
      instead of appending duplicates or creating "YYYY-MM-DD (1)" siblings.

Row schema (12 columns — see DEFAULT_HEADERS_EN):
    date_added | source | title | company | location | score | llm_score |
    url | career_url | description | status | rejection_reason

The first two columns are writeable from the pipeline; `status` and
`rejection_reason` are filled in by Lars during review (defaults are blank).

Usage (library):
    from src.sheet import create_daily_tab, append_jobs, list_tabs
    svc = get_service()                                     # cached
    create_daily_tab(svc, sheet_id, "2026-09-28")
    append_jobs(svc, sheet_id, "2026-09-28", jobs)

Usage (CLI smoke):
    python -m src.sheet.google_writer --smoke
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Iterable

# Lazy imports — only paid when get_service() is called. Lets unit tests
# import the module without google-api-python-client installed.
try:
    from google.oauth2 import service_account  # type: ignore
    from googleapiclient.discovery import build  # type: ignore
    from googleapiclient.errors import HttpError  # type: ignore
except ImportError:  # pragma: no cover - import-time guard
    service_account = None  # type: ignore
    build = None  # type: ignore
    HttpError = Exception  # type: ignore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sheet.writer")

# ─────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────

# Reuse the Hermes shared service account by default — Lars's gpl-love
# infra already shares it. Override via GOOGLE_APPLICATION_CREDENTIALS.
DEFAULT_SA_PATH = os.path.expanduser(
    "~/.hermes/credentials/google-service-account.json"
)

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"

# Row schema. Order = column order in the tab. Keep aligned with the
# spec in tasks/adopt-10 (12 columns).
DEFAULT_HEADERS_EN = [
    "date_added",
    "source",
    "title",
    "company",
    "location",
    "score",
    "llm_score",
    "url",
    "career_url",
    "description",
    "status",
    "rejection_reason",
]

# Description truncation — Sheets stays snappy, full text lives at the URL.
DESC_MAX_CHARS = 500

# Tab name = date_added for the daily batch. Sheets caps sheet names at
# 100 chars and disallows : \ / ? * [ ]. YYYY-MM-DD is safe.
TAB_DATE_FMT = "%Y-%m-%d"

# How much of a tab we wipe on idempotent re-run. 5000 rows × 12 cols is
# way more than the daily batch cap (50 jobs from config/delivery.yaml).
WIPE_RANGE = "A1:L5000"

# ─────────────────────────────────────────────────────────────────────────
# Service construction
# ─────────────────────────────────────────────────────────────────────────

_service_cache: dict[str, Any] = {}


def get_service(sa_path: str | None = None, scopes: list[str] | None = None):
    """Build a Sheets v4 service. Cached per (sa_path, scopes) tuple.

    Auth = service account JWT. Caller is responsible for sharing the
    target spreadsheet with the SA's client_email (see docs/DAILY-RUN.md §1.3).
    """
    if service_account is None or build is None:
        raise RuntimeError(
            "google-api-python-client not installed. "
            "Run: .venv/bin/python -m pip install google-api-python-client google-auth"
        )

    path = sa_path or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or DEFAULT_SA_PATH
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Service account JSON not found at {path}. "
            "Set GOOGLE_APPLICATION_CREDENTIALS or copy the SA file there."
        )

    effective_scopes = scopes or [SHEETS_SCOPE]
    cache_key = f"{path}::{','.join(effective_scopes)}"
    if cache_key in _service_cache:
        return _service_cache[cache_key]

    creds = service_account.Credentials.from_service_account_file(
        path, scopes=effective_scopes
    )
    # cache_discovery=False avoids the slow gRPC/Discovery bootstrap on every
    # cold start (gpl-love-scraper uses this for the same reason).
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
    _service_cache[cache_key] = svc
    log.info("Sheets service ready (SA=%s, scopes=%s)", path, effective_scopes)
    return svc


# ─────────────────────────────────────────────────────────────────────────
# Tab management
# ─────────────────────────────────────────────────────────────────────────

def list_tabs(svc, sheet_id: str) -> list[dict]:
    """Return all tabs in the spreadsheet as [{name, sheetId, index}, ...]."""
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    tabs = []
    for idx, sh in enumerate(meta.get("sheets", [])):
        props = sh.get("properties", {})
        tabs.append({
            "name": props.get("title", ""),
            "sheetId": props.get("sheetId"),
            "index": props.get("index", idx),
        })
    return tabs


def _ensure_tab(svc, sheet_id: str, tab_name: str) -> dict:
    """Find or create a tab by name. Returns the tab's properties dict.

    Idempotent: if the tab already exists, returns it untouched. If not,
    creates a fresh one. Tab names are unique per spreadsheet, so existence
    is enough — no need to handle 'YYYY-MM-DD (1)' duplicates.
    """
    existing = {t["name"]: t for t in list_tabs(svc, sheet_id)}
    if tab_name in existing:
        log.info("Tab %r already exists — reusing.", tab_name)
        return existing[tab_name]

    log.info("Creating new tab %r in sheet %s", tab_name, sheet_id)
    body = {
        "requests": [
            {
                "addSheet": {
                    "properties": {
                        "title": tab_name,
                        "gridProperties": {"rowCount": 1000, "columnCount": len(DEFAULT_HEADERS_EN)},
                    }
                }
            }
        ]
    }
    resp = svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body=body).execute()
    new_sheet = resp["replies"][0]["addSheet"]["properties"]
    log.info("Tab %r created (sheetId=%s)", tab_name, new_sheet.get("sheetId"))
    return new_sheet


def create_daily_tab(
    svc,
    sheet_id: str,
    date_str: str,
    headers: list[str] | None = None,
) -> dict:
    """Create (or reuse) the YYYY-MM-DD tab and seed headers.

    Args:
        svc: Sheets v4 service from get_service().
        sheet_id: The spreadsheet ID (from the URL between /d/ and /edit).
        date_str: ISO date, e.g. "2026-09-28". Used as the tab name verbatim
                  if it matches TAB_DATE_FMT, otherwise the tab name falls
                  back to today().
        headers: Override the default EN headers. Pass `headers=[]` to skip
                 writing a header row (tests do this).

    Returns:
        Tab properties dict {name, sheetId, index}.
    """
    tab = _ensure_tab(svc, sheet_id, date_str)
    if headers is None:
        headers = DEFAULT_HEADERS_EN

    if headers:
        _write_range(
            svc,
            sheet_id,
            f"{date_str}!A1",
            [list(headers)],
            value_input_option="RAW",
        )
        log.info("Headers written to %r (%d cols)", date_str, len(headers))
    return tab


# ─────────────────────────────────────────────────────────────────────────
# Row construction
# ─────────────────────────────────────────────────────────────────────────

def _truncate(s: str | None, limit: int) -> str:
    if not s:
        return ""
    if len(s) <= limit:
        return s
    return s[: limit - 3] + "..."


def row_from_job(
    job: dict,
    date_str: str | None = None,
    desc_limit: int = DESC_MAX_CHARS,
) -> list[Any]:
    """Map a job dict (SQLite row + extras) to one row in our schema.

    Args:
        job: Dict with at least keys: source, title, company, location,
             score, url, career_url, description. llm_score optional.
        date_str: Defaults to today (UTC). Pass explicitly for tests.
        desc_limit: Truncate description beyond this. Default 500 chars
                    per ADOPT-10 spec.
    """
    when = date_str or datetime.now(timezone.utc).strftime(TAB_DATE_FMT)
    llm = job.get("llm_score")
    return [
        when,
        job.get("source") or "",
        job.get("title") or "",
        job.get("company") or "",
        job.get("location") or "",
        job.get("score") if job.get("score") is not None else "",
        llm if llm is not None else "",
        job.get("url") or "",
        job.get("career_url") or "",
        _truncate(job.get("description") or "", desc_limit),
        "",  # status — Lars fills this in
        "",  # rejection_reason — Lars fills this in
    ]


# ─────────────────────────────────────────────────────────────────────────
# Row write — idempotent overwrite of the data block
# ─────────────────────────────────────────────────────────────────────────

def _write_range(svc, sheet_id: str, a1_range: str, values: list[list[Any]],
                 value_input_option: str = "USER_ENTERED") -> dict:
    body = {"values": values, "majorDimension": "ROWS"}
    return (
        svc.spreadsheets()
        .values()
        .update(
            spreadsheetId=sheet_id,
            range=a1_range,
            valueInputOption=value_input_option,
            body=body,
        )
        .execute()
    )


def append_jobs(
    svc,
    sheet_id: str,
    tab_name: str,
    jobs: Iterable[dict],
    date_str: str | None = None,
) -> dict:
    """Write `jobs` to `tab_name`. Idempotent — clears existing rows first.

    The wipe is scoped to A1:L5000 (well past the daily cap of ~50 jobs).
    Re-running the same date produces the same final state, no duplicates.

    Returns:
        The Sheets API update response (mostly for tests).
    """
    jobs_list = list(jobs)
    if not jobs_list:
        log.info("append_jobs: no jobs to write — leaving tab %r untouched.", tab_name)
        return {"updatedRows": 0, "updatedColumns": 0, "updatedCells": 0}

    # Idempotency: blank the data block first. Headers in row 1 stay.
    try:
        svc.spreadsheets().values().clear(
            spreadsheetId=sheet_id,
            range=f"{tab_name}!{WIPE_RANGE}",
            body={},
        ).execute()
    except HttpError as e:  # pragma: no cover - depends on tab state
        log.warning("clear() failed (probably empty tab) — continuing: %s", e)

    rows = [row_from_job(j, date_str=date_str) for j in jobs_list]
    resp = _write_range(svc, sheet_id, f"{tab_name}!A2", rows)
    log.info(
        "Wrote %d rows to %r (updatedRows=%s)",
        len(rows),
        tab_name,
        resp.get("updatedRows"),
    )
    return resp


# ─────────────────────────────────────────────────────────────────────────
# Read helpers (used by tests + future review tooling)
# ─────────────────────────────────────────────────────────────────────────

def get_rows(
    svc,
    sheet_id: str,
    tab_name: str,
    a1_range: str | None = None,
) -> list[list[str]]:
    """Read raw cell values. Default range = full used range of the tab.

    Returns rows as list-of-lists. Empty strings for unset cells.
    """
    rng = a1_range or tab_name
    result = (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=rng)
        .execute()
    )
    return result.get("values", [])


# ─────────────────────────────────────────────────────────────────────────
# CLI smoke
# ─────────────────────────────────────────────────────────────────────────

def _smoke(sheet_id: str | None) -> int:
    """Auth + read-only sanity probe. Never writes.

    With sheet_id=None: confirm the SA can authenticate and discover APIs.
    With sheet_id set: also list the tab names so we know sharing works.
    """
    svc = get_service()
    log.info("Sheets service constructed OK")
    if not sheet_id:
        log.info("Pass --sheet-id <id> to also probe tab listing.")
        return 0
    tabs = list_tabs(svc, sheet_id)
    log.info("Sheet %s has %d tab(s):", sheet_id, len(tabs))
    for t in tabs:
        log.info("  • %s (sheetId=%s)", t["name"], t["sheetId"])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ADOPT-10 Google Sheets writer")
    parser.add_argument("--smoke", action="store_true", help="Auth probe + (optional) tab listing")
    parser.add_argument("--sheet-id", default=None, help="Spreadsheet ID for smoke probe")

    # ADOPT-10 CLI mode — used by lars-daily-run.sh --sheet
    parser.add_argument("--db", default=None, help="SQLite jobs DB (CLI mode)")
    parser.add_argument("--tab", default=None, help="Tab name, e.g. 2026-09-28 (CLI mode)")
    parser.add_argument("--limit", type=int, default=50, help="Max jobs to write (CLI mode)")
    parser.add_argument("--min-score", type=int, default=20, help="Score threshold (CLI mode)")
    args = parser.parse_args(argv)

    if args.smoke:
        return _smoke(args.sheet_id)

    if args.db and args.tab and args.sheet_id:
        return _write_from_db(
            sheet_id=args.sheet_id,
            db_path=args.db,
            tab_name=args.tab,
            limit=args.limit,
            min_score=args.min_score,
        )

    parser.print_help()
    return 1


def _write_from_db(sheet_id: str, db_path: str, tab_name: str,
                   limit: int, min_score: int) -> int:
    """CLI entry: pull top jobs from SQLite and write them to the given tab."""
    import sqlite3

    if not os.path.exists(db_path):
        log.error("DB not found: %s", db_path)
        return 2

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, title, company, location, url, career_url, score,
               llm_score, source, description
        FROM jobs
        WHERE score >= ? AND status NOT IN ('applied', 'filtered_out', 'batched', 'skipped')
        ORDER BY score DESC
        LIMIT ?
        """,
        (min_score, limit),
    ).fetchall()
    conn.close()

    jobs = [dict(r) for r in rows]
    log.info("CLI: %d jobs selected from %s (min_score=%d limit=%d)",
             len(jobs), db_path, min_score, limit)

    if not jobs:
        log.warning("CLI: no jobs match — nothing to write")
        return 0

    svc = get_service()
    create_daily_tab(svc, sheet_id, tab_name)
    append_jobs(svc, sheet_id, tab_name, jobs, date_str=tab_name)
    log.info("CLI: wrote %d jobs to sheet %s tab %s", len(jobs), sheet_id, tab_name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
