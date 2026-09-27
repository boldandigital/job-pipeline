"""Google Sheets delivery surface (ADOPT-10/11).

ADOPT-10 ships the WRITE side (this package) — daily scored batch lands in a
per-date tab in Lars's "Job Pipeline" Sheet. The 12-column row schema is
documented in :mod:`src.sheet.google_writer` (``DEFAULT_HEADERS_EN``).

ADOPT-11 ships the READ side (:mod:`src.sheet.sync_approvals`) — it pulls
``status`` + ``rejection_reason`` columns back from each tab and writes them
to SQLite ``jobs.status`` + ``jobs.rejection_reason`` + ``jobs.approved_at``.

Public surface:

    google_writer:
        - get_service()               -> Sheets v4 service (cached per process)
        - create_daily_tab(...)       -> creates YYYY-MM-DD tab, idempotent
        - append_jobs(...)            -> writes rows; overwrites existing data
        - list_tabs(...)              -> names of all tabs
        - get_rows(...)               -> raw values, used by tests + sync
        - row_from_job(...)           -> SQLite row -> Sheet row

    sync_approvals (ADOPT-11):
        - SHEET_SCHEMA                -> the row contract (matches writer)
        - REJECTION_REASONS           -> enum keys + display strings
        - pull_approvals(...)         -> read ★ rows from a tab
        - pull_rejections(...)        -> read ✗ rows + reasons
        - apply_to_db(...)            -> idempotent SQLite write
        - sync_from_tab(...)          -> one-call convenience (pull+apply)
"""

from src.sheet.google_writer import (
    SHEETS_SCOPE,
    DEFAULT_HEADERS_EN,
    get_service,
    create_daily_tab,
    append_jobs,
    list_tabs,
    get_rows,
    row_from_job,
)

from src.sheet.sync_approvals import (
    SHEET_SCHEMA,
    REJECTION_REASONS,
    pull_approvals,
    pull_rejections,
    apply_to_db,
    sync_from_tab,
)

__all__ = [
    # google_writer
    "SHEETS_SCOPE",
    "DEFAULT_HEADERS_EN",
    "get_service",
    "create_daily_tab",
    "append_jobs",
    "list_tabs",
    "get_rows",
    "row_from_job",
    # sync_approvals
    "SHEET_SCHEMA",
    "REJECTION_REASONS",
    "pull_approvals",
    "pull_rejections",
    "apply_to_db",
    "sync_from_tab",
]
