#!/usr/bin/env python3
"""
Fetch full job descriptions for jobs that only have title/company/URL.

Uses the JobSpy database as a source for descriptions, falling back to
direct HTTP fetching when needed.

Usage:
    python -m src.scrapers.fetch_descriptions
    python -m src.scrapers.fetch_descriptions --db ./data/jobs.db --limit 50
"""

import argparse
import logging
import os
import re
import sqlite3
import sys
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("fetch_descriptions")

DB_PATH = os.getenv("DB_PATH", "./data/jobs.db")
JOBSPY_DB = os.getenv("JOBSPY_DB_PATH", "./data/jobspy.db")

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36"


def strip_html(html):
    """Remove HTML tags and normalize whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()[:5000]


def fetch_from_jobspy(conn, jobspy_db):
    """Cross-reference main DB with JobSpy DB to get descriptions."""
    if not os.path.exists(jobspy_db):
        log.info("JobSpy DB not found at %s, skipping cross-reference", jobspy_db)
        return 0

    jobspy = sqlite3.connect(jobspy_db)
    jobspy.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT id, title, company FROM jobs WHERE (description IS NULL OR description = '') AND url IS NOT NULL"
    ).fetchall()

    updated = 0
    for row in rows:
        match = jobspy.execute(
            "SELECT description FROM jobs WHERE title = ? AND company_name = ? AND description IS NOT NULL AND description != ''",
            (row[1], row[2]),
        ).fetchone()

        if match:
            conn.execute("UPDATE jobs SET description = ? WHERE id = ?", (match["description"], row[0]))
            updated += 1

    conn.commit()
    jobspy.close()
    log.info("Updated %d descriptions from JobSpy DB", updated)
    return updated


def fetch_from_url(url, timeout=15):
    """Fetch page content from URL and extract text."""
    try:
        req = Request(url)
        req.add_header("User-Agent", USER_AGENT)
        with urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="replace")
            return strip_html(html)
    except Exception as e:
        log.debug("Failed to fetch %s: %s", url, e)
        return None


def main():
    parser = argparse.ArgumentParser(description="Fetch job descriptions")
    parser.add_argument("--db", default=DB_PATH, help="Path to main SQLite database")
    parser.add_argument("--jobspy-db", default=JOBSPY_DB, help="Path to JobSpy database")
    parser.add_argument("--limit", type=int, default=100, help="Max jobs to fetch via HTTP")
    parser.add_argument("--delay", type=float, default=2.0, help="Delay between HTTP requests")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # Phase 1: Cross-reference with JobSpy
    fetch_from_jobspy(conn, args.jobspy_db)

    # Phase 2: Fetch remaining from URLs
    rows = conn.execute(
        "SELECT id, url FROM jobs WHERE (description IS NULL OR description = '') AND url IS NOT NULL LIMIT ?",
        (args.limit,),
    ).fetchall()

    log.info("Fetching descriptions for %d jobs via HTTP...", len(rows))
    fetched = 0

    for row in rows:
        desc = fetch_from_url(row["url"])
        if desc and len(desc) > 100:
            conn.execute("UPDATE jobs SET description = ? WHERE id = ?", (desc, row["id"]))
            fetched += 1
        time.sleep(args.delay)

    conn.commit()
    conn.close()
    log.info("Fetched %d descriptions via HTTP", fetched)


if __name__ == "__main__":
    main()
