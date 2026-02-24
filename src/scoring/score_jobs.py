#!/usr/bin/env python3
"""
Keyword-based job scorer.

Scores jobs by matching title and description against weighted keyword categories
defined in a YAML configuration file.

Usage:
    python -m src.scoring.score_jobs
    python -m src.scoring.score_jobs --config config/example.yaml --db ./data/jobs.db
"""

import argparse
import logging
import os
import re
import sqlite3
import sys

import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("score_jobs")

DB_PATH = os.getenv("DB_PATH", "./data/jobs.db")
CONFIG_PATH = os.getenv("CONFIG_PATH", "./config/example.yaml")


def load_config(path):
    with open(path) as f:
        return yaml.safe_load(f)


def score_job(title, description, keywords, weights):
    """Score a single job against keyword categories."""
    text = f"{title or ''} {description or ''}".lower()
    total = 0

    for category, weight in weights.items():
        category_keywords = keywords.get(category, [])
        for kw in category_keywords:
            if re.search(kw if kw.startswith(r'\b') else re.escape(kw), text, re.IGNORECASE):
                total += weight
                break  # only count each category once

    return total


def main():
    parser = argparse.ArgumentParser(description="Keyword-based job scorer")
    parser.add_argument("--db", default=DB_PATH, help="Path to SQLite database")
    parser.add_argument("--config", default=CONFIG_PATH, help="Path to YAML config with scoring rules")
    parser.add_argument("--rescore", action="store_true", help="Re-score all jobs (not just unscored)")
    args = parser.parse_args()

    config = load_config(args.config)
    scoring = config.get("scoring", {})
    weights = scoring.get("weights", {})
    keywords = scoring.get("keywords", {})
    threshold = scoring.get("threshold", 20)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    if args.rescore:
        rows = conn.execute("SELECT id, title, description FROM jobs").fetchall()
    else:
        rows = conn.execute("SELECT id, title, description FROM jobs WHERE score = 0 OR score IS NULL").fetchall()

    log.info("Scoring %d jobs...", len(rows))

    scored = 0
    above_threshold = 0

    for row in rows:
        s = score_job(row["title"], row["description"], keywords, weights)
        conn.execute("UPDATE jobs SET score = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (s, row["id"]))
        scored += 1
        if s >= threshold:
            above_threshold += 1

    conn.commit()
    conn.close()

    log.info("Scored %d jobs. %d above threshold (%d).", scored, above_threshold, threshold)


if __name__ == "__main__":
    main()
