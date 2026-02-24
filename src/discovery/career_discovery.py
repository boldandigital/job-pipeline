#!/usr/bin/env python3
"""
Career page discovery -- finds direct career page URLs for jobs.

Three-layer strategy (no search engine dependency):
  1. JobSpy DB cross-reference -- uses job_url_direct from scraper data
  2. StepStone redirect -- extracts external apply link from StepStone pages
  3. Direct website probing -- guesses company domain, checks /careers /jobs /karriere

Usage:
    python -m src.discovery.career_discovery --db ./data/jobs.db --max 50
    python -m src.discovery.career_discovery --db ./data/jobs.db --max 5 --dry-run
"""

import argparse
import json
import logging
import os
import random
import re
import sqlite3
import sys
import time
import urllib.parse
from typing import Optional

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("career_discovery")

DB_PATH = os.getenv("DB_PATH", "./data/jobs.db")
JOBSPY_DB = os.getenv("JOBSPY_DB_PATH", "./data/jobspy.db")

PORTAL_DOMAINS = {
    "indeed.com", "indeed.de", "de.indeed.com",
    "linkedin.com", "www.linkedin.com",
    "stepstone.de", "www.stepstone.de",
    "glassdoor.de", "glassdoor.com", "www.glassdoor.de", "www.glassdoor.com",
    "xing.com", "www.xing.com",
    "monster.de", "www.monster.de",
    "kimeta.de", "www.kimeta.de",
    "stellenanzeigen.de", "www.stellenanzeigen.de",
    "jobware.de", "www.jobware.de",
    "absolventa.de", "www.absolventa.de",
    "equest.com", "ars2.equest.com",
    "gohiring.com", "t.gohiring.com",
}

ATS_PATTERNS = [
    (r"softgarden\.io|softgarden\.de", "softgarden"),
    (r"myworkday\.com|workday\.com", "workday"),
    (r"smartrecruiters\.com", "smartrecruiters"),
    (r"join\.com", "join"),
    (r"ashbyhq\.com", "ashby"),
    (r"greenhouse\.io", "greenhouse"),
    (r"coveto\.de", "coveto"),
    (r"umantis\.com", "umantis"),
    (r"workable\.com", "workable"),
    (r"lever\.co", "lever"),
    (r"recruitee\.com", "recruitee"),
    (r"personio\.de|personio\.com", "personio"),
    (r"successfactors\.com|successfactors\.eu", "sap_sf"),
    (r"taleo\.net", "taleo"),
    (r"icims\.com", "icims"),
    (r"breezy\.hr", "breezy"),
    (r"bamboohr\.com", "bamboohr"),
    (r"jobvite\.com", "jobvite"),
]

CAREER_PATHS = [
    "/karriere", "/careers", "/jobs", "/career",
    "/stellenangebote", "/en/careers", "/de/karriere",
    "/work-with-us", "/join-us", "/vacancies",
]


def detect_ats(url: str) -> Optional[str]:
    """Detect ATS type from URL."""
    if not url:
        return None
    for pattern, ats_name in ATS_PATTERNS:
        if re.search(pattern, url, re.IGNORECASE):
            return ats_name
    return None


def is_portal_url(url: str) -> bool:
    """Check if URL is a job portal (not a direct career page)."""
    if not url:
        return True
    try:
        domain = urllib.parse.urlparse(url).netloc.lower()
        return any(d in domain for d in PORTAL_DOMAINS)
    except Exception:
        return True


def layer1_jobspy_xref(conn, jobspy_db):
    """Layer 1: Cross-reference with JobSpy DB for job_url_direct."""
    if not os.path.exists(jobspy_db):
        log.info("JobSpy DB not found, skipping Layer 1")
        return 0

    jobspy = sqlite3.connect(jobspy_db)
    jobspy.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT id, title, company FROM jobs WHERE career_url IS NULL OR career_url = ''"
    ).fetchall()

    updated = 0
    for row in rows:
        match = jobspy.execute(
            "SELECT job_url_direct, company_url FROM jobs WHERE title = ? AND company_name = ?",
            (row[1], row[2]),
        ).fetchone()

        if match:
            direct_url = match["job_url_direct"] or match["company_url"] or ""
            if direct_url and not is_portal_url(direct_url):
                ats = detect_ats(direct_url)
                conn.execute(
                    "UPDATE jobs SET career_url = ?, ats_type = ? WHERE id = ?",
                    (direct_url, ats, row[0]),
                )
                updated += 1

    conn.commit()
    jobspy.close()
    log.info("Layer 1 (JobSpy xref): updated %d career URLs", updated)
    return updated


