#!/bin/bash
# Daily Job Pipeline — runs all steps sequentially
# Schedule: 02:00 UTC daily via cron
#
# Required env vars: DB_PATH, TELEGRAM_CHAT_ID (optional)

set -euo pipefail

LOG="${LOG_DIR:-/tmp}/daily_pipeline.log"
DB="${DB_PATH:-./data/jobs.db}"
CHAT="${TELEGRAM_CHAT_ID:-}"
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

# Telegram notification (optional)
if [ -n "$CHAT" ] && [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
  python3 -c "
from urllib.request import Request, urlopen
import json, os
token = os.environ['TELEGRAM_BOT_TOKEN']
chat = os.environ['TELEGRAM_CHAT_ID']
msg = f'Daily Pipeline done\n\n+${NEW} new jobs (Total: ${AFTER})\n${STATS}'
data = json.dumps({'chat_id': chat, 'text': msg}).encode()
req = Request(f'https://api.telegram.org/bot{token}/sendMessage', data=data)
req.add_header('Content-Type', 'application/json')
urlopen(req, timeout=30)
" >> "$LOG" 2>&1 || true
fi
