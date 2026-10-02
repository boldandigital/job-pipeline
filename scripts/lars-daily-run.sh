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
# ║  Discord delivery (ADOPT-8):                                          ║
# ║    Default = Discord bot API (uses the same Hermes bot/channel as    ║
# ║    ~/Documents/Projects/job-pipeline/.env). Falls back to webhook    ║
# ║    mode when only DISCORD_WEBHOOK_URL is set (e.g. dome317 Docker    ║
# ║    path). See docs/SECRETS.md for cross-project secret sharing.     ║
# ║                                                                      ║
# ║  Scraping (ADOPT-13):                                                 ║
# ║    Default sources = stepstone,xing,arbeitsagentur. XING is the     ║
# ║    DACH-native 4th platform (LinkedIn needs JobSpy — not set up).    ║
# ║    Use --source <csv> to override, --skip-scrape to use existing DB. ║
# ║                                                                      ║
# ║  Sheet approval loop (ADOPT-11):                                      ║
# ║    `--sync-from-sheet [tab]` reads ★ / ✗ marks from the named tab   ║
# ║    and applies them to the SQLite jobs table (status, rejection_*).  ║
# ║    Defaults to yesterday's tab — the most common case is "review    ║
# ║    yesterday's batch, sync before today's scrape at 09:00".          ║
# ║                                                                      ║
# ║  Sheet delivery (ADOPT-10):                                          ║
# ║    `--sheet` writes today's scored batch into a YYYY-MM-DD tab on    ║
# ║    the configured Google Sheet. Reuses the same Hermes service       ║
# ║    account as gpl-love (~/.hermes/credentials/google-service-account  ║
# ║    .json). Idempotent — re-running overwrites the day's tab.        ║
# ║                                                                      ║
# ║  Lifecycle sync (MAIL-2):                                            ║
# ║    `--sync-lifecycle [tab]` pushes DB lifecycle state (mail_*,      ║
# ║    interview_at, outcome, salary_range, cv_version) into the Sheet's ║
# ║    M→S columns. Pairs with `--sheet` — the daily batch creates the  ║
# ║    tab with A:L data, `--sync-lifecycle` patches the MAIL-2 columns.║
# ║                                                                      ║
# ║  Usage:                                                              ║
# ║    bash scripts/lars-daily-run.sh                       # daily run  ║
# ║    bash scripts/lars-daily-run.sh --dry-run             # no Discord║
# ║    bash scripts/lars-daily-run.sh --test                # ping only  ║
# ║    bash scripts/lars-daily-run.sh --source xing         # xing only ║
# ║    bash scripts/lars-daily-run.sh --skip-scrape         # no scrape ║
# ║    bash scripts/lars-daily-run.sh --sync-from-sheet     # sync yest.║
# ║    bash scripts/lars-daily-run.sh --sync-from-sheet 2026-09-26       ║
# ║                                              # sync a specific tab  ║
# ║    bash scripts/lars-daily-run.sh --sheet --sync-lifecycle           ║
# ║                                              # write + patch in one ║
# ║    bash scripts/lars-daily-run.sh --sync-lifecycle       # patch    ║
# ║    bash scripts/lars-daily-run.sh --apply              # generate   ║
# ║                                              # apply-zip for appr. ║
# ╚═══════════════════════════════════════════════════════════════════════╝

set -euo pipefail

log()   { printf '[%s] %s\n' "$(date '+%H:%M:%S %Z')" "$*"; }
fail()  { log "[FAIL] $*" >&3; log "[FAIL] $*"; exit 1; }

# ───────────────────────────────────────────────────────────────────────
# Paths & flags
# ───────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_PY="$PROJECT_DIR/.venv/bin/python"
PYTHON_BIN="${PYTHON_BIN:-$(command -v "$VENV_PY" 2>/dev/null || which python3)}"
cd "$PROJECT_DIR"

