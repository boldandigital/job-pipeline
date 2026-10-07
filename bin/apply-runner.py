#!/usr/bin/env python3
"""
bin/apply-runner.py — end-to-end CLI for the apply orchestrator.

Thin wrapper around :class:`src.apply.orchestrator.ApplyOrchestrator`.
This is what the daily cron calls, and what the dashboard's "Apply now"
button fires for a single approved card.

Routing (see src/apply/orchestrator.py for the verified vendor contracts):

    greenhouse / lever / ashby   -> API POST   (needs an employer-issued key;
                                    falls back to the browser channel without it)
    xing / linkedin / indeed     -> browser    (fills, then PAUSES)
    workday                      -> browser    (fills step 1, then PAUSES)
    anything else                -> refused

The browser channels never click Submit — Lars reviews the filled form and
submits by hand. Only jobs with status='approved' are ever applied to.

Usage::

    # One job
    bin/apply-runner.py --job-id 79
    bin/apply-runner.py --job-id 79 --dry-run

    # Whole approved queue (subject to the daily cap)
    bin/apply-runner.py --all-pending
    bin/apply-runner.py --all-pending --dry-run
    bin/apply-runner.py --all-pending --cap 5

    # Who am I applying as?
    bin/apply-runner.py --all-pending --user lars --db /path/to/jobs.db

Exit codes:
  0 — something was attempted, or --dry-run
  1 — internal error (missing DB, bad args, unhandled exception)
  2 — nothing matched (no approved jobs, or the job was refused/skipped)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# Make `src.*` importable when the script is run directly (not via -m).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.apply.orchestrator import (  # noqa: E402
    DEFAULT_DAILY_CAP,
    DEFAULT_DB_PATH,
    DEFAULT_USER_ID,
    ApplyOrchestrator,
    build_orchestrator,
)

log = logging.getLogger("apply.runner.cli")

#: Statuses that mean "this run did real work".
ACTIONED = {"applied", "paused", "failed"}


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="apply-runner",
        description=(
            "Apply to approved jobs. Greenhouse/Lever/Ashby go to their apply "
            "APIs when an employer-issued key is configured; XING/LinkedIn/"
            "Indeed/Workday are filled in a real browser and then pause so you "
            "click Submit yourself."
        ),
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--job-id", type=int,
                      help="Apply to a single job id (debug / dashboard 'Apply now').")
    mode.add_argument("--all-pending", action="store_true",
                      help="Apply to every status='approved' job with applied_at IS NULL.")

    p.add_argument("--user", default=DEFAULT_USER_ID,
                   help="user_id to apply as (drives credential lookup + rate limit).")
    p.add_argument("--db", default=os.getenv("DB_PATH") or DEFAULT_DB_PATH,
                   help=f"Path to the SQLite jobs DB (default: {DEFAULT_DB_PATH}).")
    p.add_argument("--rate-limit-file", default=os.getenv("DAILY_APPLY_LIMIT_PATH"),
                   help="Override the daily-apply counter JSON path.")
    p.add_argument("--cap", type=int, default=DEFAULT_DAILY_CAP,
                   help=f"Max applies per user per day (default: {DEFAULT_DAILY_CAP}).")
    p.add_argument("--webhook", default=os.getenv("DISCORD_WEBHOOK_URL", ""),
                   help="Discord webhook for per-apply notifications.")
    p.add_argument("--headless", action="store_true",
                   help="(browser channel) run Chromium headless. Default is a visible "
                        "window so you can review the filled form before submitting.")
    p.add_argument("--dry-run", action="store_true",
                   help="Preview the routing without any adapter call or DB write.")
    p.add_argument("--json", action="store_true",
                   help="Emit the JSON result on stdout (default when not a TTY).")
    p.add_argument("--quiet", action="store_true", help="Suppress the JSON payload.")
    p.add_argument("--verbose", "-v", action="store_true", help="Verbose logging.")
    return p


def _human_summary(payload: dict) -> str:
    if payload.get("status") == "dry_run":
        return (f"[dry-run] job={payload.get('job_id')} "
                f"channel={payload.get('channel')} platform={payload.get('platform')} "
                f"status={payload.get('job_status')} "
                f"cap={payload.get('applied_today')}/{payload.get('cap')}")
    if payload.get("status") == "applied":
        return f"✅ applied job={payload.get('job_id')} via {payload.get('channel')}"
    if payload.get("status") == "paused":
        return (f"⚠️ paused job={payload.get('job_id')} via {payload.get('platform')} — "
                f"{payload.get('error')}")
    if payload.get("status") == "failed":
        return f"❌ failed job={payload.get('job_id')} — {payload.get('error')}"
    return (f"refused job={payload.get('job_id')} — "
            f"{payload.get('error')} ({payload.get('channel')})")


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # A missing DB is a hard error for a real run; --dry-run still needs one
    # to preview against, so it is treated the same way (fail loudly).
    db_path = args.db
    if not Path(db_path).exists():
        log.error("DB not found at %s — pass --db or set DB_PATH", db_path)
        return 1

    kwargs: dict = {"db_path": db_path, "dry_run": args.dry_run, "headless": args.headless}
    if args.rate_limit_file:
        kwargs["rate_limit_path"] = args.rate_limit_file
    if args.webhook:
        kwargs["discord_webhook"] = args.webhook
    if args.cap:
        kwargs["daily_cap"] = args.cap

    try:
        orch: ApplyOrchestrator = build_orchestrator(args.user, **kwargs)

        if args.job_id is not None:
            payload = {"user_id": args.user, "dry_run": args.dry_run,
                       **orch.apply_job(int(args.job_id))}
        else:
            jobs = [orch.apply_job(jid) for jid in orch._pending_ids()]
            attempted = sum(1 for j in jobs if j.get("status") in ACTIONED)
            payload = {
                "user_id": args.user, "dry_run": args.dry_run,
                "attempted": attempted, "pending": len(jobs),
                "cap": orch.daily_cap, "jobs": jobs,
            }
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        log.exception("apply-runner crashed: %s", exc)
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}),
              file=sys.stderr)
        return 1

    if not args.quiet:
        if args.json or not sys.stdout.isatty():
            print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        elif args.job_id is not None:
            print(_human_summary(payload))
        else:
            print(f"attempted={payload['attempted']}/{payload['pending']} "
                  f"(cap {payload['cap']}/day)")

    if args.dry_run:
        return 0
    if args.job_id is not None:
        return 0 if payload.get("status") in ACTIONED else 2
    return 0 if payload.get("attempted", 0) > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())