#!/bin/bash
# Start the Captain's Bridge webapp as a background daemon.
#
# Usage:
#   bin/web-start.sh                 # default port 8742, workers 1
#   bin/web-start.sh --port 9000     # custom port
#   bin/web-start.sh --workers 2     # multi-worker (default 1)
#   bin/web-start.sh --no-logfile    # don't redirect to logs/web.log
#                                    # (stdout/stderr stay attached to the
#                                    # terminal — useful for debugging)
#   bin/web-start.sh --foreground    # don't background; Ctrl-C to stop
#                                    # (the same behavior the original
#                                    # web-start.sh had; useful for
#                                    # `npm run`-style iteration)
#
# What it does:
#   - If a live process is already recorded in logs/web.pid AND that process
#     is serving HTTP 200 on $PORT, exits 0 (idempotent).
#   - Otherwise launches uvicorn in the background, redirected to
#     logs/web.log (unless --no-logfile), and writes the PID atomically to
#     logs/web.pid.
#   - Waits up to 10s for the port to answer HTTP 200 before exiting. On
#     timeout it surfaces the last 20 log lines and kills the child.
#
# Auth: HTTP Basic (user=lars, pass=captain). Override via LARS_USER/LARS_PASS.
#
# For auto-restart on crash/reboot, use bin/web-watch.sh (which supervises
# uvicorn directly under launchd KeepAlive). Don't run web-start.sh AND the
# launchd-supervised web-watch.sh at the same time — you'll have two webapps
# fighting for the port.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/web-env.sh"

PORT="${PORT:-8742}"
HOST="${HOST:-127.0.0.1}"
WORKERS=1
USE_LOGFILE=1
FOREGROUND=0
LOGFILE="$WEB_ROOT/logs/web.log"
PIDFILE="$WEB_ROOT/logs/web.pid"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)        PORT="$2"; shift 2 ;;
    --port=*)      PORT="${1#*=}"; shift ;;
    --workers)     WORKERS="$2"; shift 2 ;;
    --workers=*)   WORKERS="${1#*=}"; shift ;;
    --no-logfile)  USE_LOGFILE=0; shift ;;
    --foreground)  FOREGROUND=1; USE_LOGFILE=0; shift ;;
    -h|--help)
      sed -n '2,32p' "$0"; exit 0 ;;
    *)
      echo "✗ unknown arg: $1" >&2
      exit 64
      ;;
  esac
done

cd "$WEB_ROOT"

# Idempotency: if PID file points to a live process, exit cleanly. We also
# confirm it's actually serving on the port (the PID could be a zombie or a
# different process that got the PID recycled).
if [[ -f "$PIDFILE" ]]; then
  existing="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [[ -n "$existing" ]] && kill -0 "$existing" 2>/dev/null; then
    if curl -sS -o /dev/null -w "%{http_code}" --max-time 2 "http://${HOST}:${PORT}/" 2>/dev/null | grep -q '^200$'; then
      echo "⚓ already running (pid $existing, http://${HOST}:${PORT})"
      exit 0
    fi
    # PID alive but not serving. Reap and continue (don't leave a zombie
    # bound to the port).
    kill -TERM "$existing" 2>/dev/null || true
    sleep 1
    kill -KILL "$existing" 2>/dev/null || true
  fi
  rm -f "$PIDFILE"
fi

if [[ ! -x "$PYBIN" ]]; then
  echo "✗ venv python missing: $PYBIN" >&2
  exit 1
fi

mkdir -p "$WEB_ROOT/logs"

# Build the uvicorn command in an array (no shell injection from args).
cmd=(
  "$PYBIN" -m uvicorn src.web.app:app
    --host "$HOST" --port "$PORT"
    --workers "$WORKERS"
    --no-access-log
)

if [[ "$FOREGROUND" -eq 1 ]]; then
  # --foreground is the original web-start.sh behavior: replace the shell
  # with uvicorn so Ctrl-C works naturally. No PID file is written; the
  # user owns the lifecycle.
  echo "⚓ Starting Captain's Bridge (foreground) on http://${HOST}:${PORT}"
  echo "   user:  $LARS_USER"
  echo "   root:  $WEB_ROOT"
  echo "   data:  data/jobs.db"
  exec "${cmd[@]}"
fi

echo "⚓ Starting Captain's Bridge on http://${HOST}:${PORT} (workers=${WORKERS})"
echo "   user:  $LARS_USER"
echo "   root:  $WEB_ROOT"
echo "   log:   ${LOGFILE}"
echo "   pid:   ${PIDFILE}"

if [[ "$USE_LOGFILE" -eq 1 ]]; then
  # >> not > so restarts append — handy for crash forensics.
  nohup "${cmd[@]}" >>"$LOGFILE" 2>&1 &
else
  nohup "${cmd[@]}" >/dev/null 2>&1 &
fi
LAUNCHED_PID=$!

# Disown so the background process survives shell exit even outside nohup
# (e.g. when called from a tool that monitors children).
disown "$LAUNCHED_PID" 2>/dev/null || true

# Write PID atomically: write to .pid.tmp, then mv into place. A reader that
# sees the PID file is therefore guaranteed to see the full PID, not a
# half-written number.
echo "$LAUNCHED_PID" > "$PIDFILE.tmp"
mv "$PIDFILE.tmp" "$PIDFILE"

# Wait for the port to answer (up to 10s). On timeout, kill the child and
# surface the last log lines so the user can see why.
ok=0
for _ in $(seq 1 50); do
  if ! kill -0 "$LAUNCHED_PID" 2>/dev/null; then
    break
  fi
  code="$(curl -sS -o /dev/null -w "%{http_code}" --max-time 1 "http://${HOST}:${PORT}/" 2>/dev/null || echo 000)"
  if [[ "$code" == "200" ]]; then
    ok=1
    break
  fi
  sleep 0.2
done

if [[ "$ok" -ne 1 ]]; then
  echo "✗ server failed to come up within 10s (last log lines):" >&2
  tail -n 20 "$LOGFILE" 2>/dev/null | sed 's/^/   /' >&2 || true
  # best-effort cleanup
  kill -TERM "$LAUNCHED_PID" 2>/dev/null || true
  rm -f "$PIDFILE"
  exit 1
fi

echo "⚓ up (pid $LAUNCHED_PID) — http://${HOST}:${PORT}"
