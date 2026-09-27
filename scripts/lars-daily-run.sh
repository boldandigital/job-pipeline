#!/usr/bin/env bash
# ╔═══════════════════════════════════════════════════════════════════════╗
# ║  LARS DAILY RUN — cron-targetable wrapper for the dome317 pipeline   ║
# ║  Fire-at: 09:00 Europe/Brussels (set up via `crontab -e`)            ║
# ║                                                                      ║
# ║  Why a wrapper, not the bare pipeline:                                ║
# ║    1. Loads .env into the cron environment (cron has a minimal env)  ║
# ║    2. Rotates logs daily (logs grow fast — Haiku calls + scraping)   ║
# ║    3. Surfaces failures to Discord even if batch_pipeline errors    ║
# ║    4. Sleeps a small jitter on cold-start to avoid 09:00 thundering  ║
# ║    5. Exits 0 on partial success, 1 on hard failure (cron-friendly) ║
# ║                                                                      ║
# ║  Usage:                                                              ║
# ║    bash scripts/lars-daily-run.sh           # daily run, full         ║
# ║    bash scripts/lars-daily-run.sh --dry-run # preview top jobs, no   ║
# ║                                              Discord, no docs        ║
# ║    bash scripts/lars-daily-run.sh --test    # Discord ping only      ║
# ╚═══════════════════════════════════════════════════════════════════════╝

set -euo pipefail

# ───────────────────────────────────────────────────────────────────────
# Paths & flags
# ───────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

DRY_RUN=0
TEST_PING=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --test)    TEST_PING=1 ;;
    -h|--help)
      sed -n '2,18p' "$0"
      exit 0
      ;;
  esac
done

# ───────────────────────────────────────────────────────────────────────
# Load .env (cron runs with a minimal environment)
# ───────────────────────────────────────────────────────────────────────
ENV_FILE="$PROJECT_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "[FATAL] Missing $ENV_FILE — copy .env.example to .env and fill in tokens." >&2
  exit 2
fi

