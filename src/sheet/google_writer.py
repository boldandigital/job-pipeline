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
# spec in tasks/adopt-10 (12 columns) + MAIL-2 (7 lifecycle columns M→S).
DEFAULT_HEADERS_EN = [
    "date_added",        # A
    "source",            # B
    "title",             # C
    "company",           # D
    "location",          # E
    "score",             # F
    "llm_score",         # G
    "url",               # H
    "career_url",        # I
    "description",       # J
    "status",            # K
    "rejection_reason",  # L
    # --- MAIL-2 lifecycle columns (M → S) ---
    "mail_received_at",  # M  first time any reply was matched (idempotent)
    "last_email_subject",# N  most recent inbound mail subject (truncated 80ch)
    "last_email_at",     # O  most recent inbound mail timestamp
    "interview_at",      # P  best-effort parse from invite body
    "outcome",           # Q  dropdown: see OUTCOMES + LIFECYCLE_COLOR_HEX
    "salary_range",      # R  free-text ("€80-100k")
    "cv_version",        # S  CV identifier ("v3-hosting-cto-2026-09-25")
]

# Description truncation — Sheets stays snappy, full text lives at the URL.
DESC_MAX_CHARS = 500

# Email-subject truncation for the last_email_subject column. Sheets allows
# ~50k chars per cell but 80 chars is plenty for visual scanning.
EMAIL_SUBJECT_MAX_CHARS = 80

# Tab name = date_added for the daily batch. Sheets caps sheet names at
# 100 chars and disallows : \ / ? * [ ]. YYYY-MM-DD is safe.
TAB_DATE_FMT = "%Y-%m-%d"

# How much of a tab we wipe on idempotent re-run. 5000 rows × 19 cols is
# way more than the daily batch cap (50 jobs from config/delivery.yaml).
# MAIL-2: 12 → 19 cols (added 7 lifecycle columns).
WIPE_RANGE = "A1:S5000"

# Column index of the outcome column (1-based for Sheets A1 notation).
# 17 = column Q (12 + 5 appended cols = column 17 = Q).
OUTCOME_COLUMN_LETTER = "Q"
OUTCOME_COLUMN_INDEX = DEFAULT_HEADERS_EN.index("outcome") + 1  # 0-based → 1-based

# ─────────────────────────────────────────────────────────────────────────
# MAIL-2: outcome enum + color coding for the Google Sheet dropdown
# ─────────────────────────────────────────────────────────────────────────

# Lifecycle states Lars cares about, in priority order (used as Sheet rows
# default sort: oldest active states first, terminal states last).
OUTCOMES: list[str] = [
    "pending",    # default for legacy NULL rows — backfilled by migrate_lifecycle
    "received",   # got an auto-reply / "we received your application"
    "interview",  # invited to interview
    "offer",      # offer received, decision pending
    "accepted",   # Lars accepted the offer
    "rejected",   # company rejected us
    "declined",   # Lars declined or withdrew
]

VALID_OUTCOMES: frozenset[str] = frozenset(OUTCOMES)

# Hex colors (no leading #) for the conditional-formatting rule on the
# outcome column. None = no color (default banding). Sheets conditional
# formatting accepts {backgroundColor: {red, green, blue}} where each
# channel is in [0, 1]. The colors below match the MAIL-2 spec.
LIFECYCLE_COLOR_HEX: dict[str, str | None] = {
    "pending":   None,        # no color (default banding)
    "received":  "#E3F2FD",   # light blue
    "interview": "#E1D5F5",   # light purple
    "offer":     "#D9F2D9",   # light green
    "accepted":  "#A8E6A8",   # solid green
    "rejected":  "#FAEAEA",   # pink
    "declined":  "#E0E0E0",   # gray
}


