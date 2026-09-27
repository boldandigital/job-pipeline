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

    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(title, company)
);

CREATE INDEX IF NOT EXISTS idx_jobs_status  ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_score   ON jobs(score DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_source  ON jobs(source);
CREATE INDEX IF NOT EXISTS idx_jobs_llm     ON jobs(llm_score DESC);
