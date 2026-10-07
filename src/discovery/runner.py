"""Discover new public jobs from Greenhouse / Lever / Ashby.

Top-level orchestrator: loads a YAML config of company slugs per platform,
fans out to the right :class:`BaseDiscoverySource`, dedupes by URL, and
inserts new rows into the user's jobs DB. Idempotent — re-running for the
same companies inserts nothing new (URL is the natural key).

The runner is deliberately platform-agnostic at the call site: callers
pass a ``companies_yaml_path`` and a ``platform`` filter, and the runner
does the rest. This keeps the CLI thin (see ``bin/discover.py``).

Insert schema mirrors the legacy ``jobs`` table:

    title, company, location, url (UNIQUE-ish via INSERT OR IGNORE),
    source, description, score=0, status='new', created_at=now, updated_at=now

The DB connection is supplied by the per-user factory in
``src/db/jobs_db.py`` (admin user for the CLI today; per-user in Phase 4).
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from ..db.jobs_db import connect_user_db
from .ashby import AshbySource
from .base import BaseDiscoverySource
from .greenhouse import GreenhouseSource
from .lever import LeverSource

log = logging.getLogger("discovery.runner")


# ---------------------------------------------------------------------------
# Source registry — name → class. Adding a fourth platform means one line.
# ---------------------------------------------------------------------------

SOURCE_REGISTRY: Dict[str, type] = {
    "greenhouse": GreenhouseSource,
    "lever": LeverSource,
    "ashby": AshbySource,
}


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_companies_config(path: str | Path) -> Dict[str, List[str]]:
    """Load a YAML mapping of platform name → list of slugs.

    The file shape is::

        greenhouse:
          - stripe
          - verkada
        lever:
          - netflix
        ashby:
          - openai

    Unknown platform names are passed through (the runner ignores them).
    Empty/missing file yields an empty mapping.
    """
    p = Path(path)
    if not p.exists():
        log.warning("companies config not found: %s", p)
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        log.error("companies config YAML parse error: %s", exc)
        return {}
    if not isinstance(data, dict):
        log.error("companies config root must be a mapping, got %s", type(data).__name__)
        return {}
    out: Dict[str, List[str]] = {}
    for platform, slugs in data.items():
        if not isinstance(slugs, list):
            continue
        cleaned = [str(s).strip() for s in slugs if s and str(s).strip()]
        if cleaned:
            out[str(platform)] = cleaned
    return out


# ---------------------------------------------------------------------------
# Per-source run
# ---------------------------------------------------------------------------

def _build_source(platform: str) -> Optional[BaseDiscoverySource]:
    cls = SOURCE_REGISTRY.get(platform)
    if cls is None:
        return None
    return cls()


def run_source(
    platform: str,
    companies: List[str],
    conn: sqlite3.Connection,
) -> Tuple[int, int, int]:
    """Run one platform end-to-end. Returns (fetched, inserted, duplicates).

    ``fetched`` is the raw count returned by the upstream API (including
    already-known postings). ``inserted`` is the count of NEW rows actually
    added to the DB. ``duplicates`` is the count skipped because the URL
    was already in the DB.
    """
    src = _build_source(platform)
    if src is None:
        log.warning("unknown platform: %s — skipping", platform)
        return 0, 0, 0
    if not companies:
        log.info("%s: no companies configured — skipping", platform)
        return 0, 0, 0

    t0 = time.monotonic()
    log.info("%s: fetching %d companies…", platform, len(companies))
    raw_jobs = src.fetch_jobs(companies)
    fetched = len(raw_jobs)
    log.info("%s: %d raw jobs returned", platform, fetched)

    inserted = 0
    duplicates = 0
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Pull the existing URL set once up front. The `url` column has no
    # UNIQUE constraint (the schema was created for the legacy scrapers
    # which dedup on (title, company)), so we have to check manually.
    existing_urls = {
        row[0]
        for row in conn.execute("SELECT url FROM jobs WHERE url IS NOT NULL").fetchall()
    }

    # Per-source batched loop so we know which company each row came from
    # (Lever/Ashby don't tag individual records; Greenhouse does).
    for raw in raw_jobs:
        # Pull the company label the user sees in the DB. Greenhouse tags
        # each record with the board token at fetch time; for Lever/Ashby
        # we resolve it from the URL.
        if raw.get("_board_token"):
            company_label = str(raw["_board_token"])
        elif platform == "lever":
            company_label = _extract_lever_client_from_url(
                str(raw.get("hostedUrl") or "")
            )
        elif platform == "ashby":
            company_label = _extract_ashby_board_from_url(
                str(raw.get("applyUrl") or "")
            )
        else:
            company_label = ""

        parsed = src.parse(raw, company_label)
        url = (parsed.get("url") or "").strip()
        if not url:
            # Without a URL we cannot dedup or apply — skip.
            continue
        if not parsed.get("title"):
            continue
        if url in existing_urls:
            duplicates += 1
            continue

        try:
            conn.execute(
                """INSERT INTO jobs
                       (title, company, location, url, source, description,
                        score, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 0, 'new', ?, ?)""",
                (
                    parsed.get("title", ""),
                    parsed.get("company", ""),
                    parsed.get("location", ""),
                    url,
                    parsed.get("source", platform),
                    parsed.get("description", ""),
                    now_iso,
                    now_iso,
                ),
            )
            inserted += 1
            existing_urls.add(url)  # protect against dup URLs in same batch
        except sqlite3.IntegrityError:
            duplicates += 1

    conn.commit()

    elapsed = time.monotonic() - t0
    log.info(
        "%s: %d fetched, %d new, %d dup'd, took %.1fs",
        platform, fetched, inserted, duplicates, elapsed,
    )
    return fetched, inserted, duplicates


def _extract_lever_client_from_url(url: str) -> str:
    """Extract the Lever client slug from a hostedUrl.

    Example: ``https://jobs.lever.co/netflix/abc-123`` → ``netflix``.
    The first non-empty path segment after the host is the client slug.
    """
    if not url:
        return ""
    from urllib.parse import urlparse
    if urlparse(url).netloc != "jobs.lever.co":
        return ""
    parts = [p for p in urlparse(url).path.split("/") if p]
    if not parts:
        return ""
    return parts[0]


def _extract_ashby_board_from_url(url: str) -> str:
    """Extract the Ashby board slug from an applyUrl.

    Example: ``https://jobs.ashbyhq.com/anthropic/job_456`` → ``anthropic``.
    The first non-empty path segment after the host is the board slug.
    """
    if not url:
        return ""
    from urllib.parse import urlparse
    if urlparse(url).netloc != "jobs.ashbyhq.com":
        return ""
    parts = [p for p in urlparse(url).path.split("/") if p]
    if not parts:
        return ""
    return parts[0]


# ---------------------------------------------------------------------------
# Top-level entry point used by the CLI
# ---------------------------------------------------------------------------

def discover_all(
    companies_yaml_path: str,
    platform: str = "all",
    user_id: str = "lars",
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Tuple[int, int, int]]:
    """Run discovery across the requested platforms.

    Args:
        companies_yaml_path: path to config/companies.yaml
        platform:            "all" or one of greenhouse|lever|ashby
        user_id:             admin user for now (per-user lands in Phase 4)
        conn:                optional pre-opened SQLite connection (used
                             by tests with an in-memory DB). When None,
                             the per-user factory in src/db/jobs_db.py is
                             used.

    Returns:
        Dict mapping platform name → (fetched, inserted, duplicates).
        Returns an empty dict when the platform is unknown or no companies
        are configured.
    """
    config = load_companies_config(companies_yaml_path)
    if not config:
        log.info("no companies configured — nothing to do")
        return {}

    if platform == "all":
        platforms = [p for p in ("greenhouse", "lever", "ashby") if p in config]
    elif platform in config:
        platforms = [platform]
    elif platform in SOURCE_REGISTRY:
        # Platform requested but no companies listed for it — still record
        # a "no companies" entry so the CLI can report 0 cleanly.
        platforms = [platform]
    else:
        log.warning("unknown platform: %s", platform)
        return {}

    own_conn = conn is None
    if own_conn:
        conn = connect_user_db(user_id)

    results: Dict[str, Tuple[int, int, int]] = {}
    try:
        for plat in platforms:
            slugs = config.get(plat, [])
            results[plat] = run_source(plat, slugs, conn)
    finally:
        if own_conn:
            conn.close()

    return results


__all__ = [
    "SOURCE_REGISTRY",
    "discover_all",
    "load_companies_config",
    "run_source",
]
