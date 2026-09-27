"""Apply runner — orchestrates the CUA submission pipeline.

Wires the SQLite approved-jobs queue (set by ADOPT-11's sheet sync) into
the ATS adapter registry:

  - For each approved job:
    1. Load job + render CV + Anschreiben (via ats_templates.py).
    2. Pick the right adapter by ``ats_type`` (falls back to Generic).
    3. Drive the adapter against a real cua-driver instance.
    4. Update SQLite ``status`` to ``submitted`` or ``needs_human``.
    5. Append an audit entry to ``auto/logs/<date>.jsonl``.

Safety rails:
  - Idempotent: jobs already at status ``submitted`` or ``needs_human``
    are skipped on re-run.
  - Rate limit: ``--max`` caps the number of applies per invocation.
  - Pause-only generic: the runner refuses to call submit on results that
    carry ``paused_for_review`` or ``is_generic_fallback``.

CLI: see ``bin/apply-cua.sh`` (the thin shell wrapper around
:func:`run_apply_pipeline`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from src.apply.ats_adapters import base as base_mod
from src.apply.ats_adapters import GenericAdapter

log = logging.getLogger("apply.runner")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent.parent  # src/apply -> job-pipeline/

DEFAULT_DB_PATH = os.getenv("DB_PATH") or str(_PROJECT_ROOT / "data" / "jobs.db")
DEFAULT_BATCHES_DIR = os.getenv("BATCHES_DIR") or str(_PROJECT_ROOT / "data" / "batches")
DEFAULT_LOGS_DIR = os.getenv("APPLY_LOGS_DIR") or str(_PROJECT_ROOT / "logs" / "apply")
DEFAULT_SCREENSHOTS_DIR = os.getenv("SCREENSHOTS_DIR") or str(_PROJECT_ROOT / "screenshots")

STATUS_SUBMITTED = "submitted"
STATUS_NEEDS_HUMAN = "needs_human"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_approved_jobs(
    conn: sqlite3.Connection,
    job_id: Optional[int] = None,
    limit: Optional[int] = None,
) -> List[Mapping[str, Any]]:
    """Return approved jobs ordered by score DESC.

    Excludes jobs already at ``submitted`` or ``needs_human`` (idempotency).
    """
    where = "status = 'approved'"
    if job_id is not None:
        where = f"id = {int(job_id)} AND " + where
    sql = f"""
        SELECT id, title, company, location, url, career_url, ats_type,
               score, source, cv_path, cover_letter_path
          FROM jobs
         WHERE {where}
         ORDER BY score DESC, id ASC
    """
    rows = conn.execute(sql).fetchall()
    if limit:
        rows = rows[:limit]
    return [dict(r) for r in rows]


def update_job_status(
    conn: sqlite3.Connection,
    job_id: int,
    status: str,
    applied_at: Optional[str] = None,
) -> None:
    """Set status + applied_at for a job.

    ``applied_at`` is recorded on successful submission; on needs_human we
    leave it null so a future run can re-attempt after the human intervenes.
    """
    if applied_at:
        conn.execute(
            "UPDATE jobs SET status = ?, applied_at = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (status, applied_at, job_id),
        )
    else:
        conn.execute(
            "UPDATE jobs SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, job_id),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def _audit_log_path(logs_dir: str) -> Path:
    p = Path(logs_dir)
    p.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return p / f"{today}.jsonl"


def append_audit(logs_dir: str, entry: Dict[str, Any]) -> None:
    """Append a single audit entry. Auto-adds ``ts`` if missing."""
    path = _audit_log_path(logs_dir)
    if "ts" not in entry:
        entry = {**entry, "ts": _now_iso()}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


# ---------------------------------------------------------------------------
# Adapter dispatch
# ---------------------------------------------------------------------------


def dispatch_adapter(ats_type: Optional[str]):
    """Pick the adapter class for ``ats_type``, falling back to Generic.

    Returns ``(adapter_cls, is_generic_fallback)`` so the caller can record
    which path was taken.
    """
    cls = base_mod.resolve_adapter(ats_type)
    if cls is None:
        return GenericAdapter, True
    # GenericAdapter is the explicit fallback — every other registered
    # adapter is "known".
    return cls, cls is GenericAdapter


# ---------------------------------------------------------------------------
# CV / cover letter rendering (delegates to ats_templates)
# ---------------------------------------------------------------------------


def _render_documents(
    job: Mapping[str, Any],
    profile: base_mod.Profile,
    batches_dir: str,
) -> tuple[str, str]:
    """Render the CV + Anschreiben for a job via the ADOPT-3 template loader.

    Returns ``(cv_path, cover_letter_path)``. Both are absolute paths under
    ``batches_dir/<date>/<job_id>/...``.

    Best-effort: if ats_templates isn't importable, or the templates /
    why-you YAML are missing, both paths come back empty. The runner still
    proceeds — adapters don't care if cv_path is empty (they pause for
    review in that case).
    """
    try:
        from src.generation import ats_templates  # type: ignore
    except ImportError:
        log.warning("ats_templates not importable — skipping render")
        return "", ""

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    job_dir = Path(batches_dir) / f"apply-{today}" / f"job-{job.get('id')}"
    job_dir.mkdir(parents=True, exist_ok=True)

    variant = "founder_cto"
    language = (job.get("language") or "en").lower()

    # Build a minimal personal dict from the Profile.
    personal = {
        "first_name": profile.first_name,
        "last_name": profile.last_name,
        "name": profile.display_name(),
        "email": profile.email,
        "phone": profile.phone,
        "location": profile.location,
        "linkedin": profile.linkedin,
        "portfolio": profile.portfolio,
        "summary": profile.summary,
    }

    cv_path = ""
    cover_path = ""
    try:
        cv_html = ats_templates.render_cv_classic(ctx=personal)  # type: ignore[arg-type]
        cv_path = str(job_dir / "cv.html")
        Path(cv_path).write_text(cv_html, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.warning("cv render failed: %s", exc)
    try:
        cover_html = ats_templates.build_cover_letter_for_job(  # type: ignore[arg-type]
            job=dict(job),
            language=language,
            variant=variant,
            personal=personal,
            opening="",  # ADOPT-3 batch pipeline supplies these; apply-cua
            me_paragraph="",  # leaves them empty rather than re-running the
            close="",  # LLM that produces them in a separate (offline) step.
            salutation="",
            recipient_block="",
        )
        cover_path = str(job_dir / "anschreiben.html")
        Path(cover_path).write_text(cover_html, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.warning("cover letter render failed: %s", exc)

    return cv_path, cover_path


# ---------------------------------------------------------------------------
# Per-job apply
# ---------------------------------------------------------------------------


async def _apply_one(
    job: Mapping[str, Any],
    profile: base_mod.Profile,
    cv_path: str,
    cover_letter_path: str,
    driver: base_mod.Driver,
) -> base_mod.ApplyResult:
    adapter_cls, is_generic = dispatch_adapter(job.get("ats_type"))
    adapter = adapter_cls()
    log.info("dispatching %s (job %s) via %s%s",
             job.get("title"), job.get("id"),
             adapter.ats_name,
             " [generic fallback]" if is_generic else "")
    return await adapter.apply(job, profile, cv_path, cover_letter_path, driver)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


async def run_apply_pipeline(
    db_path: str = DEFAULT_DB_PATH,
    batches_dir: str = DEFAULT_BATCHES_DIR,
    logs_dir: str = DEFAULT_LOGS_DIR,
    screenshots_dir: str = DEFAULT_SCREENSHOTS_DIR,
    job_id: Optional[int] = None,
    limit: Optional[int] = None,
    delay_seconds: float = 0.0,
    profile: Optional[base_mod.Profile] = None,
    driver_factory=None,
) -> Dict[str, Any]:
    """Run the apply pipeline.

    Args:
        db_path: SQLite path (default ``./data/jobs.db``).
        batches_dir: where rendered CV + cover letter land.
        logs_dir: where ``<date>.jsonl`` audit entries go.
        screenshots_dir: where per-apply screenshots go.
        job_id: optional single-job mode.
        limit: cap on jobs processed in this run.
        delay_seconds: pause between jobs (rate limit / politeness).
        profile: optional profile override (test hook).
        driver_factory: optional callable returning a fresh Driver per job
            (defaults to ``None`` for tests that inject their own).

    Returns:
        A summary dict: ``{applied, needs_human, failed, jobs: [...]}``.
    """
    if profile is None:
        profile = base_mod.parse_master_profile()

    Path(screenshots_dir).mkdir(parents=True, exist_ok=True)
    conn = _connect(db_path)
    jobs = fetch_approved_jobs(conn, job_id=job_id, limit=limit)

    summary: Dict[str, Any] = {
        "applied": 0,
        "needs_human": 0,
        "failed": 0,
        "jobs": [],
    }

    if not jobs:
        log.info("no approved jobs to apply — exit 0")
        return summary

    for i, job in enumerate(jobs):
        if delay_seconds and i > 0:
            await asyncio.sleep(delay_seconds)

        cv_path, cover_path = _render_documents(job, profile, batches_dir)
        driver = driver_factory() if driver_factory else None
        if driver is None:
            # No driver wired up — record as needs_human so the user can
            # run with the real cua-driver.
            update_job_status(conn, job["id"], STATUS_NEEDS_HUMAN)
            append_audit(logs_dir, {
                "ts": _now_iso(),
                "job_id": job["id"],
                "ats_type": job.get("ats_type"),
                "result": "no_driver",
                "error": "no driver_factory configured",
            })
            summary["needs_human"] += 1
            summary["jobs"].append({"id": job["id"], "result": "no_driver"})
            continue

        result = await _apply_one(job, profile, cv_path, cover_path, driver)

        # Apply side effects: status + audit.
        if base_mod.is_generic_fallback_result(result):
            update_job_status(conn, job["id"], STATUS_NEEDS_HUMAN)
            append_audit(logs_dir, {
                "ts": _now_iso(),
                "job_id": job["id"],
                "ats_type": job.get("ats_type"),
                "result": "needs_human",
                "adapter": result.ats_type,
                "error": result.error,
                "screenshot": result.screenshot_path,
                "fields_filled": result.fields_filled,
            })
            summary["needs_human"] += 1
            summary["jobs"].append({"id": job["id"], "result": "needs_human"})
        elif result.success:
            update_job_status(
                conn, job["id"], STATUS_SUBMITTED, applied_at=_now_iso(),
            )
            append_audit(logs_dir, {
                "ts": _now_iso(),
                "job_id": job["id"],
                "ats_type": job.get("ats_type"),
                "result": "submitted",
                "adapter": result.ats_type,
                "screenshot": result.screenshot_path,
                "fields_filled": result.fields_filled,
            })
            summary["applied"] += 1
            summary["jobs"].append({"id": job["id"], "result": "submitted"})
        else:
            update_job_status(conn, job["id"], STATUS_NEEDS_HUMAN)
            append_audit(logs_dir, {
                "ts": _now_iso(),
                "job_id": job["id"],
                "ats_type": job.get("ats_type"),
                "result": "failed",
                "adapter": result.ats_type,
                "error": result.error,
                "screenshot": result.screenshot_path,
                "fields_filled": result.fields_filled,
            })
            summary["failed"] += 1
            summary["jobs"].append({"id": job["id"], "result": "failed"})

    conn.close()
    return summary


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# CLI (used by bin/apply-cua.sh)
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="apply-cua",
        description="Run the ADOPT-12 CUA submission pipeline against approved jobs.",
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    parser.add_argument("--batches-dir", default=DEFAULT_BATCHES_DIR)
    parser.add_argument("--logs-dir", default=DEFAULT_LOGS_DIR)
    parser.add_argument("--screenshots-dir", default=DEFAULT_SCREENSHOTS_DIR)
    parser.add_argument("--job-id", type=int, default=None,
                        help="Apply to a single job id (debug)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap on jobs processed in this run")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="Seconds to wait between applies (rate limit)")
    parser.add_argument("--dry-run", action="store_true",
                        help="List approved jobs without submitting")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.dry_run:
        conn = _connect(args.db)
        rows = fetch_approved_jobs(conn, job_id=args.job_id, limit=args.limit)
        conn.close()
        print(json.dumps({"approved": len(rows), "jobs": rows},
                         indent=2, ensure_ascii=False, default=str))
        return 0

    summary = asyncio.run(run_apply_pipeline(
        db_path=args.db,
        batches_dir=args.batches_dir,
        logs_dir=args.logs_dir,
        screenshots_dir=args.screenshots_dir,
        job_id=args.job_id,
        limit=args.limit,
        delay_seconds=args.delay,
    ))
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    # Cron-friendly exit code: 0 always (apply-cua never fails the cron
    # when at least one job was attempted; per-job outcomes are in JSON).
    return 0


if __name__ == "__main__":
    sys.exit(main())