DRY_RUN=0
TEST_PING=0
SYNC_FROM_SHEET_TAB=""
SYNC_LIFECYCLE_TAB=""   # MAIL-2: tab to push DB lifecycle state into M:S
APPLY_MODE=0
SCRAPE_SOURCES="stepstone,xing,arbeitsagentur"   # ADOPT-13 default — LinkedIn needs JobSpy
SKIP_SCRAPE=0
SHEET_MODE=0   # ADOPT-10: also write the daily batch to today's Google Sheet tab
REST_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)        DRY_RUN=1 ;;
    --test)           TEST_PING=1 ;;
    --sync-from-sheet)
      # next arg is the optional tab name; if absent we'll default to yesterday
      if [[ "${2:-}" && "${2:-}" != --* ]]; then
        SYNC_FROM_SHEET_TAB="$2"; shift
      else
        SYNC_FROM_SHEET_TAB="__YESTERDAY__"
      fi
      ;;
    --sync-lifecycle)
      # MAIL-2: optional tab name; if absent we default to today (the tab
      # that --sheet just wrote). The flag is a no-op when the DB has no
      # jobs with lifecycle state — sync_lifecycle logs and exits 0.
      if [[ "${2:-}" && "${2:-}" != --* ]]; then
        SYNC_LIFECYCLE_TAB="$2"; shift
      else
        SYNC_LIFECYCLE_TAB="__TODAY__"
      fi
      ;;
    --apply)          APPLY_MODE=1 ;;
    --source)
      if [[ "${2:-}" && "${2:-}" != --* ]]; then
        SCRAPE_SOURCES="$2"; shift
      else
        echo "[FATAL] --source requires a value (e.g. --source stepstone,xing)" >&2
        exit 5
      fi
      ;;
    --source=*)       SCRAPE_SOURCES="${1#--source=}" ;;
    --skip-scrape)    SKIP_SCRAPE=1 ;;
    --sheet)          SHEET_MODE=1 ;;    # ADOPT-10
    -h|--help)
      sed -n '2,40p' "$0"
      echo
      echo "Scrape flags (ADOPT-13):"
      echo "  --source stepstone,xing,arbeitsagentur  Comma-separated scraper sources (default)"
      echo "  --skip-scrape                           Skip scraping step (use existing DB data)"
      echo
      echo "Sheet flags (ADOPT-10):"
      echo "  --sheet                                 After the batch step, write today's jobs to"
      echo "                                          today's YYYY-MM-DD tab. Needs GOOGLE_SPREADSHEET_ID"
      echo "                                          + GOOGLE_APPLICATION_CREDENTIALS in .env."
      echo "  --sync-lifecycle [tab]                  After --sheet, push DB lifecycle state (MAIL-2"
      echo "                                          columns M:S) into the tab. Defaults to today's tab."
      exit 0
      ;;
    *)                REST_ARGS+=("$1") ;;
  esac
  shift || true
done
export SCRAPE_SOURCES

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

# Fallback to the Hermes shared env when a Discord var is missing locally.
# This keeps job-pipeline from having to duplicate secrets — see docs/SECRETS.md.
HERMES_ENV="${HERMES_ENV:-$HOME/.hermes/.env}"
load_hermes_discord_fallback() {
  [[ -r "$HERMES_ENV" ]] || return 0
  # Only fill values that aren't already set by the project .env
  local k v
  for k in DISCORD_BOT_TOKEN DISCORD_HOME_CHANNEL DISCORD_HOME_CHANNEL_NAME DISCORD_ALLOWED_USERS DISCORD_USERNAME; do
    if [[ -z "${!k:-}" ]]; then
      v="$(grep -E "^${k}=" "$HERMES_ENV" | head -1 | cut -d= -f2- || true)"
      if [[ -n "$v" ]]; then export "$k=$v"; fi
    fi
  done
  # Convenience: the project's DISCORD_CHANNEL_ID defaults to Hermes's home channel
  : "${DISCORD_CHANNEL_ID:=${DISCORD_HOME_CHANNEL:-}}"
  export DISCORD_CHANNEL_ID
}
load_hermes_discord_fallback

