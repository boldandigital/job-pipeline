#!/usr/bin/env bash
# ╔═══════════════════════════════════════════════════════════════════════╗
# ║  bin/apply-cua.sh — ADOPT-12 (CUA submission) shell wrapper          ║
# ║                                                                     ║
# ║  Iterates jobs.status = 'approved' in SQLite and submits them via   ║
# ║  the desktop CUA driver. Per ADOPT-12 safety contract:              ║
# ║                                                                     ║
# ║    - Generic fallback NEVER auto-submits (pauses for human review)  ║
# ║    - Login walls / captchas -> needs_human (no auto-solve)          ║
# ║    - Idempotent: re-running skips already-submitted jobs            ║
# ║    - Rate limited: max 5 applies per run, 30s default delay        ║
# ║    - Audit trail: auto/logs/<date>.jsonl (via the runner)          ║
# ║                                                                     ║
# ║  Usage:                                                             ║
# ║    bash bin/apply-cua.sh                       # default run        ║
# ║    bash bin/apply-cua.sh --dry-run             # list only          ║
# ║    bash bin/apply-cua.sh --job-id 42          # single job         ║
# ║    bash bin/apply-cua.sh --max 5 --delay 30   # custom cadence     ║
# ║                                                                     ║
# ║  Exit codes:                                                        ║
# ║    0  — run finished (cron-friendly: never fails the cron)          ║
# ║    2  — bad invocation (missing arg, unknown flag)                  ║
# ╚═══════════════════════════════════════════════════════════════════════╝

set -euo pipefail

# ---- QA-12 fix: pre-initialize accumulators -----------------------------
# `set -u` would otherwise keelhaul us when we try to expand an unset
# accumulator like "${ARGS[@]}". Declaring them empty up front satisfies
# nounset and keeps the flag-parse loops clean.
ARGS=()
SUBCMD_ARGS=()

log()   { printf '[%s] %s\n' "$(date '+%H:%M:%S %Z')" "$*"; }
fail()  { log "[FAIL] $*" >&2; exit "${EXIT_BAD_INVOCATION:-2}"; }

# ───────────────────────────────────────────────────────────────────────
# Paths
# ───────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

# Defaults (env-overridable, same pattern as apply-from-sheet.sh).
: "${DB_PATH:=$PROJECT_DIR/data/jobs.db}"
: "${BATCHES_DIR:=$PROJECT_DIR/data/batches}"
: "${APPLY_LOGS_DIR:=$PROJECT_DIR/logs/apply}"
: "${SCREENSHOTS_DIR:=$PROJECT_DIR/screenshots}"
: "${VENV_PY:=$PROJECT_DIR/.venv/bin/python}"
: "${MAX:=5}"
: "${DELAY:=30}"

# ───────────────────────────────────────────────────────────────────────
# Flag parsing
# ───────────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      ARGS+=(--dry-run)
      shift
      ;;
    --job-id)
      [[ -n "${2:-}" ]] || fail "--job-id requires an argument"
      ARGS+=(--job-id "$2")
      shift 2
      ;;
    --max)
      [[ -n "${2:-}" ]] || fail "--max requires an argument"
      MAX="$2"
      ARGS+=(--limit "$2")
      shift 2
      ;;
    --delay)
      [[ -n "${2:-}" ]] || fail "--delay requires an argument"
      DELAY="$2"
      ARGS+=(--delay "$2")
      shift 2
      ;;
    -v|--verbose)
      ARGS+=(--verbose)
      shift
      ;;
    -h|--help)
      sed -n '2,30p' "$0"
      exit 0
      ;;
    *)
      fail "unknown flag: $1 (try --help)"
      ;;
  esac
done

# --max hard cap (5 per run, per ADOPT-12 brief).
if (( MAX > 5 )); then
  log "WARN: --max=$MAX exceeds the ADOPT-12 cap of 5; clamping to 5"
  MAX=5
  # Rewrite the --limit arg we already appended.
  for i in "${!ARGS[@]}"; do
    if [[ "${ARGS[$i]}" == "--limit" ]]; then
      ARGS[$((i+1))]="5"
    fi
  done
fi

# ───────────────────────────────────────────────────────────────────────
# Pre-flight
# ───────────────────────────────────────────────────────────────────────
if [[ ! -f "$DB_PATH" ]]; then
  fail "DB not found at $DB_PATH — run scripts/setup.sh first."
fi
if [[ ! -x "$VENV_PY" ]]; then
  fail "venv python not found at $VENV_PY — run scripts/setup.sh first."
fi

mkdir -p "$BATCHES_DIR" "$APPLY_LOGS_DIR" "$SCREENSHOTS_DIR"

log "apply-cua starting"
log "  DB:           $DB_PATH"
log "  Batches:      $BATCHES_DIR"
log "  Logs:         $APPLY_LOGS_DIR"
log "  Screenshots:  $SCREENSHOTS_DIR"
log "  Max applies:  $MAX"
log "  Delay (s):    $DELAY"

# ───────────────────────────────────────────────────────────────────────
# Run
# ───────────────────────────────────────────────────────────────────────
# Use a python subcommand invocation. The -- separator prevents user args
# from being interpreted as further python flags.
SUBCMD_ARGS=(
  --db "$DB_PATH"
  --batches-dir "$BATCHES_DIR"
  --logs-dir "$APPLY_LOGS_DIR"
  --screenshots-dir "$SCREENSHOTS_DIR"
)

set +e
"$VENV_PY" -m src.apply.runner "${SUBCMD_ARGS[@]}" "${ARGS[@]}"
run_rc=$?
set -e

if [[ $run_rc -ne 0 ]]; then
  log "WARN: runner exited $run_rc — see $APPLY_LOGS_DIR for details"
fi

log "apply-cua done"
exit 0
