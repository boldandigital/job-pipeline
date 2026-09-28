-- =============================================================================
-- JOB SEARCH PIPELINE — Canonical SQLite Schema
-- =============================================================================
-- Single source of truth for the `jobs` table.
--
-- History:
--   - Originally defined inline in arbeitsagentur_scraper / stepstone_scraper /
--     import_jobspy.save_to_db()
--   - ADOPT-6 added career_url + description (used by ATS discovery + scoring)
--   - ADOPT-9 unified into this file + src/db/__init__.py:init_db()
--   - ADOPT-11 added approved_at + rejection_reason for the Sheet→DB approval
--     feedback loop (sync_approvals.py). approved/rejected jobs are excluded
--     from future scoring.
--   - MAIL-1 added outcome + mail_received_at + last_email_subject +
--     last_email_at + interview_at for the iCloud IMAP reply classifier
--     (src/mail/watcher.py). The companion migration script
--     scripts/migrate_schema_add_mail_columns.py ALTER-TABLE-adds these to
--     pre-existing DBs; new DBs inherit them directly from this schema.
--   - MAIL-2 added salary_range + cv_version + idx_jobs_outcome so the daily
--     Google Sheet can surface the full received→interview→offer→accepted
--     pipeline at a glance. Outcome enum was extended to 7 values
--     ('pending' is the default backfill for legacy NULL rows). The migration
--     scripts/migrate_schema_add_lifecycle.py ALTER-TABLE-adds the two
--     columns + index on pre-existing DBs.
--
-- Idempotent: every statement uses IF NOT EXISTS. Safe to re-execute.
-- Run via:  conn.executescript(open("src/db/schema.sql").read())
-- =============================================================================

CREATE TABLE IF NOT EXISTS jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    title               TEXT    NOT NULL,
    company             TEXT    NOT NULL,
    location            TEXT,
    url                 TEXT,
    career_url          TEXT,
    source              TEXT,
    description         TEXT,
    search_query        TEXT,
    score               INTEGER DEFAULT 0,
    ats_type            TEXT,
    status              TEXT    DEFAULT 'new',
    cv_path             TEXT,
    cover_letter_path   TEXT,
    applied_at          TIMESTAMP,

    -- LLM scoring (ADOPT-9 consolidation — was previously added at runtime by
    -- llm_scorer.migrate_db(); now part of the canonical schema so fresh DBs
    -- inherit them without ALTER TABLE churn)
    llm_score           INTEGER,
    llm_reasoning       TEXT,
    llm_match_reason    TEXT,
    llm_scores_json     TEXT,
    llm_scored_at       TIMESTAMP,

    -- ADOPT-11: Sheet→DB approval loop. Set by sync_approvals.py when Lars
    -- marks rows ★ Approved / ✗ Rejected in the daily Google Sheet.
    -- approved_at: timestamp of first approval (idempotent — never overwritten
    --               on re-sync).
    -- rejection_reason: enum key from REJECTION_REASONS (sync_approvals.py) —
    --                   'too_junior', 'too_senior', 'wrong_location', etc.
    --                   Free-text fallback stored as the 'other' enum with the
    --                   verbatim string in rejection_note.
    -- rejection_note: optional free-text supplement when reason='other'.
    approved_at         TIMESTAMP,
    rejection_reason    TEXT,
    rejection_note      TEXT,

    -- MAIL-1: iCloud IMAP watcher reply-classifier outputs.
    --   outcome:           'received' | 'interview' | 'offer' | 'rejected' |
    --                      'declined' | 'accepted' | 'pending'
    --                      (MAIL-2 expanded to 7 values; 'pending' is the
    --                       default for legacy NULL rows — backfilled by
    --                       scripts/migrate_schema_add_lifecycle.py).
    --   mail_received_at:  first time any reply was matched to this row
    --                      (idempotent — never overwritten by re-polls)
    --   last_email_*:      last time ANY reply was matched (for debugging +
    --                      "did the watcher run today?" sanity checks)
    --   interview_at:      best-effort date parsed from the invite body
    --                      (NULL if not found / not parseable)
    outcome             TEXT,
    mail_received_at    TIMESTAMP,
    last_email_subject  TEXT,
    last_email_at       TIMESTAMP,
    interview_at        TIMESTAMP,

    -- MAIL-2: extended lifecycle state surfaced via the daily Google Sheet.
    -- These two are populated downstream of MAIL-1 (the mail watcher) — the
    -- schema just needs them so sync_lifecycle.py can write + read.
    --
    --   salary_range:  free-text offer compensation ("€80-100k"). Usually
    --                  parsed from the offer email body by MAIL-1; may also
    --                  be hand-edited in the Sheet.
    --   cv_version:    CV identifier submitted ("v3-hosting-cto-2026-09-25").
    --                  Joined back to master/templates/* on the apply side;
    --                  optional — NULL until the apply pipeline stamps it.
    salary_range        TEXT,
    cv_version          TEXT,

    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(title, company)
);

CREATE INDEX IF NOT EXISTS idx_jobs_status         ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_score          ON jobs(score DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_created        ON jobs(created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_source         ON jobs(source);
CREATE INDEX IF NOT EXISTS idx_jobs_llm            ON jobs(llm_score DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_company_outcome ON jobs(company, outcome);
CREATE INDEX IF NOT EXISTS idx_jobs_outcome        ON jobs(outcome);