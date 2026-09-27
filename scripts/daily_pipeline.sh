#!/bin/bash
# Daily Job Pipeline — runs all steps sequentially
# Schedule: 02:00 UTC daily via cron
#
# Required env vars: DB_PATH
# Discord: ADOPT-8 — bot mode (DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID) preferred,
# webhook (DISCORD_WEBHOOK_URL) still supported as fallback. See config/delivery.yaml.
#
# NOTE: Lars's wrapper (scripts/lars-daily-run.sh) handles delivery for the
# macOS-local 09:00 Brussels cron. This dome317 stock file is kept aligned for
# whoever runs the upstream pipeline directly (Docker compose path).

set -euo pipefail

LOG="${LOG_DIR:-/tmp}/daily_pipeline.log"
DB="${DB_PATH:-./data/jobs.db}"
WEBHOOK="${DISCORD_WEBHOOK_URL:-}"
BOT_TOKEN="${DISCORD_BOT_TOKEN:-}"
BOT_CHANNEL="${DISCORD_CHANNEL_ID:-${DISCORD_HOME_CHANNEL:-}}"
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

# Discord notification (optional) — ADOPT-8: bot mode preferred, webhook fallback
if [ -n "$BOT_TOKEN" ] && [ -n "$BOT_CHANNEL" ]; then
  DISCORD_BOT_TOKEN="$BOT_TOKEN" DISCORD_CHANNEL_ID="$BOT_CHANNEL" python3 -c "
import os, json, urllib.request
token   = os.environ['DISCORD_BOT_TOKEN']
channel = os.environ['DISCORD_CHANNEL_ID']
msg     = f'Daily Pipeline done\n\n+${NEW} new jobs (Total: ${AFTER})\n${STATS}'
url     = f'https://discord.com/api/v10/channels/{channel}/messages'
req     = urllib.request.Request(url, data=json.dumps({'content': msg}).encode())
req.add_header('Authorization', f'Bot {token}')
req.add_header('Content-Type', 'application/json')
# Cloudflare blocks default Python-urllib UA with 1010. Identify as DiscordBot.
req.add_header('User-Agent', 'DiscordBot (job-pipeline, 1.0)')
urllib.request.urlopen(req, timeout=30)
" >> "$LOG" 2>&1 || true
elif [ -n "$WEBHOOK" ]; then
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