# ───────────────────────────────────────────────────────────────────────
# Sanity checks
# ───────────────────────────────────────────────────────────────────────
if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "[FATAL] ANTHROPIC_API_KEY missing in .env" >&2; exit 3
fi

# Resolve the Discord delivery mode: bot (preferred) or webhook (legacy).
# Bot mode is selected when DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID are set.
# Webhook mode is only used when bot creds are absent but DISCORD_WEBHOOK_URL is set.
DISCORD_MODE=""
if [[ -n "${DISCORD_BOT_TOKEN:-}" && -n "${DISCORD_CHANNEL_ID:-}" ]]; then
  DISCORD_MODE="bot"
elif [[ -n "${DISCORD_WEBHOOK_URL:-}" ]]; then
  DISCORD_MODE="webhook"
fi

if [[ "$TEST_PING" -eq 0 && -z "$DISCORD_MODE" && "$SYNC_FROM_SHEET_TAB" == "" && "$APPLY_MODE" -eq 0 && -z "$SYNC_LIFECYCLE_TAB" ]]; then
  echo "[FATAL] No Discord delivery configured. Need one of:" >&2
  echo "  • DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID (recommended — same Hermes bot)" >&2
  echo "  • DISCORD_WEBHOOK_URL (legacy webhook — see ADOPT-7 commit)" >&2
  echo "  See .env.example and docs/SECRETS.md." >&2
  echo "  (Note: --sync-from-sheet, --sync-lifecycle, and --apply don't need Discord — pass that flag if it's missing.)" >&2
  exit 4
fi

if [[ ! -d "$PROJECT_DIR/data" ]]; then mkdir -p "$PROJECT_DIR/data"; fi
mkdir -p "$PROJECT_DIR/logs"

# ───────────────────────────────────────────────────────────────────────
# Ensure DB schema exists (ADOPT-9) — defensive idempotent init.
# Calls src/db/__init__.py:init_db() which is a no-op if the table already
# exists (CREATE TABLE IF NOT EXISTS). Safe to run on every startup; covers
# the case where setup.sh wasn't run.
# ───────────────────────────────────────────────────────────────────────
log_needs_init() {
  "$PYTHON_BIN" - "$DB_PATH" <<'PY' 2>/dev/null
import sqlite3, sys
db = sys.argv[1]
try:
    c = sqlite3.connect(db)
    c.execute("SELECT 1 FROM jobs LIMIT 1")
    sys.exit(0)          # table exists
except sqlite3.OperationalError:
    sys.exit(1)          # need init
PY
}

if [[ ! -n "${DB_PATH:-}" ]]; then
  : "${DB_PATH:=$PROJECT_DIR/data/jobs.db}"
fi

if ! log_needs_init; then
  log "Jobs table missing — running init_db($DB_PATH)"
  "$PYTHON_BIN" - "$DB_PATH" <<PY
import sys
sys.path.insert(0, "$PROJECT_DIR")
from src.db import init_db
conn = init_db(sys.argv[1])
n = len(conn.execute("PRAGMA table_info(jobs)").fetchall())
print(f"init_db OK: jobs table has {n} columns")
conn.close()
PY
else
  log "Jobs table already exists at $DB_PATH"
fi

# ───────────────────────────────────────────────────────────────────────
# Scoring config — ADOPT-9 wiring. Default to Lars's tuned config so the
# dry-run actually scores Founder/CTO roles >100 (see config/lars.yaml).
# Override with SCORING_CONFIG=./config/example.yaml for stock behavior.
# ───────────────────────────────────────────────────────────────────────
: "${SCORING_CONFIG:=$PROJECT_DIR/config/lars.yaml}"
export SCORING_CONFIG
log "Scoring config: $SCORING_CONFIG"

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

# log() and fail() defined near the top of the script (before any use).

