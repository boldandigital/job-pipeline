#!/bin/bash
# StepStone Scrape Pipeline — browser-based scraping for StepStone + Google Jobs
# Schedule: 08:30 UTC daily via cron
#
# Required env vars: DB_PATH

set -euo pipefail

LOG="${LOG_DIR:-/tmp}/stepstone_pipeline.log"
DB="${DB_PATH:-./data/jobs.db}"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== STEPSTONE PIPELINE START: $(date) ===" >> "$LOG"

BEFORE=$(python3 -c "import sqlite3; print(sqlite3.connect('$DB').execute(\"SELECT COUNT(*) FROM jobs WHERE source='stepstone'\").fetchone()[0])")

cd "$PROJECT_DIR"

# Step 1: StepStone scrape via browser
echo "--- Step 1: StepStone Scrape ---" >> "$LOG"
python3 -m src.scrapers.stepstone_scraper \
  --sites stepstone \
  --location Deutschland \
  --db "$DB" \
  >> "$LOG" 2>&1 || true

# Step 2: Keyword scoring for new entries
echo "--- Step 2: Keyword Score ---" >> "$LOG"
python3 -m src.scoring.score_jobs --db "$DB" >> "$LOG" 2>&1 || true

# Step 3: Fetch descriptions for new StepStone jobs
echo "--- Step 3: Fetch Descriptions ---" >> "$LOG"
python3 -m src.scrapers.fetch_descriptions --db "$DB" --limit 100 >> "$LOG" 2>&1 || true

echo "=== STEPSTONE PIPELINE DONE: $(date) ===" >> "$LOG"

AFTER=$(python3 -c "import sqlite3; print(sqlite3.connect('$DB').execute(\"SELECT COUNT(*) FROM jobs WHERE source='stepstone'\").fetchone()[0])")
NEW=$((AFTER - BEFORE))

echo "StepStone: +${NEW} new jobs" >> "$LOG"
