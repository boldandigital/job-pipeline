#!/usr/bin/env bash
# ╔═══════════════════════════════════════════════════════════════════════╗
# ║  bin/captain.sh — THE daily orchestrator for the job pipeline         ║
# ║                                                                     ║
# ║  Full cycle: discover → score → render → wait (for Lars to triage   ║
# ║  in the dashboard) → apply. Composed of the existing pieces:         ║
# ║    - discover:  src.scrapers.{stepstone,xing,arbeitsagentur}        ║
# ║    - score:     src.scoring.score_jobs (+ llm_scorer if available)  ║
# ║    - render:    src.pipeline.batch_pipeline (top 10 → CV + CL)      ║
# ║    - apply:     src.apply.runner (CUA submission, --with-cua only)  ║
# ║                                                                     ║
# ║  State file:  data/last_cycle.json                                  ║
# ║               { discover_at, score_at, render_at, apply_at }        ║
# ║  Discord:     one summary line per phase                             ║
# ║  Idempotent:  re-runs don't dupe jobs (scrapers use UNIQUE           ║
# ║               (title,company); apply runner skips submitted jobs)   ║
# ║                                                                     ║
# ║  Usage:                                                              ║
# ║    bash bin/captain.sh once                              # full     ║
# ║    bash bin/captain.sh discover                          # just 1   ║
# ║    bash bin/captain.sh score [--jobs 79,63]               # default: ║
# ║                                                          # all new  ║
# ║    bash bin/captain.sh render [--job-ids 79,63,76]        # top-10  ║
# ║                                                          # by score ║
# ║    bash bin/captain.sh apply [--all-pending]             # only if  ║
# ║                                                          # explicit ║
# ║    bash bin/captain.sh status                            # last run ║
# ║    bash bin/captain.sh --help                            # this     ║
# ║                                                                     ║
# ║  Safety rails:                                                       ║
# ║    - 'apply' is NEVER run by 'once' (waiting for dashboard triage    ║
# ║      and an explicit GO). To actually apply, run `captain apply`.   ║
# ║    - Failed phase halts the run, updates state with the error, and  ║
# ║      notifies Discord. Re-run is safe (idempotent).                ║
# ╚═══════════════════════════════════════════════════════════════════════╝

set -euo pipefail

# ───────────────────────────────────────────────────────────────────────
# Paths & env
# ───────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

: "${DB_PATH:=$PROJECT_DIR/data/jobs.db}"
: "${BATCHES_DIR:=$PROJECT_DIR/data/batches}"
: "${VENV_PY:=$PROJECT_DIR/.venv/bin/python}"
: "${PYTHON_BIN:=$(command -v "$VENV_PY" 2>/dev/null || which python3)}"
: "${SCORING_CONFIG:=$PROJECT_DIR/config/lars.yaml}"
: "${RENDER_LIMIT:=10}"          # top N to render per cycle
: "${DAILY_APPLY_CAP:=5}"        # hard cap per apply run (ADOPT-12)
: "${STATE_FILE:=$PROJECT_DIR/data/last_cycle.json}"
: "${LOG_DIR:=$PROJECT_DIR/logs}"
: "${ENV_FILE:=$PROJECT_DIR/.env}"
: "${CAPTAIN_LABEL:=captain}"
: "${SCRAPE_SOURCES:=stepstone,xing,arbeitsagentur,ats}"

export DB_PATH BATCHES_DIR SCORING_CONFIG VENV_PY PYTHON_BIN STATE_FILE

# ───────────────────────────────────────────────────────────────────────
# Logging
# ───────────────────────────────────────────────────────────────────────
mkdir -p "$LOG_DIR"
DAILY_LOG="$LOG_DIR/captain-$(date +%Y%m%d).log"
ERROR_LOG="$LOG_DIR/captain-error-$(date +%Y%m%d).log"

# Find-by-date logs older than 14 days (matches apply-cua.sh pattern)
find "$LOG_DIR" -maxdepth 1 -name "captain-*.log" -mtime +14 -delete 2>/dev/null || true

exec 3>>"$ERROR_LOG"
log()    { printf '[%s] %s\n' "$(date '+%H:%M:%S %Z')" "$*" | tee -a "$DAILY_LOG"; }
err()    { printf '[%s] [FAIL] %s\n' "$(date '+%H:%M:%S %Z')" "$*" | tee -a "$DAILY_LOG" >&2; }
fail()   { err "$*"; exit 1; }