log "──────────────────────────────────────────────────────────────"
log "Lars daily pipeline START  (PID $$)"
log "Project: $PROJECT_DIR"
log "Python:  $PYTHON_BIN ($($PYTHON_BIN --version 2>&1))"
export CV_GENERATOR="$PYTHON_BIN -m src.generation.cv_generator"
export CL_GENERATOR="$PYTHON_BIN -m src.generation.cover_letter_generator"
log "Discord: mode=$DISCORD_MODE  channel=${DISCORD_CHANNEL_ID:-${DISCORD_WEBHOOK_URL:0:40}…}"

# Cold-start jitter — avoid stampede at exactly 09:00:00
if [[ "$DRY_RUN" -eq 0 && "$TEST_PING" -eq 0 ]]; then
  JITTER=$((RANDOM % 60))
  log "Cold-start jitter: ${JITTER}s"
  sleep "$JITTER"
fi

# ───────────────────────────────────────────────────────────────────────
# Discord helpers — both modes share one place to extend later
# ───────────────────────────────────────────────────────────────────────
# discord_send <message> [<attachment_path>]
discord_send() {
  local msg="$1"
  local attachment="${2:-}"
  DISCORD_MODE="$DISCORD_MODE" \
  DISCORD_BOT_TOKEN="${DISCORD_BOT_TOKEN:-}" \
  DISCORD_CHANNEL_ID="${DISCORD_CHANNEL_ID:-}" \
  DISCORD_WEBHOOK_URL="${DISCORD_WEBHOOK_URL:-}" \
  DISCORD_USERNAME="${DISCORD_USERNAME:-}" \
  "$PYTHON_BIN" - "$attachment" <<PY || return $?
import os, json, sys, urllib.request, urllib.error

msg        = """$msg"""
attachment = sys.argv[1] if len(sys.argv) > 1 else ""
username   = os.environ.get("DISCORD_USERNAME") or None
mode       = os.environ.get("DISCORD_MODE", "")

def _post(url, data, headers):
    req = urllib.request.Request(url, data=data, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    # Cloudflare in front of discord.com blocks the default
    # `Python-urllib/x.y` UA (returns 1010). Identify as DiscordBot per
    # https://discord.com/developers/docs/reference#user-agent — that's
    # the official format and Cloudflare lets it through.
    if "User-Agent" not in req.headers:
        req.add_header("User-Agent", "DiscordBot (job-pipeline, 1.0)")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace") if e.fp else ""
        raise SystemExit(f"discord {e.code}: {body[:200]}")

if mode == "bot":
    token    = os.environ["DISCORD_BOT_TOKEN"]
    channel  = os.environ["DISCORD_CHANNEL_ID"]
    base     = f"https://discord.com/api/v10/channels/{channel}/messages"
    headers  = {"Authorization": f"Bot {token}"}

    if attachment:
        # multipart upload — 25 MB per file cap on the bot API
        boundary = "----jpboundary" + os.urandom(8).hex()
        body = []
        payload = {"content": msg}
        if username:
            payload["username"] = username
        body.append(f"--{boundary}\r\n".encode())
        body.append(b'Content-Disposition: form-data; name="payload_json"\r\n\r\n')
        body.append(json.dumps(payload).encode())
        body.append(b"\r\n")
        body.append(f"--{boundary}\r\n".encode())
        body.append(f'Content-Disposition: form-data; name="files[0]"; filename="{os.path.basename(attachment)}"\r\n'.encode())
        body.append(b"Content-Type: application/octet-stream\r\n\r\n")
        with open(attachment, "rb") as fh:
            body.append(fh.read())
        body.append(b"\r\n")
        body.append(f"--{boundary}--\r\n".encode())
        data = b"".join(body)
        status, _ = _post(base, data, {**headers, "Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        payload = {"content": msg}
        if username:
            payload["username"] = username
        status, _ = _post(base, json.dumps(payload).encode(),
                          {**headers, "Content-Type": "application/json"})
    print(f"discord bot status: {status}")
elif mode == "webhook":
    webhook = os.environ["DISCORD_WEBHOOK_URL"]
    payload = {"content": msg}
    if username:
        payload["username"] = username
    status, _ = _post(webhook, json.dumps(payload).encode(),
                      {"Content-Type": "application/json"})
    print(f"discord webhook status: {status}")
else:
    raise SystemExit("no Discord mode configured")
PY
}

# ───────────────────────────────────────────────────────────────────────
# ADOPT-11: --sync-from-sheet — read ★ / ✗ marks from the named tab
# (or yesterday's tab by default) and apply them to SQLite. Runs offline
# (no Discord), short-circuits the rest of the daily flow. Idempotent —
# safe to run multiple times per day.
# ───────────────────────────────────────────────────────────────────────
if [[ -n "$SYNC_FROM_SHEET_TAB" ]]; then
  if [[ "$SYNC_FROM_SHEET_TAB" == "__YESTERDAY__" ]]; then
    SYNC_FROM_SHEET_TAB="$(date -u -v-1d +%Y-%m-%d 2>/dev/null || date -u -d 'yesterday' +%Y-%m-%d)"
  fi
  log "ADOPT-11 sync-from-sheet: tab=$SYNC_FROM_SHEET_TAB  db=$DB_PATH"

  if [[ -z "${GOOGLE_SPREADSHEET_ID:-}" ]]; then
    log "[WARN] GOOGLE_SPREADSHEET_ID not set in .env — sync cannot read the Sheet."
    log "[WARN] Set it in /Users/lars/Documents/Projects/job-pipeline/.env then retry."
    log "[WARN] (See .env.example for the key name.)"
    exit 0
  fi

  # Pre-flight: ensure the schema has the ADOPT-11 columns. Migration is
  # idempotent — a no-op when the columns already exist.
  "$PYTHON_BIN" - "$DB_PATH" <<PY 2>&1 || log "(schema migration skipped)"
import sys
sys.path.insert(0, "$PROJECT_DIR")
from scripts.migrate_schema_add_approval_columns import migrate
migrate(sys.argv[1])
PY

  set +e
  $PYTHON_BIN -m src.sheet.sync_approvals \
    --sheet-id "$GOOGLE_SPREADSHEET_ID" \
    --db "$DB_PATH" \
    "$SYNC_FROM_SHEET_TAB"
  sync_rc=$?
  set -e

  if [[ $sync_rc -ne 0 ]]; then
    log "[WARN] sync_from_sheet exited $sync_rc — see $ERROR_LOG for details"
  fi
  log "sync-from-sheet DONE"
  exit 0
fi

# ───────────────────────────────────────────────────────────────────────
# MAIL-2: --sync-lifecycle — push DB lifecycle state into Sheet M:S
# columns. Standalone (no Discord required) so it can run on cron or
# on demand. Idempotent — re-running with unchanged DB state is a no-op.
# ───────────────────────────────────────────────────────────────────────
if [[ -n "$SYNC_LIFECYCLE_TAB" ]]; then
  if [[ "$SYNC_LIFECYCLE_TAB" == "__TODAY__" ]]; then
    SYNC_LIFECYCLE_TAB="$(date -u +%Y-%m-%d)"
  fi
  log "MAIL-2 sync-lifecycle: tab=$SYNC_LIFECYCLE_TAB  db=$DB_PATH"

  if [[ -z "${GOOGLE_SPREADSHEET_ID:-}" ]]; then
    log "[WARN] GOOGLE_SPREADSHEET_ID not set in .env — sync-lifecycle cannot reach the Sheet."
    log "[WARN] Set it in /Users/lars/Documents/Projects/job-pipeline/.env then retry."
    log "[WARN] (See .env.example for the key name.)"
    exit 0
  fi

  # Schema pre-flight: ensure MAIL-2 columns + outcome backfill. Idempotent.
  "$PYTHON_BIN" - "$DB_PATH" <<PY 2>&1 || log "(lifecycle migration skipped)"
import sys
sys.path.insert(0, "$PROJECT_DIR")
try:
    from scripts.migrate_schema_add_lifecycle import run as _ml
    _ml(sys.argv[1])
except Exception as e:
    print(f"lifecycle migration skipped: {e}")
PY

  set +e
  $PYTHON_BIN -m src.sheet.google_writer \
    --sheet-id "$GOOGLE_SPREADSHEET_ID" \
    --tab   "$SYNC_LIFECYCLE_TAB" \
    --sync-lifecycle \
    --db    "$DB_PATH" 2>&1
  sync_lc_rc=$?
  set -e

  if [[ $sync_lc_rc -ne 0 ]]; then
    log "[WARN] sync_lifecycle exited $sync_lc_rc — see $ERROR_LOG for details"
  else
    log "MAIL-2 sync-lifecycle DONE"
  fi
  exit 0
fi

# ───────────────────────────────────────────────────────────────────────
# ADOPT-11: --apply — generate tailored CV + Anschreiben for approved jobs
# Delegates to bin/apply-from-sheet.sh. Same offline semantics as sync.
# ───────────────────────────────────────────────────────────────────────
if [[ "$APPLY_MODE" -eq 1 ]]; then
  log "ADOPT-11 --apply: delegating to bin/apply-from-sheet.sh"
  APPLY_BIN="$PROJECT_DIR/bin/apply-from-sheet.sh"
  if [[ ! -x "$APPLY_BIN" ]]; then
    fail "$APPLY_BIN not executable — run: chmod +x $APPLY_BIN"
  fi
  # Pass through any unrecognized flags (e.g. --dry-run, --limit N).
  exec "$APPLY_BIN" "${REST_ARGS[@]}"
fi

# ───────────────────────────────────────────────────────────────────────
# Test-only Discord ping — verifies delivery before enabling cron
# ───────────────────────────────────────────────────────────────────────
if [[ "$TEST_PING" -eq 1 ]]; then
  log "Test ping: sending a one-off Discord message (mode=$DISCORD_MODE)"
  discord_send "⚓ job-pipeline ping — creds OK, ready for daily run." \
    || fail "Discord ping failed"
  log "Discord ping OK"
  exit 0
fi

# ───────────────────────────────────────────────────────────────────────
# Dry-run: print top jobs, no docs, no Discord
# ───────────────────────────────────────────────────────────────────────
if [[ "$DRY_RUN" -eq 1 ]]; then
  log "Dry-run: scoring + selection + career discovery"
  # SCORING_CONFIG exported above — picked up by score_jobs via --config arg.
  /usr/bin/env SCORING_CONFIG="$SCORING_CONFIG" \
    $PYTHON_BIN -m src.scoring.score_jobs --db "$DB_PATH" --config "$SCORING_CONFIG" 2>&1 || true
  # ADOPT-14: batch_pipeline --dry-run now also runs career discovery and logs
  # "Career URLs discovered: N (L1=X L2=Y L3=Z)" — see src/pipeline/batch_pipeline.py.
  $PYTHON_BIN -m src.pipeline.batch_pipeline --db "$DB_PATH" --limit 20 --dry-run
  CAREER_COUNT="$($PYTHON_BIN -c "import sqlite3; print(sqlite3.connect('$DB_PATH').execute('SELECT COUNT(*) FROM jobs WHERE career_url IS NOT NULL AND career_url!=\"\"').fetchone()[0])")"
  ATS_COUNT="$($PYTHON_BIN -c "import sqlite3; print(sqlite3.connect('$DB_PATH').execute('SELECT COUNT(*) FROM jobs WHERE ats_type IS NOT NULL AND ats_type!=\"\"').fetchone()[0])")"
  log "Dry-run summary: $CAREER_COUNT jobs have career_url, $ATS_COUNT have ATS detected"
  exit 0
fi

# ───────────────────────────────────────────────────────────────────────
# ADOPT-13: scrape step. Default sources = stepstone,xing,arbeitsagentur.
# LinkedIn is excluded by default — needs JobSpy which we haven't set up.
# Each source failure is logged but does NOT halt the pipeline (graceful).
# Set --skip-scrape to bypass (useful for re-scoring on existing data).
# ───────────────────────────────────────────────────────────────────────
run_scrapers() {
  if [[ "$SKIP_SCRAPE" -eq 1 ]]; then
    log "Scrape step skipped (--skip-scrape)"
    return 0
  fi
  log "Scrape sources: $SCRAPE_SOURCES"
  IFS=',' read -ra SOURCES <<< "$SCRAPE_SOURCES"
  for src in "${SOURCES[@]}"; do
    src="$(echo "$src" | tr -d '[:space:]')"
    case "$src" in
      stepstone)
        log "  scraping stepstone…"
        set +e
        $PYTHON_BIN -m src.scrapers.stepstone_scraper \
          --queries "Founder" "CTO" "Head of Digital" "Geschäftsführer" \
          --location "Deutschland" --max-pages 3 --db "$DB_PATH" 2>&1 \
          || log "  stepstone failed (non-fatal)"
        set -e
        ;;
      xing)
        log "  scraping xing…"
        set +e
        $PYTHON_BIN -m src.scrapers.xing_scraper \
          --queries "founder digital" "cto startup" "head of digital" \
                    "managing director agency" "geschäftsführer digital" \
          --location "Deutschland" --max-pages 3 --limit 25 --db "$DB_PATH" 2>&1 \
          || log "  xing failed (non-fatal)"
        set -e
        ;;
      arbeitsagentur)
        log "  scraping arbeitsagentur…"
        set +e
        $PYTHON_BIN -m src.scrapers.arbeitsagentur_scraper \
          --queries "Founder" "CTO" "Head of Digital" "Geschäftsführer" \
          --limit 25 --db "$DB_PATH" 2>&1 \
          || log "  arbeitsagentur failed (non-fatal)"
        set -e
        ;;
      "")
        ;;
      *)
        log "  unknown source: $src (skipping)"
        ;;
    esac
  done
}

