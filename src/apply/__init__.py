"""ADOPT-12: CUA-based job application submission.

This package wraps the desktop CUA driver with ATS-specific adapters that
fill, upload, and submit job applications on Lars's behalf.

Architecture (per ADOPT-12 brief):

    Approved jobs (status='approved' from ADOPT-11)
            |
            v
    [For each approved job:]
    1. Load job from SQLite
    2. Render CV + Anschreiben via ats_templates.py
    3. Detect ATS (from careers.ats_type column populated by ADOPT-14)
    4. Open career_url in CUA browser
    5. Run ATS-specific adapter (fill, upload, submit)
    6. Screenshot, update SQLite, send Discord msg
            |
            v
    status='submitted' OR 'needs_human' (captcha/login/fallback)

Safety invariants (see :mod:`src.apply.ats_adapters.base`):

  - Generic fallback NEVER auto-submits — always pauses for human review.
  - Login walls / captchas -> needs_human, Discord alert, no auto-solve.
  - Idempotent: re-running skips already-submitted jobs.
  - Rate limited: max 5 applies per run, 30s delay default.
  - Audit trail: every apply writes JSONL log under ``auto/logs/<date>.jsonl``.
"""

from __future__ import annotations

__all__ = ["runner", "ats_adapters"]
