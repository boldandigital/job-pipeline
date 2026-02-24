#!/usr/bin/env python3
"""
Import jobs from JobSpy Docker output into the main pipeline database.

JobSpy (https://github.com/Bunsly/JobSpy) runs in Docker and writes to its own SQLite DB.
This script reads from that DB and inserts into the pipeline's main DB, avoiding duplicates.

Usage:
    python -m src.scrapers.import_jobspy
    python -m src.scrapers.import_jobspy --source ./data/jobspy.db --target ./data/jobs.db
"""

import argparse
import logging
import os
import sqlite3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("import_jobspy")

SOURCE_DB = os.getenv("JOBSPY_DB_PATH", "./data/jobspy.db")
TARGET_DB = os.getenv("DB_PATH", "./data/jobs.db")


def main():
    parser = argparse.ArgumentParser(description="Import JobSpy results into main DB")
    parser.add_argument("--source", default=SOURCE_DB, help="Path to JobSpy SQLite DB")
    parser.add_argument("--target", default=TARGET_DB, help="Path to main pipeline DB")
    args = parser.parse_args()

    if not os.path.exists(args.source):
        log.error("Source DB not found: %s", args.source)
        return

    source = sqlite3.connect(args.source)
    source.row_factory = sqlite3.Row
    target = sqlite3.connect(args.target)

    target.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            location TEXT,
            url TEXT,
            career_url TEXT,
            source TEXT,
            description TEXT,
            search_query TEXT,
            score INTEGER DEFAULT 0,
            status TEXT DEFAULT 'new',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(title, company)
        )
    """)

    rows = source.execute("""
        SELECT title, company_name, location, job_url, job_url_direct,
               site, description
        FROM jobs
        WHERE title IS NOT NULL AND company_name IS NOT NULL
    """).fetchall()

    log.info("Found %d jobs in JobSpy DB", len(rows))

    inserted = 0
    skipped = 0

    for row in rows:
        try:
            target.execute(
                """INSERT OR IGNORE INTO jobs
                   (title, company, location, url, career_url, source, description)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["title"],
                    row["company_name"],
                    row["location"] or "",
                    row["job_url"] or "",
                    row["job_url_direct"] or "",
                    row["site"] or "",
                    row["description"] or "",
                ),
            )
            if target.total_changes > inserted + skipped:
                inserted += 1
            else:
                skipped += 1
        except sqlite3.IntegrityError:
            skipped += 1

    target.commit()
    source.close()
    target.close()

    log.info("Imported %d new jobs, %d duplicates skipped", inserted, skipped)


if __name__ == "__main__":
    main()