# ───────────────────────────────────────────────────────────────────────
# .env loader (tolerates multi-word + paren values, same as lars-daily-run.sh)
# ───────────────────────────────────────────────────────────────────────
if [[ -r "$ENV_FILE" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line#export }"
    line="${line#"${line%%[![:space:]]*}"}"
    [[ -z "$line" || "$line" == \#* ]] && continue
    [[ "$line" != *=* ]] && continue
    key="${line%%=*}"
    val="${line#*=}"
    if [[ "${val:0:1}" == '"' && "${val: -1}" == '"' ]]; then val="${val:1:-1}"
    elif [[ "${val:0:1}" == "'" && "${val: -1}" == "'" ]]; then val="${val:1:-1}"
    fi
    # Don't clobber values passed via env
    if [[ -z "${!key:-}" ]]; then export "$key=$val"; fi
  done < "$ENV_FILE"
fi

# Fall back to Hermes env for Discord if missing locally
if [[ -z "${DISCORD_BOT_TOKEN:-}" || -z "${DISCORD_CHANNEL_ID:-}" ]]; then
  HERMES_ENV="${HERMES_ENV:-$HOME/.hermes/.env}"
  if [[ -r "$HERMES_ENV" ]]; then
    for k in DISCORD_BOT_TOKEN DISCORD_HOME_CHANNEL DISCORD_USERNAME; do
      if [[ -z "${!k:-}" ]]; then
        v="$(grep -E "^${k}=" "$HERMES_ENV" | head -1 | cut -d= -f2- || true)"
        if [[ -n "$v" ]]; then export "$k=$v"; fi
      fi
    done
    : "${DISCORD_CHANNEL_ID:=${DISCORD_HOME_CHANNEL:-}}"
    export DISCORD_CHANNEL_ID
  fi
fi

# ───────────────────────────────────────────────────────────────────────
# State file helpers — atomic write (write-then-mv)
# ───────────────────────────────────────────────────────────────────────
state_init() {
  [[ -f "$STATE_FILE" ]] || echo '{}' > "$STATE_FILE"
}

state_set() {
  # state_set <key> <value>
  # If <value> is a non-negative integer (no decimal point, no sign),
  # store it as a JSON int. Otherwise store as a string.
  local k="$1" v="$2"
  python3 - "$STATE_FILE" "$k" "$v" <<'PY'
import json, os, sys, tempfile
path, k, v = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path) as f: data = json.load(f)
except Exception:
    data = {}
# Numeric values get stored as ints; everything else as a string.
# Empty string → JSON null (lets the consumer distinguish "no value" from "0").
if v == "":
    data[k] = None
elif v.lstrip("-").isdigit():
    data[k] = int(v)
else:
    data[k] = v
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f, indent=2, sort_keys=True)
os.replace(tmp, path)
PY
}

state_get() {
  python3 - "$STATE_FILE" "$1" <<'PY'
import json, sys
path, k = sys.argv[1], sys.argv[2]
try:
    with open(path) as f: print(json.load(f).get(k, ""))
except Exception:
    pass
PY
}

state_all() {
  python3 - "$STATE_FILE" <<'PY'
import json, sys
try:
    with open(sys.argv[1]) as f: print(json.dumps(json.load(f), indent=2, sort_keys=True))
except Exception:
    print("{}")
PY
}

# ───────────────────────────────────────────────────────────────────────
# DB counts — used for Discord summary lines
# ───────────────────────────────────────────────────────────────────────
db_count_total() {
  python3 - "$DB_PATH" <<'PY' 2>/dev/null
import sqlite3, sys
try:
    c = sqlite3.connect(sys.argv[1], timeout=10)
    print(c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
except Exception:
    print(0)
PY
}

db_count_since() {
  # db_count_since <iso8601-timestamp>
  python3 - "$DB_PATH" "$1" <<'PY' 2>/dev/null
import sqlite3, sys
try:
    c = sqlite3.connect(sys.argv[1], timeout=10)
    print(c.execute("SELECT COUNT(*) FROM jobs WHERE created_at >= ?", (sys.argv[2],)).fetchone()[0])
except Exception:
    print(0)
PY
}

db_count_fit() {
  # Count jobs at or above the SCORING threshold (default 20)
  local threshold="${1:-20}"
  python3 - "$DB_PATH" "$threshold" <<'PY' 2>/dev/null
import sqlite3, sys
try:
    c = sqlite3.connect(sys.argv[1], timeout=10)
    print(c.execute("SELECT COUNT(*) FROM jobs WHERE score >= ?", (int(sys.argv[2]),)).fetchone()[0])
except Exception:
    print(0)
PY
}

db_count_top_n() {
  # db_count_top_n <N> <min-score>
  local n="$1" min_score="${2:-20}"
  python3 - "$DB_PATH" "$n" "$min_score" <<'PY' 2>/dev/null
import sqlite3, sys
try:
    c = sqlite3.connect(sys.argv[1], timeout=10)
    print(c.execute(
        "SELECT COUNT(*) FROM jobs WHERE score >= ? "
        "AND status NOT IN ('applied','filtered_out','batched','skipped') "
        "ORDER BY score DESC LIMIT ?", (int(sys.argv[2]), int(sys.argv[3])),
    ).fetchone()[0])
except Exception:
    print(0)
PY
}

db_count_approved_pending() {
  python3 - "$DB_PATH" <<'PY' 2>/dev/null
import sqlite3, sys
try:
    c = sqlite3.connect(sys.argv[1], timeout=10)
    print(c.execute("SELECT COUNT(*) FROM jobs WHERE status='approved'").fetchone()[0])
except Exception:
    print(0)
PY
}

# ───────────────────────────────────────────────────────────────────────
# Discord helper — bot mode (preferred) or webhook (fallback)
# ───────────────────────────────────────────────────────────────────────
discord_send() {
  local msg="$1"
  local attachment="${2:-}"
  DISCORD_BOT_TOKEN="${DISCORD_BOT_TOKEN:-}" \
  DISCORD_CHANNEL_ID="${DISCORD_CHANNEL_ID:-}" \
  DISCORD_WEBHOOK_URL="${DISCORD_WEBHOOK_URL:-}" \
  DISCORD_USERNAME="${DISCORD_USERNAME:-Captain}" \
  "$PYTHON_BIN" - "$msg" "$attachment" <<'PY' 2>&1 | tee -a "$DAILY_LOG" >&3
import os, sys, json, urllib.request, urllib.error

msg = sys.argv[1]
attachment = sys.argv[2] if len(sys.argv) > 2 else ""
token = os.environ.get("DISCORD_BOT_TOKEN", "")
channel = os.environ.get("DISCORD_CHANNEL_ID", "")
webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
username = os.environ.get("DISCORD_USERNAME", "Captain")

if not token or not channel:
    if not webhook:
        # No Discord configured — silent skip
        sys.exit(0)

def _post(url, data, headers):
    req = urllib.request.Request(url, data=data, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    if "User-Agent" not in req.headers:
        req.add_header("User-Agent", "DiscordBot (job-pipeline-captain, 1.0)")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace") if e.fp else ""
        print(f"discord HTTP {e.code}: {body[:200]}", file=sys.stderr)
        return 0
    except Exception as e:
        print(f"discord error: {e}", file=sys.stderr)
        return 0

if token and channel:
    base = f"https://discord.com/api/v10/channels/{channel}/messages"
    headers = {"Authorization": f"Bot {token}"}
    payload = {"content": msg, "username": username}
    status = _post(base, json.dumps(payload).encode(),
                   {**headers, "Content-Type": "application/json"})
    print(f"discord bot status: {status}")
elif webhook:
    payload = {"content": msg, "username": username}
    status = _post(webhook, json.dumps(payload).encode(),
                   {"Content-Type": "application/json"})
    print(f"discord webhook status: {status}")
PY
}

# ───────────────────────────────────────────────────────────────────────
# Pre-flight
# ───────────────────────────────────────────────────────────────────────
preflight() {
  if [[ ! -f "$DB_PATH" ]]; then
    log "jobs.db missing — running init_db"
    "$PYTHON_BIN" - <<PY 2>&1 | tee -a "$ERROR_LOG" || fail "init_db failed"
import sys
sys.path.insert(0, "$PROJECT_DIR")
from src.db import init_db
init_db("$DB_PATH")
print("init_db OK")
PY
  fi
  if [[ ! -x "$VENV_PY" && "$PYTHON_BIN" == "$VENV_PY" ]]; then
    fail "venv python not found at $VENV_PY — run scripts/setup.sh first."
  fi
  mkdir -p "$BATCHES_DIR" "$LOG_DIR"
  state_init
}

# ───────────────────────────────────────────────────────────────────────
# Phase: DISCOVER
# Runs configured scrapers; idempotent (UNIQUE(title,company)).
# ───────────────────────────────────────────────────────────────────────
phase_discover() {
  log "Phase 1/4 — discover (sources=$SCRAPE_SOURCES)"
  local before after new_count
  before=$(db_count_total)
  local ts_start
  ts_start=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

  IFS=',' read -ra SOURCES <<< "$SCRAPE_SOURCES"
  local fail_count=0
  for src in "${SOURCES[@]}"; do
    src="$(echo "$src" | tr -d '[:space:]')"
    case "$src" in
      stepstone)
        log "  scraping stepstone…"
        set +e
        "$PYTHON_BIN" -m src.scrapers.stepstone_scraper \
          --queries "Founder" "CTO" "Head of Digital" "Geschäftsführer" \
          --location "Deutschland" --max-pages 3 --db "$DB_PATH" 2>&1 \
          | tee -a "$DAILY_LOG" >&3 || { err "stepstone failed (non-fatal)"; fail_count=$((fail_count+1)); }
        set -e
        ;;
      xing)
        log "  scraping xing…"
        set +e
        "$PYTHON_BIN" -m src.scrapers.xing_scraper \
          --queries "founder digital" "cto startup" "head of digital" \
                    "managing director agency" "geschäftsführer digital" \
          --location "Deutschland" --max-pages 3 --limit 25 --db "$DB_PATH" 2>&1 \
          | tee -a "$DAILY_LOG" >&3 || { err "xing failed (non-fatal)"; fail_count=$((fail_count+1)); }
        set -e
        ;;
      arbeitsagentur)
        log "  scraping arbeitsagentur…"
        set +e
        "$PYTHON_BIN" -m src.scrapers.arbeitsagentur_scraper \
          --queries "Founder" "CTO" "Head of Digital" "Geschäftsführer" \
          --limit 25 --db "$DB_PATH" 2>&1 \
          | tee -a "$DAILY_LOG" >&3 || { err "arbeitsagentur failed (non-fatal)"; fail_count=$((fail_count+1)); }
        set -e
        ;;
      ats)
        # Phase 1 (card): Greenhouse / Lever / Ashby public-API discovery.
        # Delegates to bin/discover.py which reads config/companies.yaml
        # (221 companies across the 3 platforms). Per-board failures (404,
        # rate-limit) do NOT abort the cycle.
        log "  scraping ATS boards (greenhouse/lever/ashby)…"
        set +e
        "$PYTHON_BIN" "$PROJECT_DIR/bin/discover.py" --platform all 2>&1 \
          | tee -a "$DAILY_LOG" >&3 || { err "bin/discover.py failed (non-fatal)"; fail_count=$((fail_count+1)); }
        set -e
        ;;
      "")
        ;;
      *)
        log "  unknown source: $src (skipping)"
        ;;
    esac
  done

  # XING saves title+url only — fetch descriptions for unprocessed jobs
  set +e
  "$PYTHON_BIN" scripts/fetch_descriptions.py --limit 25 2>&1 \
    | tee -a "$DAILY_LOG" >&3 || log "  (description fetch skipped)"
  set -e

  after=$(db_count_total)
  new_count=$((after - before))
  if (( new_count < 0 )); then new_count=0; fi

  state_set "discover_at" "$ts_start"
  state_set "discover_count" "$new_count"
  state_set "discover_failed" "$((fail_count + 0))"
  log "discover done: +${new_count} new jobs (${fail_count} source failure(s))"
  discord_send "🔎 Discover done: +${new_count} new jobs" || true
  # Set a "phase" marker so 'once' can resume if interrupted
  state_set "phase" "discovered"
}

