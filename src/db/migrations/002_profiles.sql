-- =============================================================================
-- 002_profiles.sql — multi-profile support (Phase 2.7)
--
-- REFERENCE ONLY. Nothing in the codebase executes this file.
--
-- The real schema lives in Python:
--   * src/db/profiles_db.py  -> SCHEMA (CREATE TABLE IF NOT EXISTS, idempotent)
--                               + _PROFILE_CV_MIGRATIONS (gated ALTERs)
--   * src/db/jobs_db.py      -> jobs.profile_id (in the CREATE TABLE + the
--                               _GATE_MIGRATIONS list)
--
-- This document exists to document the intended shape in one place. Apply it
-- by hand ONLY if you ever need to create the tables outside the app; the app
-- self-migrates and running this manually risks drifting from the Python.
-- =============================================================================

-- Per-user profile. A user may own many (one per role they apply to);
-- exactly one is flagged is_default (enforced in code, not by the DB).
CREATE TABLE IF NOT EXISTS profiles (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    name            TEXT NOT NULL,          -- e.g. "CTO · Berlin SaaS"
    kind            TEXT NOT NULL DEFAULT 'role',  -- 'role' | 'default'
    headline        TEXT,
    summary         TEXT,
    experience_json TEXT,                   -- [{company, role, start, end, bullets}]
    skills_json     TEXT,                   -- ["Hosting", "DNS", ...]
    links_json      TEXT,                   -- [{kind, url}]
    is_default      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_profiles_user   ON profiles(user_id);
CREATE INDEX IF NOT EXISTS idx_profiles_default ON profiles(user_id, is_default);

-- One or more base CVs per profile. The first upload for a profile becomes
-- its default; set_default_cv moves the flag. file_path is stored relative
-- to profiles_db.PROFILE_CV_ROOT (data/users/), not the repo root.
CREATE TABLE IF NOT EXISTS profile_cvs (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    profile_id  TEXT NOT NULL,
    label       TEXT,                       -- "Base CTO", "EU Format", ...
    filename    TEXT NOT NULL,              -- original uploaded name
    file_path   TEXT NOT NULL,              -- relative to PROFILE_CV_ROOT
    mime_type   TEXT,                       -- application/pdf, ...docx
    size_bytes  INTEGER NOT NULL,
    is_default  INTEGER NOT NULL DEFAULT 0,
    uploaded_at TEXT NOT NULL,
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_profile_cvs_profile ON profile_cvs(profile_id);
CREATE INDEX IF NOT EXISTS idx_profile_cvs_default ON profile_cvs(profile_id, is_default);

-- Per-job profile binding: NULL means "use the user's default profile at
-- render time". A job can point at a specific profile (e.g. a CTO
-- application reuses the CTO CV, not the agency one).
--
-- Applied by jobs_db as a gated ALTER on existing DBs:
ALTER TABLE jobs ADD COLUMN profile_id TEXT;