# ───────────────────────────────────────────────────────────────────────
# Full run: scrape → score → batch → CV/CL → ZIP → Discord summary
# ───────────────────────────────────────────────────────────────────────
log "Step 1/4: scrape (sources=$SCRAPE_SOURCES)"
run_scrapers

# ADOPT-13 extension — XING scraper saves title+url but no description.
# Without descriptions, scoring can only match against title text and most
# rates come out as 0. Fetching descriptions before scoring makes scores
# meaningful.
if [[ "$SKIP_SCRAPE" -eq 0 ]] && [[ "$DRY_RUN" -eq 1 || "$DRY_RUN" -eq 0 ]]; then
  log "Step 1.5: fetch descriptions (XING jobs without text)"
  $PYTHON_BIN scripts/fetch_descriptions.py --limit 25 || log "(description fetch skipped or failed)"
fi

log "Step 2/4: keyword scoring"
# SCORING_CONFIG exported above — passed both via env and CLI for belt+suspenders.
/usr/bin/env SCORING_CONFIG="$SCORING_CONFIG" \
  $PYTHON_BIN -m src.scoring.score_jobs --db "$DB_PATH" --config "$SCORING_CONFIG" 2>&1 || fail "scoring crashed"

log "Step 3/4: batch pipeline (top 50 → CV + CL → ZIP)"
set +e
$PYTHON_BIN -m src.pipeline.batch_pipeline \
  --db "$DB_PATH" \
  --limit 50 \
  --min-score 20 2>&1