# ───────────────────────────────────────────────────────────────────────
# Phase: SCORE
# Default: all unscored, non-triaged jobs. With --jobs: just those IDs.
# ───────────────────────────────────────────────────────────────────────
phase_score() {
  local jobs_arg=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --jobs) jobs_arg="${2:-}"; shift 2 ;;
      *) shift ;;
    esac
  done

  log "Phase 2/4 — score${jobs_arg:+ (jobs=$jobs_arg)}"
  local ts_start
  ts_start=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

  set +e
  if [[ -n "$jobs_arg" ]]; then
    # Score a specific set: rescore them all (ignoring the unscored-only filter)
    IDS_CSV="$jobs_arg" \
      "$PYTHON_BIN" - <<'PY' 2>&1 | tee -a "$DAILY_LOG" || { set -e; fail "scoring crashed"; }
import os, sys, sqlite3, yaml
sys.path.insert(0, ".")
from src.scoring.score_jobs import load_config, score_job

ids = [int(x) for x in os.environ.get("IDS_CSV", "").split(",") if x.strip()]
db = os.environ.get("DB_PATH", "./data/jobs.db")
cfg_path = os.environ.get("SCORING_CONFIG", "./config/lars.yaml")
cfg = load_config(cfg_path)
weights = cfg.get("scoring", {}).get("weights", {})
keywords = cfg.get("scoring", {}).get("keywords", {})
threshold = cfg.get("scoring", {}).get("threshold", 20)

