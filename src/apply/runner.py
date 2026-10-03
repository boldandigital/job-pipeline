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
import shutil
import sqlite3
import sys
import time
import urllib.request
import urllib.error
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
# Discord notifications (ADOPT-12d)
# ---------------------------------------------------------------------------
#
# Mirrors the lars-daily-run.sh discord_send() logic but as Python functions.
# Reads Discord config from environment variables (same .env pattern).


def _discord_config() -> Dict[str, Optional[str]]:
    """Return Discord config from env (bot mode preferred, webhook fallback).

    Accept either DISCORD_CHANNEL_ID (canonical, used by all job-pipeline
    scripts) or DISCORD_HOME_CHANNEL (Hermes bot convention).
    """
    bot_token = os.getenv("DISCORD_BOT_TOKEN")
    channel_id = os.getenv("DISCORD_CHANNEL_ID") or os.getenv("DISCORD_HOME_CHANNEL")
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    username = os.getenv("DISCORD_USERNAME")
    if bot_token and channel_id:
        return {
            "mode": "bot",
            "token": bot_token,
            "channel": channel_id,
            "username": username,
        }
    if webhook_url:
        return {"mode": "webhook", "webhook_url": webhook_url, "username": username}
    return {"mode": "none", "webhook_url": None, "token": None, "channel": None, "username": None}


