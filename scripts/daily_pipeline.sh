#!/bin/bash
# Daily Job Pipeline — runs all steps sequentially
# Schedule: 02:00 UTC daily via cron
#
# Required env vars: DB_PATH, DISCORD_WEBHOOK_URL (optional)
#
# NOTE: Lars's wrapper (scripts/lars-daily-run.sh) handles delivery for the
# macOS-local 09:00 Brussels cron. This dome317 stock file is kept aligned for
# whoever runs the upstream pipeline directly (Docker compose path).

set -euo pipefail

LOG="${LOG_DIR:-/tmp}/daily_pipeline.log"
DB="${DB_PATH:-./data/jobs.db}"
WEBHOOK="${DISCORD_WEBHOOK_URL:-}"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== DAILY PIPELINE START: $(date) ===" >> "$LOG"

# Count before
BEFORE=$(python3 -c "import sqlite3; print(sqlite3.connect('$DB').execute('SELECT COUNT(*) FROM jobs').fetchone()[0])")

# Step 1: JobSpy scrape (Indeed + LinkedIn via Docker)
echo "--- Step 1: JobSpy Scrape ---" >> "$LOG"
cd "$PROJECT_DIR"
if [ -f docker-compose.yml ]; then
  timeout 7200 docker compose run --rm jobsearch python main.py >> "$LOG" 2>&1 || true
  docker compose down --remove-orphans >> "$LOG" 2>&1 || true
fi

# Step 2: Import JobSpy results into main DB
echo "--- Step 2: Import JobSpy ---" >> "$LOG"
python3 -m src.scrapers.import_jobspy --target "$DB" >> "$LOG" 2>&1 || true

# Step 3: Arbeitsagentur scrape
echo "--- Step 3: Arbeitsagentur ---" >> "$LOG"
python3 -m src.scrapers.arbeitsagentur_scraper --db "$DB" >> "$LOG" 2>&1 || true

# Step 4: Keyword scoring
echo "--- Step 4: Keyword Score ---" >> "$LOG"
python3 -m src.scoring.score_jobs --db "$DB" >> "$LOG" 2>&1 || true

echo "=== DAILY PIPELINE DONE: $(date) ===" >> "$LOG"

# Count after + stats
AFTER=$(python3 -c "import sqlite3; print(sqlite3.connect('$DB').execute('SELECT COUNT(*) FROM jobs').fetchone()[0])")
NEW=$((AFTER - BEFORE))
STATS=$(python3 -c "
import sqlite3
c = sqlite3.connect('$DB')
p = c.execute('SELECT COUNT(*) FROM jobs WHERE score >= 150').fetchone()[0]
s = c.execute('SELECT COUNT(*) FROM jobs WHERE score >= 80 AND score < 150').fetchone()[0]
print(f'Premium: {p} | Standard: {s} | Total: {p+s} eligible')
")

echo "Results: +${NEW} new jobs. ${STATS}" >> "$LOG"

# Discord webhook notification (optional)
if [ -n "$WEBHOOK" ]; then
  python3 -c "
from urllib.request import Request, urlopen
import json, os
webhook = os.environ['DISCORD_WEBHOOK_URL']
msg = f'Daily Pipeline done\n\n+${NEW} new jobs (Total: ${AFTER})\n${STATS}'
data = json.dumps({'content': msg}).encode()
req = Request(webhook, data=data)
req.add_header('Content-Type', 'application/json')
urlopen(req, timeout=30)
" >> "$LOG" 2>&1 || true
fi