c = sqlite3.connect(db)
c.row_factory = sqlite3.Row
placeholders = ",".join("?" * len(ids))
rows = c.execute(f"SELECT id, title, description FROM jobs WHERE id IN ({placeholders})", ids).fetchall()
for r in rows:
    s = score_job(r["title"], r["description"], keywords, weights)
    c.execute("UPDATE jobs SET score=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (s, r["id"]))
c.commit()
print(f"scored {len(rows)} jobs (threshold {threshold})")
PY
  else
    SCORING_CONFIG="$SCORING_CONFIG" \
      "$PYTHON_BIN" -m src.scoring.score_jobs --db "$DB_PATH" --config "$SCORING_CONFIG" \
      2>&1 | tee -a "$DAILY_LOG" || { set -e; fail "scoring crashed"; }
  fi
  set -e

  local fit_count
  fit_count=$(db_count_fit 20)
  state_set "score_at" "$ts_start"
  state_set "score_fit" "$fit_count"
  state_set "phase" "scored"
  log "score done: ${fit_count} jobs at score>=20"
  discord_send "🎯 Score done: ${fit_count} jobs at fit threshold (>=20)" || true
}

# ───────────────────────────────────────────────────────────────────────
# Phase: RENDER
# Generate CV + Anschreiben for top N (or explicit --job-ids).
# ───────────────────────────────────────────────────────────────────────
phase_render() {
  local job_ids_arg=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --job-ids) job_ids_arg="${2:-}"; shift 2 ;;
      --limit)   RENDER_LIMIT="${2:-10}"; shift 2 ;;
      *) shift ;;
    esac
  done

  log "Phase 3/4 — render (limit=$RENDER_LIMIT${job_ids_arg:+, explicit ids})"
  local ts_start
  ts_start=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

  set +e
  if [[ -n "$job_ids_arg" ]]; then
    # Render only the specified ids — for ad-hoc CV generation.
    # We use batch_pipeline's --limit with a temp filter via Python.
    IDS_CSV="$job_ids_arg" LIMIT="$RENDER_LIMIT" \
      "$PYTHON_BIN" -m src.pipeline.batch_pipeline --db "$DB_PATH" --limit "$RENDER_LIMIT" \
      2>&1 | tee -a "$DAILY_LOG" >&3 \
      || { set -e; fail "render (explicit ids) failed"; }
  else
    "$PYTHON_BIN" -m src.pipeline.batch_pipeline \
      --db "$DB_PATH" --limit "$RENDER_LIMIT" --min-score 20 \
      2>&1 | tee -a "$DAILY_LOG" >&3 \
      || { set -e; fail "render failed"; }
  fi
  set -e

  # Count what got rendered this cycle (batched today, score >= 20)
  local rendered
  rendered=$("$PYTHON_BIN" - "$DB_PATH" <<'PY' 2>/dev/null
import sqlite3, sys
try:
    c = sqlite3.connect(sys.argv[1], timeout=10)
    print(c.execute(
        "SELECT COUNT(*) FROM jobs WHERE status='batched' AND score >= 20"
    ).fetchone()[0])
except Exception:
    print(0)
PY
)
  state_set "render_at" "$ts_start"
  state_set "render_count" "$rendered"
  state_set "phase" "rendered"
  log "render done: ${rendered} job(s) marked batched"
  discord_send "📝 Render done: ${rendered} job(s) ready (CV+Anschreiben generated)" || true
}

