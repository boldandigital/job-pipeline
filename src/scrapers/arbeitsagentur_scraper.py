#!/usr/bin/env python3
"""
Arbeitsagentur (German Federal Employment Agency) job scraper.

Uses the public REST API to search for jobs and stores results in SQLite.

Usage:
    python -m src.scrapers.arbeitsagentur_scraper
    python -m src.scrapers.arbeitsagentur_scraper --db ./data/jobs.db --limit 100
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("arbeitsagentur")

DB_PATH = os.getenv("DB_PATH", "./data/jobs.db")

API_BASE = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobs"
OAUTH_URL = "https://rest.arbeitsagentur.de/oauth/gettoken_cc"
CLIENT_ID = "c003a37f-024f-462a-b36d-b001be4cd24a"
CLIENT_SECRET = "32a39620-32b3-4f22-9e3c-f0bec36a0e25"


def get_token():
    """Get OAuth token from Arbeitsagentur API."""
    data = f"client_id={CLIENT_ID}&client_secret={CLIENT_SECRET}&grant_type=client_credentials"
    req = Request(OAUTH_URL, data=data.encode(), method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())["access_token"]
    except Exception as e:
        log.error("Failed to get OAuth token: %s", e)
        return None


def search_jobs(token, query, page=1, size=25):
    """Search jobs via Arbeitsagentur API."""
    url = f"{API_BASE}?was={quote_plus(query)}&page={page}&size={size}&pav=false"
    req = Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("User-Agent", "Mozilla/5.0")

    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        if e.code == 429:
            log.warning("Rate limited, waiting 30s...")
            time.sleep(30)
            return search_jobs(token, query, page, size)
        log.error("API error %d for query '%s': %s", e.code, query, e)
        return None
    except Exception as e:
        log.error("Request failed for query '%s': %s", query, e)
        return None


def save_to_db(jobs, db_path):
    """Save jobs to SQLite database."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            location TEXT,
            url TEXT,
            source TEXT,
            search_query TEXT,
            score INTEGER DEFAULT 0,
            status TEXT DEFAULT 'new',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(title, company)
        )
    """)

    inserted = 0
    skipped = 0

    for job in jobs:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO jobs (title, company, location, url, source, search_query) VALUES (?, ?, ?, ?, ?, ?)",
                (job["title"], job["company"], job.get("location", ""), job.get("url", ""), "arbeitsagentur", job.get("query", "")),
            )
            if conn.total_changes > inserted + skipped:
                inserted += 1
            else:
                skipped += 1
        except sqlite3.IntegrityError:
            skipped += 1

    conn.commit()
    conn.close()
    return inserted, skipped


def main():
    parser = argparse.ArgumentParser(description="Arbeitsagentur job scraper")
    parser.add_argument("--db", default=DB_PATH, help="Path to SQLite database")
    parser.add_argument("--queries", nargs="+", default=["Data Analyst", "AI Specialist", "Business Analyst"],
                        help="Search queries")
    parser.add_argument("--limit", type=int, default=25, help="Results per query")
    args = parser.parse_args()

    token = get_token()
    if not token:
        log.error("Could not authenticate with Arbeitsagentur API")
        sys.exit(1)

    all_jobs = []

    for query in args.queries:
        log.info("Searching: %s", query)
        result = search_jobs(token, query, size=args.limit)
        if not result:
            continue

        stellenangebote = result.get("stellenangebote", [])
        for s in stellenangebote:
            all_jobs.append({
                "title": s.get("titel", ""),
                "company": s.get("arbeitgeber", "Unknown"),
                "location": s.get("arbeitsort", {}).get("ort", ""),
                "url": f"https://www.arbeitsagentur.de/jobsuche/suche?id={s.get('hashId', '')}",
                "query": query,
            })

        log.info("  Found %d jobs for '%s'", len(stellenangebote), query)
        time.sleep(1)

    if all_jobs:
        inserted, skipped = save_to_db(all_jobs, args.db)
        log.info("Total: %d new, %d duplicates (from %d scraped)", inserted, skipped, len(all_jobs))
    else:
        log.warning("No jobs found")


if __name__ == "__main__":
    main()