def _discord_post(url: str, data: bytes, headers: Dict[str, str]) -> int:
    """POST to Discord, return HTTP status."""
    req = urllib.request.Request(url, data=data, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    # Discord/Cloudflare require a proper User-Agent
    if "User-Agent" not in req.headers:
        req.add_header("User-Agent", "DiscordBot (job-pipeline, 1.0)")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace") if e.fp else ""
        log.warning("Discord HTTP %d: %s", e.code, body[:200])
        raise


def discord_send(msg: str, attachment: Optional[str] = None) -> bool:
    """
    Send a message to Discord. Returns True on success, False if disabled/failed.

    Bot mode: multipart if attachment given, else JSON.
    Webhook mode: JSON only (no file upload support in this shim).

    Implementation note: urllib.request from this host hits Discord 1010
    (message content intent denied) for the Hermes bot. curl works, so
    we shell out to curl here. Fall back to urllib only if curl is missing.
    """
    cfg = _discord_config()
    if cfg["mode"] == "none":
        log.debug("Discord not configured — skipping notification")
        return False

    if cfg["mode"] == "bot":
        token = cfg["token"]
        channel = cfg["channel"]
        username = cfg.get("username")
        url = f"https://discord.com/api/v10/channels/{channel}/messages"

        # Build payload
        payload = {"content": msg}
        if username:
            payload["username"] = username
        body_json = json.dumps(payload)

        try:
            import subprocess
            cmd = [
                "curl", "-s", "-X", "POST", url,
                "-H", f"Authorization: Bot {token}",
                "-H", "Content-Type: application/json",
                "-d", body_json,
                "-w", "\n%{http_code}",
            ]
            if attachment:
                # Multipart upload
                cmd = [
                    "curl", "-s", "-X", "POST", url,
                    "-H", f"Authorization: Bot {token}",
                    "-F", f"payload_json={body_json}",
                    "-F", f"files[0]=@{attachment}",
                    "-w", "\n%{http_code}",
                ]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            status = int(r.stdout.strip().split("\n")[-1]) if r.stdout.strip() else 0
            log.info("Discord bot status: %d", status)
            return status in (200, 201, 204)
        except Exception as exc:  # noqa: BLE001
            log.warning("discord send failed: %s", exc)
            return False

    # webhook mode
    webhook = cfg["webhook_url"]
    if not webhook:
        return False
    payload = {"content": msg}
    if cfg.get("username"):
        payload["username"] = cfg["username"]
    status = _discord_post(webhook, json.dumps(payload).encode(), {"Content-Type": "application/json"})
    log.info("Discord webhook status: %d", status)
    return status == 200 or status == 201 or status == 204


def _discord_notify_apply_result(job: Mapping[str, Any], result: base_mod.ApplyResult, screenshot: str) -> None:
    """Send per-apply Discord notification."""
    if base_mod.is_generic_fallback_result(result):
        msg = f"⚠️ Needs human: {job.get('title')} @ {job.get('company')} via {result.ats_type or 'generic'} — {result.error or 'generic fallback paused for review'}"
    elif result.success:
        msg = f"✅ Applied: {job.get('title')} @ {job.get('company')} via {result.ats_type}"
    else:
        msg = f"❌ Failed: {job.get('title')} @ {job.get('company')} — {result.error}"
    discord_send(msg)


def _discord_notify_summary(summary: Dict[str, Any]) -> None:
    """Send end-of-run summary to Discord."""
    applied = summary.get("applied", 0)
    needs_human = summary.get("needs_human", 0)
    failed = summary.get("failed", 0)
    total = applied + needs_human + failed
    if total == 0:
        return
    msg = f"📊 Apply run complete: {total} job(s) — {applied} ✅, {needs_human} ⚠️, {failed} ❌"
    discord_send(msg)


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

    # ADOPT-MAIL-3: Also copy the headshot as a separate file (many ATS portals
    # strip embedded photos from PDFs but accept profile-picture uploads).
    # Photo source priority: config/photo.jpg > PHOTO_PATH env > skip.
    try:
        photo_src = Path(os.getenv("PHOTO_PATH") or _PROJECT_ROOT / "config" / "photo.jpg")
        if photo_src.exists():
            photo_dst = job_dir / "photo.jpg"
            shutil.copyfile(photo_src, photo_dst)
            log.info("copied photo to %s", photo_dst)
    except Exception as exc:  # noqa: BLE001
        log.warning("photo copy failed (non-fatal): %s", exc)

    # Generate MANIFEST.md with submission instructions for ATS adapters.
    try:
        job_id = job.get("id", "?")
        title = job.get("title", "?")
        company = job.get("company", "?")
        manifest = (
            f"Submission Manifest — Job {job_id}: {title} @ {company}\n"
            f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')} UTC\n\n"
            "ATS upload order:\n"
            "  1. photo.jpg — profile picture (upload FIRST)\n"
            "  2. cv.html + anschreiben.html — paste content into form fields\n"
            "     OR convert to PDF for file upload\n"
            "  3. Watch for captcha / SSO walls → adapter pauses for review\n\n"
            f"Job URL: {job.get('url', '?')}\n"
            f"ATS type: {job.get('ats_type') or 'generic'}\n"
        )
        (job_dir / "MANIFEST.md").write_text(manifest, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.warning("manifest write failed (non-fatal): %s", exc)
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

        # Open the career page BEFORE the adapter sees the driver — the
        # adapter reads driver.page synchronously in many adapters.
        career_url = job.get("career_url") or job.get("url") or ""
        if career_url:
            try:
                await driver.open(career_url)
            except Exception as exc:
                log.warning("driver.open(%s) failed: %s", career_url, exc)

        result = await _apply_one(job, profile, cv_path, cover_path, driver)

        # Apply side effects: status + audit + Discord.
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
            _discord_notify_apply_result(job, result, result.screenshot_path)
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
            _discord_notify_apply_result(job, result, result.screenshot_path)
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
            _discord_notify_apply_result(job, result, result.screenshot_path)
            summary["failed"] += 1
            summary["jobs"].append({"id": job["id"], "result": "failed"})

    conn.close()
    _discord_notify_summary(summary)
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
    parser.add_argument("--with-cua", action="store_true",
                        help="Wire the local Playwright driver (NOT cua-driver MCP) "
                             "and PAUSE before each submit. Per memory rule: explicit 'go' per apply.")
    parser.add_argument("--headless", action="store_true",
                        help="(with --with-cua) Run browser in headless mode. "
                             "Default: visible browser window so you can review before submit.")
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

    driver_factory = None
    if args.with_cua:
        from src.apply.playwright_driver import PlaywrightDriverFactory

        async def _run_with_driver():
            async with PlaywrightDriverFactory(headless=args.headless) as factory:
                return await run_apply_pipeline(
                    db_path=args.db,
                    batches_dir=args.batches_dir,
                    logs_dir=args.logs_dir,
                    screenshots_dir=args.screenshots_dir,
                    job_id=args.job_id,
                    limit=args.limit,
                    delay_seconds=args.delay,
                    driver_factory=factory,
                )
        summary = asyncio.run(_run_with_driver())
    else:
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