batch_rc=$?
set -e

if [[ $batch_rc -ne 0 ]]; then
  fail "batch_pipeline exited $batch_rc — see $ERROR_LOG"
fi

# ───────────────────────────────────────────────────────────────────────
# ADOPT-10: write today's scored batch into today's YYYY-MM-DD Sheet tab.
# Runs after batch_pipeline (so the day's CV/CL pipeline is already done)
# and before the Discord summary (so failures here can be reflected in the
# summary line). Re-running the same date is idempotent — the tab's data
# block is wiped and rewritten, no duplicates.
# ───────────────────────────────────────────────────────────────────────
if [[ "$SHEET_MODE" -eq 1 ]]; then
  if [[ -z "${GOOGLE_SPREADSHEET_ID:-}" ]]; then
    log "[WARN] --sheet requested but GOOGLE_SPREADSHEET_ID is not set in .env — skipping sheet write."
    log "[WARN] See .env.example and docs/DAILY-RUN.md §1.3 to set it up."
  else
    log "ADOPT-10 sheet write: sheet=$GOOGLE_SPREADSHEET_ID  db=$DB_PATH"
    TODAY_TAB="$(date -u +%Y-%m-%d)"

    # Schema pre-flight: ADOPT-11 added columns used here. ADOPT-10 is fine
    # without them, but the SELECT below reads rejection_reason if present.
    "$PYTHON_BIN" - "$DB_PATH" <<PY 2>&1 || log "(schema migration skipped)"