# ───────────────────────────────────────────────────────────────────────
# Phase: APPLY
# Defaults to the ADOPT-12 cap.  --all-pending raises the cap to all
# approved jobs (still subject to apply-cua.sh's own internal cap of 5
# unless --max is bumped).  NEVER called by `once` — needs explicit GO.
# ───────────────────────────────────────────────────────────────────────
phase_apply() {
  local all_pending=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --all-pending) all_pending=1; shift ;;
      *) shift ;;
    esac
  done

  local limit="$DAILY_APPLY_CAP"
  if (( all_pending == 1 )); then
    limit=999
  fi

  log "Phase 4/4 — apply (limit=$limit)"
  local ts_start
  ts_start=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

  set +e
  bash "$PROJECT_DIR/bin/apply-cua.sh" --max "$limit" \
    2>&1 | tee -a "$DAILY_LOG" >&3 \
    || { set -e; fail "apply failed"; }
  set -e

  state_set "apply_at" "$ts_start"
  state_set "phase" "applied"
  log "apply done"
  discord_send "📮 Apply run done (cap=$limit)" || true
}

# ───────────────────────────────────────────────────────────────────────
# Subcommand: 'once' — full pipeline, but NO apply (waiting on dashboard)
# ───────────────────────────────────────────────────────────────────────
cmd_once() {
  preflight
  log "──────────────────────────────────────────────────────────────"
  log "captain once: starting full cycle (no auto-apply)"
  log "──────────────────────────────────────────────────────────────"

  local cycle_id
  cycle_id="cycle-$(date -u +%Y%m%dT%H%M%SZ)"
  state_set "cycle_id" "$cycle_id"

  # Phase 1: discover
  if ! phase_discover; then
    err "discover phase failed"
    state_set "last_error" "discover"
    discord_send "❌ Captain cycle ABORTED at discover phase" || true
    return 1
  fi

  # Phase 2: score
  if ! phase_score; then
    err "score phase failed"
    state_set "last_error" "score"
    discord_send "❌ Captain cycle ABORTED at score phase" || true
    return 2
  fi

  # Phase 3: render
  if ! phase_render; then
    err "render phase failed"
    state_set "last_error" "render"
    discord_send "❌ Captain cycle ABORTED at render phase" || true
    return 3
  fi

  # Phase 4 (waiting): tally and prompt for dashboard review
  local total fit_count top10 approved
  total=$(db_count_total)
  fit_count=$(db_count_fit 20)
  top10=$(db_count_top_n "$RENDER_LIMIT" 20)
  approved=$(db_count_approved_pending)

  state_set "wait_at" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  state_set "phase" "awaiting_review"

  log "──────────────────────────────────────────────────────────────"
  log "Cycle complete. Awaiting dashboard triage."
  log "  Total jobs in DB:        $total"
  log "  At fit threshold (>=20): $fit_count"
  log "  Top $RENDER_LIMIT ready:    $top10"
  log "  Approved (pending apply): $approved"
  log "  Dashboard:               http://127.0.0.1:8080"
  log "  Next:                    bash bin/captain.sh apply"
  log "──────────────────────────────────────────────────────────────"

  discord_send "🌅 Captain cycle done: ${top10} ready to send
📊 Total: ${total} | Fit (≥20): ${fit_count} | Approved pending apply: ${approved}
👉 Open http://127.0.0.1:8080 to triage, then: bash bin/captain.sh apply" \
    || true
}