def _hex_to_rgb01(hex_color: str) -> tuple[float, float, float]:
    """Convert "#RRGGBB" or "RRGGBB" to (r, g, b) floats in [0, 1]."""
    s = hex_color.lstrip("#")
    return (
        int(s[0:2], 16) / 255.0,
        int(s[2:4], 16) / 255.0,
        int(s[4:6], 16) / 255.0,
    )

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

    The 7 trailing cells (M→S) are the MAIL-2 lifecycle columns. They
    default to empty strings so :func:`append_jobs` can ship a fresh
    daily batch without any mail-stage data — ``sync_lifecycle`` will
    patch these columns later.
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
        # MAIL-2 lifecycle columns (M→S) — defaults are blank for fresh rows.
        # sync_lifecycle() patches these on a second pass.
        "",  # mail_received_at
        "",  # last_email_subject
        "",  # last_email_at
        "",  # interview_at
        "",  # outcome
        "",  # salary_range
        "",  # cv_version
    ]


def lifecycle_cells_from_job(
    job: dict,
    email_subject_limit: int = EMAIL_SUBJECT_MAX_CHARS,
) -> list[Any]:
    """Return the 7 MAIL-2 lifecycle cells (M→S) for a job dict.

    The order matches DEFAULT_HEADERS_EN[12:19]:
        mail_received_at | last_email_subject | last_email_at |
        interview_at | outcome | salary_range | cv_version

    Used by :func:`sync_lifecycle` to patch the M:S columns of an
    existing row without touching A:L. Empty strings for None values
    so the Sheet stays clean.
    """
    outcome = job.get("outcome") or ""
    if outcome and outcome not in VALID_OUTCOMES:
        # Defensive: a watcher regression could write a typo'd value. We
        # still emit it (Sheets validation will reject on edit), but
        # log so the issue surfaces in the daily run.
        log.warning(
            "lifecycle_cells_from_job: unknown outcome %r on id=%s",
            outcome, job.get("id"),
        )
    return [
        job.get("mail_received_at") or "",
        _truncate(job.get("last_email_subject") or "", email_subject_limit),
        job.get("last_email_at") or "",
        job.get("interview_at") or "",
        outcome,
        job.get("salary_range") or "",
        job.get("cv_version") or "",
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

    The wipe is scoped to A1:S5000 (well past the daily cap of ~50 jobs).
    Re-running the same date produces the same final state, no duplicates.
    The 7 MAIL-2 lifecycle columns (M→S) start blank — ``sync_lifecycle``
    patches them later.

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
# MAIL-2: lifecycle sync (DB → Sheet) + outcome dropdown + color coding
# ─────────────────────────────────────────────────────────────────────────

# Columns that ``sync_lifecycle`` reads from the DB. Used both as the SELECT
# column list and to position values into the right cell offsets.
LIFECYCLE_DB_COLUMNS: tuple[str, ...] = (
    "id",
    "title",
    "company",
    "mail_received_at",
    "last_email_subject",
    "last_email_at",
    "interview_at",
    "outcome",
    "salary_range",
    "cv_version",
)

# Match key for which Sheet row holds which DB job. We join on
# (title, company) — same key ADOPT-11's sync_approvals uses, robust
# against row reordering and case drift.
_SHEET_KEY_INDICES = (
    DEFAULT_HEADERS_EN.index("title"),
    DEFAULT_HEADERS_EN.index("company"),
)
# Indices of the 7 lifecycle columns inside DEFAULT_HEADERS_EN.
_LIFECYCLE_COL_INDICES = tuple(
    DEFAULT_HEADERS_EN.index(c)
    for c in DEFAULT_HEADERS_EN
    if c in LIFECYCLE_DB_COLUMNS[3:]  # skip id/title/company
)


def _load_existing_rows(
    svc, sheet_id: str, tab_name: str
) -> list[list[str]]:
    """Fetch all current rows from ``tab_name`` (including header).

    Returns ``[]`` if the tab is missing or empty. Sheets pads rows with
    trailing empty cells when reading — we re-pad to ``len(DEFAULT_HEADERS_EN)``
    so positional indexing doesn't IndexError on shorter rows.
    """
    raw = get_rows(svc, sheet_id, tab_name)
    if not raw:
        return []
    ncols = len(DEFAULT_HEADERS_EN)
    padded = []
    for r in raw:
        row = list(r) + [""] * (ncols - len(r))
        padded.append(row[:ncols])
    return padded


def _row_key(row: list[str]) -> tuple[str, str]:
    """Return (title, company) — lowercased for case-insensitive join."""
    title = row[_SHEET_KEY_INDICES[0]].strip().lower()
    company = row[_SHEET_KEY_INDICES[1]].strip().lower()
    return (title, company)


def sync_lifecycle(
    svc,
    sheet_id: str,
    tab_name: str,
    conn=None,
    *,
    db_path: str | None = None,
) -> dict:
    """Push MAIL-2 lifecycle state from SQLite to the Sheet's M→S columns.

    Reads jobs where ``mail_received_at IS NOT NULL OR outcome IS NOT NULL
    OR applied_at IS NOT NULL`` and patches the matching Sheet rows
    (matched on (title, company)). Only M→S cells are written — A→L is
    never touched. Rows that don't appear in the DB are left alone.

    Idempotency: per-cell diff. We compute the new lifecycle cells for each
    matched job, compare against the current Sheet values, and only issue
    updates for cells that differ. A second call with unchanged DB state
    is a no-op (zero ``values.update`` calls).

    Args:
        svc: Sheets v4 service from get_service().
        sheet_id: Spreadsheet ID.
        tab_name: Tab to patch (typically YYYY-MM-DD).
        conn: Pre-opened sqlite3.Connection with row_factory=sqlite3.Row.
              If None, ``db_path`` is used to open one.
        db_path: Path to jobs.db. Ignored if ``conn`` is provided.

    Returns:
        Summary dict for the callback:
        {
            "tab":        str,
            "matched":    int,  # DB rows that joined a Sheet row
            "unmatched":  int,  # DB rows with no Sheet row (logged, not fatal)
            "patched":    int,  # cells updated
            "no_op":      int,  # cells checked but already equal
        }
    """
    summary = {
        "tab": tab_name,
        "matched": 0,
        "unmatched": 0,
        "patched": 0,
        "no_op": 0,
    }

    # ── Read the Sheet (header + data) ─────────────────────────────────
    existing = _load_existing_rows(svc, sheet_id, tab_name)
    if len(existing) <= 1:
        log.info(
            "sync_lifecycle: tab %r has no data rows — nothing to patch.",
            tab_name,
        )
        return summary
    # Build (key → row index in DEFAULT_HEADERS_EN, 0-based from row 2 in the Sheet)
    # We index by sheet_row (1-based A1 row number) to write back directly.
    key_to_sheet_row: dict[tuple[str, str], int] = {}
    for sheet_row_idx, row in enumerate(existing[1:], start=2):
        key_to_sheet_row[_row_key(row)] = sheet_row_idx

    # ── Read the DB ────────────────────────────────────────────────────
    if conn is None:
        if db_path is None:
            raise ValueError("sync_lifecycle: provide conn or db_path")
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        close_conn = True
    else:
        close_conn = False

    try:
        cols_sql = ", ".join(LIFECYCLE_DB_COLUMNS)
        db_rows = conn.execute(
            f"""
            SELECT {cols_sql}
              FROM jobs
             WHERE mail_received_at IS NOT NULL
                OR outcome IS NOT NULL
                OR applied_at IS NOT NULL
            """,
        ).fetchall()
    finally:
        if close_conn:
            conn.close()

    if not db_rows:
        log.info("sync_lifecycle: no DB rows with lifecycle state — done.")
        return summary

    # ── Diff + collect patches ────────────────────────────────────────
    # Batched updates — one values.update per Sheet row that has any
    # diff, writing only M:S cells.
    patches: list[tuple[int, list[Any]]] = []
    for r in db_rows:
        key = (r["title"].strip().lower(), r["company"].strip().lower())
        sheet_row = key_to_sheet_row.get(key)
        if sheet_row is None:
            summary["unmatched"] += 1
            log.info(
                "sync_lifecycle: no Sheet match for %r @ %r (id=%s)",
                r["title"], r["company"], r["id"],
            )
            continue
        summary["matched"] += 1

        job_dict = dict(r)
        new_cells = lifecycle_cells_from_job(job_dict)
        current_row = existing[sheet_row - 1]  # back to 0-based
        cell_diffs: list[Any] = []
        any_diff = False
        for offset, col_idx in enumerate(_LIFECYCLE_COL_INDICES):
            current_val = current_row[col_idx] if col_idx < len(current_row) else ""
            new_val = new_cells[offset]
            if (current_val or "") != (new_val or ""):
                cell_diffs.append(new_val)
                any_diff = True
                summary["patched"] += 1
            else:
                cell_diffs.append(current_val)  # write-back no-op (skip)
                summary["no_op"] += 1
        if any_diff:
            patches.append((sheet_row, cell_diffs))

    # ── Apply patches ──────────────────────────────────────────────────
    if not patches:
        log.info(
            "sync_lifecycle: %d matched, 0 cells to patch (all current).",
            summary["matched"],
        )
        return summary

    # Each patch row needs its own A1 range (single row, M{S}:S{S}).
    # We batch into a single ``batchUpdate`` call to keep the API cost
    # down (1 quota unit vs N).
    start_col_idx = _LIFECYCLE_COL_INDICES[0] + 1  # 1-based for A1
    end_col_idx = _LIFECYCLE_COL_INDICES[-1] + 1
    start_letter = _col_idx_to_letter(start_col_idx)
    end_letter = _col_idx_to_letter(end_col_idx)
    body = []
    for sheet_row, cells in patches:
        body.append({
            "range": f"{tab_name}!{start_letter}{sheet_row}:{end_letter}{sheet_row}",
            "values": [cells],
        })
    resp = (
        svc.spreadsheets()
        .values()
        .batchUpdate(spreadsheetId=sheet_id, body={"valueInputOption": "USER_ENTERED", "data": body})
        .execute()
    )
    log.info(
        "sync_lifecycle: matched=%d patched=%d cells across %d rows "
        "(unmatched=%d no_op=%d)",
        summary["matched"], summary["patched"], len(patches),
        summary["unmatched"], summary["no_op"],
    )
    return summary


def _col_idx_to_letter(idx_1based: int) -> str:
    """Convert 1-based column index to Excel letter (1→A, 13→M, 19→S, etc.)."""
    s = ""
    n = idx_1based
    while n > 0:
        n, rem = divmod(n - 1, 26)
        s = chr(65 + rem) + s
    return s


def apply_outcome_dropdown(
    svc, sheet_id: str, tab_name: str
) -> dict:
    """Add a data-validation dropdown to the outcome column (Q) of ``tab_name``.

    Range covers rows 2..5000 (header row 1 is excluded). The dropdown
    values are the 7 ``OUTCOMES`` enum keys. ``strict=True`` rejects any
    out-of-enum value — but ``showCustomUi=True`` is left at default so
    Lars sees a helpful warning if he types a typo (Sheets surfaces the
    default rejection UI).

    Idempotent — calling twice produces the same final state. The Sheets
    API treats ``setDataValidation`` as an upsert keyed on the rule's
    range, so we just call it.

    Returns:
        The Sheets API ``batchUpdate`` response (mostly for tests).
    """
    sheet_props = _ensure_tab(svc, sheet_id, tab_name)
    sheet_id_int = sheet_props.get("sheetId")
    if sheet_id_int is None:
        raise RuntimeError(f"tab {tab_name!r} has no sheetId — cannot apply dropdown")
    return (
        svc.spreadsheets()
        .batchUpdate(
            spreadsheetId=sheet_id,
            body={
                "requests": [
                    {
                        "setDataValidation": {
                            "range": {
                                "sheetId": sheet_id_int,
                                "startRowIndex": 1,    # skip header row
                                "endRowIndex": 5000,
                                "startColumnIndex": OUTCOME_COLUMN_INDEX - 1,
                                "endColumnIndex": OUTCOME_COLUMN_INDEX,
                            },
                            "rule": {
                                "condition": {
                                    "type": "ONE_OF_LIST",
                                    "values": [
                                        {"userEnteredValue": v} for v in OUTCOMES
                                    ],
                                },
                                "inputMessage": "Pick one of: "
                                + ", ".join(OUTCOMES),
                                "strict": True,
                                "showCustomUi": True,
                            },
                        }
                    }
                ]
            },
        )
        .execute()
    )


def apply_outcome_colors(
    svc, sheet_id: str, tab_name: str
) -> dict:
    """Apply conditional formatting to the outcome column (Q).

    Each OUTCOMES value gets its color from ``LIFECYCLE_COLOR_HEX``.
    ``pending`` has None color and is skipped — the Sheet's default
    banding applies.

    Idempotent — the API replaces rules at the start of the requested
    range. We add one rule per non-None outcome in stable order, so
    repeated calls produce the same rule list.

    Returns:
        The Sheets API ``batchUpdate`` response (mostly for tests).
    """
    sheet_props = _ensure_tab(svc, sheet_id, tab_name)
    sheet_id_int = sheet_props.get("sheetId")
    if sheet_id_int is None:
        raise RuntimeError(f"tab {tab_name!r} has no sheetId — cannot apply colors")
    rules = []
    # Stable order: OUTCOMES list order (matters for the rule priority —
    # Sheets applies rules top-down and stops at the first match).
    for outcome in OUTCOMES:
        hex_color = LIFECYCLE_COLOR_HEX.get(outcome)
        if not hex_color:
            continue  # pending — no color
        r, g, b = _hex_to_rgb01(hex_color)
        rules.append({
            "ranges": [{
                "sheetId": sheet_id_int,
                "startRowIndex": 1,
                "endRowIndex": 5000,
                "startColumnIndex": OUTCOME_COLUMN_INDEX - 1,
                "endColumnIndex": OUTCOME_COLUMN_INDEX,
            }],
            "booleanRule": {
                "condition": {
                    "type": "TEXT_EQ",
                    "values": [{"userEnteredValue": outcome}],
                },
                "format": {
                    "backgroundColor": {"red": r, "green": g, "blue": b},
                },
            },
        })
    if not rules:
        log.warning("apply_outcome_colors: no rules — LIFECYCLE_COLOR_HEX empty?")
    return (
        svc.spreadsheets()
        .batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"addConditionalFormatRules": {"rules": rules}}]},
        )
        .execute()
    )


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
    # MAIL-2 — push DB lifecycle state (M:S columns) into the tab.
    parser.add_argument(
        "--sync-lifecycle", action="store_true",
        help="Patch the tab's M:S columns from the jobs DB. Pairs with --sheet.",
    )
    args = parser.parse_args(argv)

    if args.smoke:
        return _smoke(args.sheet_id)

    if args.db and args.tab and args.sheet_id:
        # MAIL-2: --sync-lifecycle runs after the daily batch write so the
        # tab already exists when sync_lifecycle reads it back.
        rc = _write_from_db(
            sheet_id=args.sheet_id,
            db_path=args.db,
            tab_name=args.tab,
            limit=args.limit,
            min_score=args.min_score,
        )
        if rc != 0 or not args.sync_lifecycle:
            return rc
        # Lazy import to avoid pulling googleapiclient when unused.
        from src.sheet.google_writer import (  # noqa: E402 - local import
            sync_lifecycle as _sync_lifecycle,
        )
        try:
            svc = get_service()
        except Exception as e:
            log.error("sync-lifecycle: get_service() failed: %s", e)
            return rc or 1
        summary = _sync_lifecycle(svc, args.sheet_id, args.tab, db_path=args.db)
        log.info(
            "sync-lifecycle summary: matched=%d patched=%d unmatched=%d no_op=%d",
            summary.get("matched", 0), summary.get("patched", 0),
            summary.get("unmatched", 0), summary.get("no_op", 0),
        )
        return rc

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