def layer2_stepstone_redirect(conn, max_jobs=50):
    """Layer 2: Follow StepStone apply redirects to find career pages."""
    rows = conn.execute(
        "SELECT id, url FROM jobs WHERE (career_url IS NULL OR career_url = '') AND source = 'stepstone' AND url IS NOT NULL LIMIT ?",
        (max_jobs,),
    ).fetchall()

    updated = 0
    for row in rows:
        try:
            resp = requests.get(row[1], allow_redirects=True, timeout=15,
                                headers={"User-Agent": "Mozilla/5.0"})
            final_url = resp.url
            if not is_portal_url(final_url):
                ats = detect_ats(final_url)
                conn.execute(
                    "UPDATE jobs SET career_url = ?, ats_type = ? WHERE id = ?",
                    (final_url, ats, row[0]),
                )
                updated += 1
            time.sleep(random.uniform(1.0, 3.0))
        except Exception as e:
            log.debug("StepStone redirect failed for job %d: %s", row[0], e)

    conn.commit()
    log.info("Layer 2 (StepStone redirect): updated %d career URLs", updated)
    return updated


def layer3_website_probe(conn, max_jobs=50):
    """Layer 3: Probe company websites for career pages."""
    rows = conn.execute(
        "SELECT id, company FROM jobs WHERE (career_url IS NULL OR career_url = '') AND company IS NOT NULL LIMIT ?",
        (max_jobs,),
    ).fetchall()

    updated = 0
    for row in rows:
        company = row[1].lower().strip()
        # Generate possible domain from company name
        slug = re.sub(r"[^a-z0-9]", "", company.replace(" ", ""))
        domains = [
            f"https://www.{slug}.de",
            f"https://www.{slug}.com",
            f"https://{slug}.de",
            f"https://{slug}.com",
        ]

        for domain in domains:
            for path in CAREER_PATHS:
                url = domain + path
                try:
                    resp = requests.head(url, allow_redirects=True, timeout=8,
                                         headers={"User-Agent": "Mozilla/5.0"})
                    if resp.status_code < 400:
                        ats = detect_ats(resp.url)
                        conn.execute(
                            "UPDATE jobs SET career_url = ?, ats_type = ? WHERE id = ?",
                            (resp.url, ats, row[0]),
                        )
                        updated += 1
                        break
                except Exception:
                    continue
            else:
                continue
            break

        time.sleep(random.uniform(0.5, 1.5))

    conn.commit()
    log.info("Layer 3 (website probe): updated %d career URLs", updated)
    return updated


def main():
    parser = argparse.ArgumentParser(description="Career page URL discovery")
    parser.add_argument("--db", default=DB_PATH, help="Path to main database")
    parser.add_argument("--jobspy-db", default=JOBSPY_DB, help="Path to JobSpy database")
    parser.add_argument("--max", type=int, default=50, help="Max jobs to process per layer")
    parser.add_argument("--dry-run", action="store_true", help="Print results without saving")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    total = 0
    total += layer1_jobspy_xref(conn, args.jobspy_db)
    total += layer2_stepstone_redirect(conn, args.max)
    total += layer3_website_probe(conn, args.max)

    log.info("Total career URLs discovered: %d", total)

    # Show stats
    stats = conn.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN career_url IS NOT NULL AND career_url != '' THEN 1 ELSE 0 END) as with_career_url,
            SUM(CASE WHEN ats_type IS NOT NULL AND ats_type != '' THEN 1 ELSE 0 END) as with_ats
        FROM jobs
    """).fetchone()

    log.info("DB stats: %d total jobs, %d with career URL, %d with ATS detected",
             stats[0], stats[1], stats[2])

    conn.close()


if __name__ == "__main__":
    main()
