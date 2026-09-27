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
      sed -n '2,30p' "$0"
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

if [[ "$TEST_PING" -eq 0 && -z "$DISCORD_MODE" ]]; then
  echo "[FATAL] No Discord delivery configured. Need one of:" >&2
  echo "  • DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID (recommended — same Hermes bot)" >&2
  echo "  • DISCORD_WEBHOOK_URL (legacy webhook — see ADOPT-7 commit)" >&2
  echo "  See .env.example and docs/SECRETS.md." >&2
  exit 4
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
  /usr/bin/env python3 - "$attachment" <<PY || return $?
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
TOP_TIER_COUNT="$(/usr/bin/env python3 -c "import sqlite3; print(sqlite3.connect('$DB_PATH').execute(\"SELECT COUNT(*) FROM jobs WHERE score>=150\").fetchone()[0])")"
STANDARD_COUNT="$(/usr/bin/env python3 -c "import sqlite3; print(sqlite3.connect('$DB_PATH').execute(\"SELECT COUNT(*) FROM jobs WHERE score>=80 AND score<150\").fetchone()[0])")"
TOTAL_COUNT="$(/usr/bin/env python3 -c "import sqlite3; print(sqlite3.connect('$DB_PATH').execute('SELECT COUNT(*) FROM jobs').fetchone()[0])")"

SUMMARY="⚓ Daily batch done at $(date '+%H:%M %Z').
Jobs in DB: ${TOTAL_COUNT}
Top-tier (score≥150): ${TOP_TIER_COUNT}
Standard (80–149): ${STANDARD_COUNT}
Dashboard: http://127.0.0.1:8080"

discord_send "$SUMMARY" \
  || log "summary send failed (non-fatal)"

log "Lars daily pipeline DONE"
exit 0