# ───────────────────────────────────────────────────────────────────────
# Subcommand: status — print last cycle's timestamps + counts
# ───────────────────────────────────────────────────────────────────────
cmd_status() {
  echo "──────────────────────────────────────────────────────────────"
  echo "Captain — last cycle status"
  echo "  State file: $STATE_FILE"
  echo "──────────────────────────────────────────────────────────────"
  if [[ ! -f "$STATE_FILE" ]]; then
    echo "  (no cycles recorded yet)"
    return 0
  fi
  state_all
  echo
  echo "──────────────────────────────────────────────────────────────"
  echo "Live DB counts:"
  echo "  Total in DB:        $(db_count_total)"
  echo "  Fit (score >= 20):  $(db_count_fit 20)"
  echo "  Top $RENDER_LIMIT ready:  $(db_count_top_n "$RENDER_LIMIT" 20)"
  echo "  Approved pending:   $(db_count_approved_pending)"
  echo "──────────────────────────────────────────────────────────────"
}

# ───────────────────────────────────────────────────────────────────────
# --help
# ───────────────────────────────────────────────────────────────────────
print_help() {
  sed -n '2,46p' "$0"
}

# ───────────────────────────────────────────────────────────────────────
# Entry point
# ───────────────────────────────────────────────────────────────────────
SUBCMD="${1:-}"
shift || true

case "$SUBCMD" in
  once)     cmd_once "$@" ;;
  discover) preflight; phase_discover "$@" ;;
  score)    preflight; phase_score "$@" ;;
  render)   preflight; phase_render "$@" ;;
  apply)    preflight; phase_apply "$@" ;;
  status)   cmd_status "$@" ;;
  -h|--help|"") print_help ;;
  *)
    err "unknown subcommand: $SUBCMD"
    print_help
    exit 2
    ;;
esac
