#!/usr/bin/env python3
"""CLI: pull new public jobs from Greenhouse / Lever / Ashby into the DB.

Usage:
    bin/discover.py --platform all
    bin/discover.py --platform greenhouse
    bin/discover.py --platform lever --companies config/companies.yaml

The script writes to data/jobs.db (admin user) by default. Use
``--user-id`` to point at a different per-user DB in the future.

Output is one line per platform in the form:
    greenhouse: 47 jobs found, 12 new, 35 dup'd, took 12.4s

Exit code is 0 on success, 1 on a hard error (bad config path, unknown
platform, etc.). Per-board 404s are not errors — they're just slugs the
company doesn't host on that platform.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

# Make src.* importable when invoked as a script.
_HERE = Path(__file__).resolve()
_PROJECT_ROOT = _HERE.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.discovery.runner import SOURCE_REGISTRY, discover_all  # noqa: E402


DEFAULT_COMPANIES_YAML = _PROJECT_ROOT / "config" / "companies.yaml"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="discover",
        description="Pull new public jobs from Greenhouse/Lever/Ashby into the DB.",
    )
    p.add_argument(
        "--platform",
        choices=("all", "greenhouse", "lever", "ashby"),
        default="all",
        help="Which platform to run. Default: all.",
    )
    p.add_argument(
        "--companies",
        default=str(DEFAULT_COMPANIES_YAML),
        help="Path to companies.yaml. Default: config/companies.yaml",
    )
    p.add_argument(
        "--user-id",
        default="lars",
        help="User whose jobs DB to write to. Default: lars (admin).",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-step logging; print only the summary line(s).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("discover")

    if not Path(args.companies).exists():
        print(f"error: companies config not found: {args.companies}", file=sys.stderr)
        return 1
    if args.platform != "all" and args.platform not in SOURCE_REGISTRY:
        print(f"error: unknown platform: {args.platform}", file=sys.stderr)
        return 1

    t0 = time.monotonic()
    try:
        results = discover_all(
            companies_yaml_path=args.companies,
            platform=args.platform,
            user_id=args.user_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("discover_all failed: %s", exc)
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not results:
        print("no companies configured — nothing to do")
        return 0

    total_new = 0
    for plat, (fetched, inserted, duplicates) in results.items():
        print(
            f"{plat}: {fetched} jobs found, {inserted} new, {duplicates} dup'd"
        )
        total_new += inserted

    wall = time.monotonic() - t0
    print(f"total: {total_new} new jobs across {len(results)} platform(s), took {wall:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