import sys
sys.path.insert(0, "$PROJECT_DIR")
try:
    from scripts.migrate_schema_add_approval_columns import migrate
    migrate(sys.argv[1])
except Exception as e:
    print(f"migrate skipped: {e}")
PY

    set +e
    $PYTHON_BIN -m src.sheet.google_writer \
      --sheet-id "$GOOGLE_SPREADSHEET_ID" \
      --db "$DB_PATH" \
      --tab "$TODAY_TAB" \
      --limit 50 \
      --min-score 20 2>&1
    sheet_rc=$?
    set -e

    if [[ $sheet_rc -ne 0 ]]; then
      log "[WARN] sheet write exited $sheet_rc — see $ERROR_LOG for details"
    else
      log "ADOPT-10 sheet write OK ($TODAY_TAB)"
      # MAIL-2: --sync-lifecycle was passed alongside --sheet — the
      # google_writer CLI handles the chain (sync after write). Nothing
      # more to do here; see google_writer main() :: --sync-lifecycle.
    fi
  fi
fi

log "Step 4/4: send friendly summary even if batch was empty"
TOP_TIER_COUNT="$($PYTHON_BIN -c "import sqlite3; print(sqlite3.connect('$DB_PATH').execute(\"SELECT COUNT(*) FROM jobs WHERE score>=150\").fetchone()[0])")"
STANDARD_COUNT="$($PYTHON_BIN -c "import sqlite3; print(sqlite3.connect('$DB_PATH').execute(\"SELECT COUNT(*) FROM jobs WHERE score>=80 AND score<150\").fetchone()[0])")"
TOTAL_COUNT="$($PYTHON_BIN -c "import sqlite3; print(sqlite3.connect('$DB_PATH').execute('SELECT COUNT(*) FROM jobs').fetchone()[0])")"
# ADOPT-14: surface career discovery stats in the Discord summary.
CAREER_COUNT="$($PYTHON_BIN -c "import sqlite3; print(sqlite3.connect('$DB_PATH').execute('SELECT COUNT(*) FROM jobs WHERE career_url IS NOT NULL AND career_url!=\"\"').fetchone()[0])")"
ATS_COUNT="$($PYTHON_BIN -c "import sqlite3; print(sqlite3.connect('$DB_PATH').execute('SELECT COUNT(*) FROM jobs WHERE ats_type IS NOT NULL AND ats_type!=\"\"').fetchone()[0])")"

SUMMARY="⚓ Daily batch done at $(date '+%H:%M %Z').
Jobs in DB: ${TOTAL_COUNT}
Top-tier (score≥150): ${TOP_TIER_COUNT}
Standard (80–149): ${STANDARD_COUNT}
Career URLs discovered: ${CAREER_COUNT} (${ATS_COUNT} with ATS detected)
Dashboard: http://127.0.0.1:8080"

discord_send "$SUMMARY" \
  || log "summary send failed (non-fatal)"

log "Lars daily pipeline DONE"
exit 0