# Tolerate unquoted multi-word values (e.g. CANDIDATE_LOCATION="Brussels, Belgium (open to DACH remote)").
# `set -a + source` would execute the value as a command if it contains spaces or parens, so we parse
# KEY=VALUE ourselves, stripping a single layer of surrounding quotes.
load_dotenv() {
  local f="$1"
  local line key val
  while IFS= read -r line || [[ -n "$line" ]]; do
    # strip leading whitespace + optional `export `
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line#export }"
    line="${line#"${line%%[![:space:]]*}"}"
    # skip blanks + comments
    [[ -z "$line" || "$line" == \#* ]] && continue
    [[ "$line" != *=* ]] && continue
    key="${line%%=*}"
    val="${line#*=}"
    # strip one matching pair of surrounding quotes (single or double)
    if [[ "${val:0:1}" == '"' && "${val: -1}" == '"' ]]; then val="${val:1:-1}"
    elif [[ "${val:0:1}" == "'" && "${val: -1}" == "'" ]]; then val="${val:1:-1}"
    fi
    export "$key=$val"
  done < "$f"
}
load_dotenv "$ENV_FILE"

# ───────────────────────────────────────────────────────────────────────
# Sanity checks
# ───────────────────────────────────────────────────────────────────────
if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "[FATAL] ANTHROPIC_API_KEY missing in .env" >&2; exit 3
fi
if [[ "$TEST_PING" -eq 0 ]]; then
  if [[ -z "${DISCORD_WEBHOOK_URL:-}" ]]; then
    echo "[FATAL] DISCORD_WEBHOOK_URL missing in .env" >&2
    echo "  → Create a webhook in your Discord channel (⚙️ Settings → Integrations → Webhooks) and paste the URL." >&2
    exit 4
  fi
fi
if [[ ! -d "$PROJECT_DIR/data" ]]; then mkdir -p "$PROJECT_DIR/data"; fi
mkdir -p "$PROJECT_DIR/logs"

# ───────────────────────────────────────────────────────────────────────
# Log rotation — keep last 14 daily logs, last 30 error logs
# ───────────────────────────────────────────────────────────────────────
LOG_DIR="$PROJECT_DIR/logs"
DAILY_LOG="$LOG_DIR/lars-daily-$(date +%Y%m%d).log"
ERROR_LOG="$LOG_DIR/lars-error-$(date +%Y%m%d).log"

# Drop any log file older than 14 days (daily) / 30 days (error)
find "$LOG_DIR" -maxdepth 1 -name "lars-daily-*.log"  -mtime +14 -delete 2>/dev/null || true
find "$LOG_DIR" -maxdepth 1 -name "lars-error-*.log" -mtime +30 -delete 2>/dev/null || true

exec > >(tee -a "$DAILY_LOG") 2>&1
exec 3>>"$ERROR_LOG"

log()   { printf '[%s] %s\n' "$(date '+%H:%M:%S %Z')" "$*"; }
fail()  { log "[FAIL] $*" >&3; log "[FAIL] $*"; exit 1; }

log "──────────────────────────────────────────────────────────────"
log "Lars daily pipeline START  (PID $$)"
log "Project: $PROJECT_DIR"
log "Python:  $(which python3) ($(python3 --version 2>&1))"

# Cold-start jitter — avoid stampede at exactly 09:00:00
if [[ "$DRY_RUN" -eq 0 && "$TEST_PING" -eq 0 ]]; then
  JITTER=$((RANDOM % 60))
  log "Cold-start jitter: ${JITTER}s"
  sleep "$JITTER"
fi

# ───────────────────────────────────────────────────────────────────────
# Test-only Discord ping — verifies webhook before enabling cron
# ───────────────────────────────────────────────────────────────────────
if [[ "$TEST_PING" -eq 1 ]]; then
  log "Test ping: sending a one-off Discord message"
  /usr/bin/env python3 - <<'PY' || fail "Discord ping failed"
import os, json, urllib.request
webhook = os.environ["DISCORD_WEBHOOK_URL"]
username = os.environ.get("DISCORD_USERNAME") or None
payload = {
    "content": "⚓ job-pipeline ping — creds OK, ready for daily run."
}
if username:
    payload["username"] = username
data = json.dumps(payload).encode()
req  = urllib.request.Request(webhook, data=data, method="POST")
req.add_header("Content-Type", "application/json")
with urllib.request.urlopen(req, timeout=30) as r:
    print("discord status:", r.status)
PY
  log "Discord ping OK"
  exit 0
fi

# ───────────────────────────────────────────────────────────────────────
# Dry-run: print top jobs, no docs, no Discord
# ───────────────────────────────────────────────────────────────────────
if [[ "$DRY_RUN" -eq 1 ]]; then
  log "Dry-run: scoring + selection only"
  /usr/bin/env python3 -m src.scoring.score_jobs --db "$DB_PATH" 2>&1 || true
  /usr/bin/env python3 -m src.pipeline.batch_pipeline --db "$DB_PATH" --limit 20 --dry-run
  exit 0
fi

# ───────────────────────────────────────────────────────────────────────
# Full run: score → batch → CV/CL → ZIP → Discord summary
# ───────────────────────────────────────────────────────────────────────
log "Step 1/3: keyword scoring"
/usr/bin/env python3 -m src.scoring.score_jobs --db "$DB_PATH" 2>&1 || fail "scoring crashed"

log "Step 2/3: batch pipeline (top 50 → CV + CL → ZIP)"
set +e
/usr/bin/env python3 -m src.pipeline.batch_pipeline \
  --db "$DB_PATH" \
  --limit 50 \
  --min-score 20 2>&1
batch_rc=$?
set -e

if [[ $batch_rc -ne 0 ]]; then
  fail "batch_pipeline exited $batch_rc — see $ERROR_LOG"
fi

log "Step 3/3: send friendly summary even if batch was empty"
SUMMARY="⚓ Daily batch done at $(date '+%H:%M %Z').
Jobs in DB: $(/usr/bin/env python3 -c "import sqlite3; print(sqlite3.connect('$DB_PATH').execute('SELECT COUNT(*) FROM jobs').fetchone()[0])")
Top-tier (score≥150): $(/usr/bin/env python3 -c "import sqlite3; print(sqlite3.connect('$DB_PATH').execute(\"SELECT COUNT(*) FROM jobs WHERE score>=150\").fetchone()[0])")
Standard (80–149): $(/usr/bin/env python3 -c "import sqlite3; print(sqlite3.connect('$DB_PATH').execute(\"SELECT COUNT(*) FROM jobs WHERE score>=80 AND score<150\").fetchone()[0])")
Dashboard: http://127.0.0.1:8080"

/usr/bin/env python3 - <<PY || log "summary send failed (non-fatal)"
import os, json, urllib.request
webhook  = os.environ["DISCORD_WEBHOOK_URL"]
username = os.environ.get("DISCORD_USERNAME") or None
payload = {
    "content": """$SUMMARY""",
    "embeds": [{
        "title": "Daily batch — quick stats",
        "fields": [
            {"name": "Run",        "value": "$(date '+%Y-%m-%d %H:%M %Z')", "inline": True},
            {"name": "Top-tier",   "value": "$(/usr/bin/env python3 -c "import sqlite3; print(sqlite3.connect('$DB_PATH').execute(\"SELECT COUNT(*) FROM jobs WHERE score>=150\").fetchone()[0])")",  "inline": True},
            {"name": "Standard",   "value": "$(/usr/bin/env python3 -c "import sqlite3; print(sqlite3.connect('$DB_PATH').execute(\"SELECT COUNT(*) FROM jobs WHERE score>=80 AND score<150\").fetchone()[0])")", "inline": True},
        ],
        "footer": {"text": "job-pipeline ADOPT-7 — Discord webhook delivery"},
    }],
}
if username:
    payload["username"] = username
data = json.dumps(payload).encode()
req  = urllib.request.Request(webhook, data=data, method="POST")
req.add_header("Content-Type", "application/json")
with urllib.request.urlopen(req, timeout=30) as r:
    print("summary status:", r.status)
PY

log "Lars daily pipeline DONE"
exit 0
