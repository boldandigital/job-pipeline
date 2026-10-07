#!/bin/bash
# Report the status of the Captain's Bridge webapp.
#
# Output (running):
#   ⚓ Captain's Bridge: RUNNING
#      pid:     8566
#      port:    127.0.0.1:8742
#      uptime:  3h 12m
#      rss:     42M
#      port:    listening
#      log:     logs/web.log (last 20 lines below)
#      ──
#      ...last 20 log lines...
#
# Output (dead):
#   ✗ Captain's Bridge: DEAD
#      last log: <last line of logs/web.log>
#      pid file: logs/web.pid (stale, last pid 8566)

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/web-env.sh"

PIDFILE="$WEB_ROOT/logs/web.pid"
# launchd-managed processes (via bin/web-watch.sh install) write the pid
# to internal disk because macOS TCC blocks writes to external volumes.
# If the project log dir is unwritable, fall back to that file.
INTERNAL_PIDFILE="$HOME/Library/Application Support/CaptainApply/web.pid"
LOGFILE="$WEB_ROOT/logs/web.log"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8742}"

# When launchd is the supervisor, the project log dir is read-only from
# the watchdog's perspective. Always consider the internal pid file as a
# source of truth.
if [[ -f "$INTERNAL_PIDFILE" ]]; then
  PID="$(cat "$INTERNAL_PIDFILE" 2>/dev/null || true)"
elif [[ -f "$PIDFILE" ]]; then
  PID="$(cat "$PIDFILE" 2>/dev/null || true)"
else
  PID=""
fi

if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
  # Port check — process alive isn't enough; the uvicorn worker may have
  # crashed even if the master is still up.
  code="$(curl -sS -o /dev/null -w "%{http_code}" --max-time 2 "http://${HOST}:${PORT}/" 2>/dev/null || echo 000)"

  # Uptime: ps -o etime= gives elapsed time since the process started, e.g.
  # "3-12:34:56" or "12:34:56" or "45:67" (MM:SS).
  etime="$(ps -o etime= -p "$PID" 2>/dev/null | tr -d ' ' || echo "?")"
  rss_kb="$(ps -o rss= -p "$PID" 2>/dev/null | tr -d ' ' || echo 0)"
  rss_mb="$(( rss_kb / 1024 ))M"

  echo "⚓ Captain's Bridge: RUNNING"
  echo "   pid:     $PID"
  echo "   port:    ${HOST}:${PORT}"
  echo "   uptime:  $etime"
  echo "   rss:     $rss_mb"
  if [[ "$code" == "200" ]]; then
    echo "   http:    200 OK"
  else
    echo "   http:    $code (NOT RESPONDING)"
  fi
  echo "   log:     $LOGFILE"
  echo "   ──"
  if [[ -f "$LOGFILE" ]]; then
    tail -n 20 "$LOGFILE" | sed 's/^/   /'
  else
    echo "   (no log file yet)"
  fi

  # Non-zero exit if process is up but port is dead — caller can treat as
  # "needs restart" without parsing human output.
  if [[ "$code" != "200" ]]; then
    exit 2
  fi
  exit 0
fi

# Dead.
echo "✗ Captain's Bridge: DEAD"
if [[ -f "$LOGFILE" ]]; then
  last="$(tail -n 1 "$LOGFILE" 2>/dev/null || true)"
  if [[ -n "$last" ]]; then
    echo "   last log: $last"
  else
    echo "   last log: (empty)"
  fi
else
  echo "   last log: (no log file at $LOGFILE)"
fi
if [[ -n "$PID" ]]; then
  echo "   pid file: $PIDFILE (stale, last pid $PID)"
else
  echo "   pid file: (none)"
fi
exit 1
