#!/bin/bash
# Stop the Captain's Bridge webapp started by bin/web-start.sh.
#
# Usage:
#   bin/web-stop.sh             # SIGTERM, then SIGKILL after 10s
#   bin/web-stop.sh --force     # SIGKILL immediately
#
# Exits 0 on clean shutdown, 1 if the server was not running.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/web-env.sh"

PIDFILE="$WEB_ROOT/logs/web.pid"
INTERNAL_PIDFILE="$HOME/Library/Application Support/CaptainApply/web.pid"
FORCE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force|-f) FORCE=1; shift ;;
    -h|--help)
      sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "✗ unknown arg: $1" >&2; exit 64 ;;
  esac
done

# Resolve pid from either the project log dir or the launchd-friendly
# internal pid file. launchd-managed watchdogs (bin/web-watch.sh install)
# use the internal file because macOS TCC blocks external-volume writes.
PID=""
for cand in "$PIDFILE" "$INTERNAL_PIDFILE"; do
  if [[ -f "$cand" ]]; then
    cand_pid="$(cat "$cand" 2>/dev/null || true)"
    if [[ -n "$cand_pid" ]] && kill -0 "$cand_pid" 2>/dev/null; then
      PID="$cand_pid"
      PIDFILE_USED="$cand"
      break
    fi
  fi
done

if [[ -z "$PID" ]]; then
  echo "⚓ not running (no live pid found in $PIDFILE or $INTERNAL_PIDFILE)"
  exit 1
fi

echo "⚓ stopping pid $PID..."

if [[ "$FORCE" -eq 1 ]]; then
  kill -KILL "$PID" 2>/dev/null || true
else
  kill -TERM "$PID" 2>/dev/null || true
  # uvicorn forwards SIGTERM to workers; allow up to 10s for clean exit.
  for _ in $(seq 1 50); do
    if ! kill -0 "$PID" 2>/dev/null; then
      break
    fi
    sleep 0.2
  done
  if kill -0 "$PID" 2>/dev/null; then
    echo "   still alive after 10s — sending SIGKILL"
    kill -KILL "$PID" 2>/dev/null || true
    # Give the kernel a beat to reap.
    sleep 0.3
  fi
fi

# Belt-and-braces: also reap any uvicorn worker children that were forked.
# (uvicorn re-uses the master PID for --workers 1, so this is a no-op there
# but matters if someone bumps WORKERS.)
pkill -KILL -P "$PID" 2>/dev/null || true

rm -f "$PIDFILE" "$INTERNAL_PIDFILE"
echo "⚓ stopped"
