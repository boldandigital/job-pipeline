#!/bin/bash
# Watchdog for the Captain's Bridge webapp.
#
# Two jobs:
#   1. `run`       — supervise uvicorn in the foreground: restart it the
#                    moment it dies (crash, kill -9, OOM), and kill+restart
#                    it if /api/health stops answering (hung process).
#                    This is what launchd executes.
#   2. `install`   — register a launchd LaunchAgent with KeepAlive=true so
#                    the webapp also survives logout and reboot.
#
# Usage:
#   bin/web-watch.sh install     # load + start (persists across reboots)
#   bin/web-watch.sh uninstall   # stop and unregister
#   bin/web-watch.sh status      # is it up? who owns the port?
#   bin/web-watch.sh logs        # tail logs/web.log
#   bin/web-watch.sh run         # supervise in foreground (launchd calls this)
#
# Acceptance: after `kill -9 <uvicorn pid>` the webapp answers /api/health
# again within ~5 seconds.

set -uo pipefail
# BASH_SOURCE[0] is empty when the script is read from stdin (e.g. the
# `bash -s -- "$@" < web-watch.sh` shim that launchd uses for scripts on
# external volumes). In that case, $0 is `/bin/bash` — useless for locating
# the script. The shim sets WEB_ROOT in the env, so we can fall back to
# `$WEB_ROOT/bin/web-env.sh`. If neither is available, we exit with a
# clear error rather than sourcing the wrong file.
_SELF="${BASH_SOURCE[0]:-}"
if [[ -z "$_SELF" || "$_SELF" == "/bin/bash" || "$_SELF" == "bash" ]]; then
  if [[ -n "${WEB_ROOT:-}" && -f "$WEB_ROOT/bin/web-env.sh" ]]; then
    # shellcheck disable=SC1091
    source "$WEB_ROOT/bin/web-env.sh"
  else
    echo "✗ web-watch.sh: cannot locate web-env.sh (BASH_SOURCE empty and WEB_ROOT unset)" >&2
    echo "   either run me from the repo (./bin/web-watch.sh …) or set WEB_ROOT before invoking." >&2
    exit 78
  fi
else
  # shellcheck disable=SC1091
  source "$(cd "$(dirname "$_SELF")" && pwd)/web-env.sh"
fi
unset _SELF

LABEL="${WEB_LABEL:-com.boldandigital.jobpipeline.web}"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
SHIM="/Users/lars/.local/bin/${LABEL}.sh"
# Internal-disk mirror of bin/web-watch.sh + bin/web-env.sh. launchd is a
# Background process and macOS Background TCC blocks it from /Volumes
# reads and writes, so it can't read the project scripts directly. A copy
# on internal disk keeps everything launchd needs on a path it can touch.
SHIM_CWD="/Users/lars/.local/share/${LABEL}"
# launchd's xpcproxy runs as a Background process and macOS TCC forbids it
# from OPENING FILES on external volumes ("error 0x1 - Operation not
# permitted"). So the launchd-owned stream goes to internal disk, and the
# supervisor tail-merges it into the project log so `bin/web-watch.sh logs`
# still works.
LOG_LAUNCHD="$HOME/Library/Logs/${LABEL}.log"
LOG="$WEB_ROOT/logs/web.log"
HEALTH_URL="http://${HOST}:${PORT}/api/health"

# Poll cadence for the hang detector. Fast enough to keep downtime tiny,
# slow enough not to matter. Health checks are also how we find a wedged
# process, which KeepAlive alone can never catch.
HEALTH_INTERVAL="${HEALTH_INTERVAL:-10}"
HEALTH_GRACE="${HEALTH_GRACE:-30}"   # seconds after boot before judging health
HEALTH_FAILS="${HEALTH_FAILS:-3}"    # consecutive failures => hung => restart

log() { printf '%s [web-watch] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

port_busy() { lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; }

healthy() { curl -fsS -m 5 -o /dev/null "$HEALTH_URL" 2>/dev/null; }

# ---------------------------------------------------------------------------
# run — supervise uvicorn
# ---------------------------------------------------------------------------
cmd_run() {
  cd "$WEB_ROOT" || { log "FATAL: cannot cd to $WEB_ROOT"; exit 1; }

  if [[ ! -x "$PYBIN" ]]; then
    log "FATAL: venv python missing at $PYBIN"
    exit 1
  fi

  child=""
  shutting_down=0

  # launchd sends SIGTERM on bootout/uninstall. Don't restart in that case —
  # take the child down and exit cleanly, or uninstall never completes.
  on_term() {
    shutting_down=1
    log "shutdown signal received — stopping uvicorn"
    [[ -n "$child" ]] && kill -TERM "$child" 2>/dev/null
  }
  trap on_term TERM INT

  backoff=1
  # Internal-disk pid file (~/Library/.../web.pid) so `web-status.sh` and
  # `web-stop.sh` can find the process even when the launchd-shim path
  # blocks writes to the external volume (macOS TCC). Mirror to the
  # project log dir too when writable, so users who don't go through
  # launchd still see the file.
  INTERNAL_PIDFILE="$HOME/Library/Application Support/CaptainApply/web.pid"
  INTERNAL_PIDDIR="$(dirname "$INTERNAL_PIDFILE")"
  mkdir -p "$INTERNAL_PIDDIR"
  while true; do
    log "starting uvicorn on ${HOST}:${PORT} (pid will follow)"
    "$PYBIN" -m uvicorn src.web.app:app \
      --host "$HOST" --port "$PORT" \
      --no-access-log &
    child=$!
    log "uvicorn started, pid=$child"
    # Best-effort pid write — both internal and project paths. The project
    # write may be blocked by macOS TCC when the supervisor runs under
    # launchd (Background class). The error is non-fatal: the internal
    # pidfile is the source of truth.
    { echo "$child" > "$INTERNAL_PIDFILE"; } 2>/dev/null || true
    { echo "$child" > "$WEB_ROOT/logs/web.pid"; } 2>/dev/null || true
    backoff=1

    # --- hang detector -----------------------------------------------------
    # Runs alongside uvicorn. If the port stops answering N times in a row
    # while uvicorn is still alive, the process is wedged: SIGKILL it so the
    # main loop below respawns it.
    (
      sleep "$HEALTH_GRACE"
      fails=0
      while kill -0 "$child" 2>/dev/null; do
        sleep "$HEALTH_INTERVAL"
        kill -0 "$child" 2>/dev/null || break
        if healthy; then
          fails=0
        else
          fails=$((fails + 1))
          echo "[$(date '+%H:%M:%S')] [web-watch] health check failed ($fails/$HEALTH_FAILS) at $HEALTH_URL"
          if (( fails >= HEALTH_FAILS )); then
            echo "[$(date '+%H:%M:%S')] [web-watch] unhealthy -> SIGKILL pid $child (wedged)"
            kill -9 "$child" 2>/dev/null
            break
          fi
        fi
      done
    ) &
    monitor=$!

    wait "$child"
    rc=$?
    kill "$monitor" 2>/dev/null
    wait "$monitor" 2>/dev/null
    child=""
    # Clear the pid file so a status check before respawn doesn't report
    # the just-dead pid. The new pid is written in the loop above.
    rm -f "$INTERNAL_PIDFILE" "$WEB_ROOT/logs/web.pid" 2>/dev/null || true

    if (( shutting_down )); then
      log "stopped cleanly"
      exit 0
    fi

    log "uvicorn exited with code $rc — restarting"

    # Don't spin hot if it dies instantly every time (e.g. bad config):
    # escalate the delay, capped at 30s.
    if (( rc != 0 )); then
      sleep "$backoff"
      backoff=$(( backoff * 2 )); (( backoff > 30 )) && backoff=30
    fi
  done
}

# ---------------------------------------------------------------------------
# install / uninstall / status / logs
# ---------------------------------------------------------------------------
cmd_install() {
  mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs" \
           "$(dirname "$SHIM")" "$SHIM_CWD" "$WEB_ROOT/logs"
  : > "$LOG_LAUNCHD"  # truncate the launchd-side log file

  # --- Internal-disk shim -------------------------------------------------
  # launchd CANNOT exec a script that lives on an external volume: the kernel
  # refuses it and bash reports "Operation not permitted" (exit 126), so every
  # boot attempt dies instantly with EX_CONFIG (78). launchd ALSO cannot read
  # project scripts (same TCC block on file open). The fix: this tiny shim
  # lives on the internal disk and is what launchd execs. The shim itself
  # execs the mirrored copy of web-watch.sh that we sync to $SHIM_CWD at
  # install time. The mirrored script sets WEB_ROOT (via web-env.sh) and
  # cd's back to the project for the actual uvicorn run.
  cat > "$SHIM" <<SHIM_EOF
#!/bin/bash
# Generated by job-pipeline bin/web-watch.sh — do not edit.
exec /bin/bash "${SHIM_CWD}/web-watch.sh" "\$@"
SHIM_EOF
  chmod +x "$SHIM"

  # Sync the script + env to internal disk so launchd can read them.
  # launchd CANNOT read from /Volumes: every attempt returns
  # "Operation not permitted". By keeping an internal-disk mirror we avoid
  # that block entirely. Mirroring is one-way (we never write back).
  #
  # The mirror is NOT a true copy of the project scripts — we have to
  # hardcode WEB_ROOT into the env loader so its BASH_SOURCE[0] fallback
  # resolves to the project, not the mirror directory. Otherwise the
  # venv python path is computed as $SHIM_CWD/.venv/bin/python.
  #
  # Strip the last line of the original env loader (it does
  # `mkdir -p "$WEB_ROOT/logs"` which is fine to keep, but we also want
  # to append the WEB_ROOT override and the PYBIN/HOST/PORT re-exports
  # AFTER the loader's own assignments).
  cat > "$SHIM_CWD/web-env.sh" <<ENV_EOF
$(cat "$WEB_ROOT/bin/web-env.sh")

# ---- MIRROR overrides (appended by bin/web-watch.sh install) ------------
# The mirror lives outside the project, so BASH_SOURCE points at the
# mirror dir. Force WEB_ROOT back to the project so PYBIN, LOG, and the
# venv path all resolve correctly.
export WEB_ROOT='${WEB_ROOT}'

# Re-export the project-relative paths that the loader normally derives.
PYBIN="${WEB_ROOT}/.venv/bin/python"
HOST="\${HOST:-127.0.0.1}"
PORT="\${PORT:-8742}"
export PYBIN HOST PORT
ENV_EOF
  cat > "$SHIM_CWD/web-watch.sh" <<WATCH_EOF
$(cat "$WEB_ROOT/bin/web-watch.sh" | sed 's|^# Generated.*$|# MIRROR from $WEB_ROOT/bin/web-watch.sh (do not edit here)|')
WATCH_EOF

  # Make logs/web.log point at the launchd log on internal disk, so the
  # same path resolves in the user shell and in the launchd-managed
  # stream. First remove the prior log file (and the symlink pointing
  # at the prior internal path), then create a fresh symlink.
  rm -f "$WEB_ROOT/logs/web.log"
  ln -sfn "$LOG_LAUNCHD" "$WEB_ROOT/logs/web.log"

  # Use the ~/Documents/Projects path (a symlink to the SSD) — that's the
  # stable contract. If the SSD is offline at boot launchd retries, and the
  # webapp comes up on its own once the mount returns.
  cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${SHIM}</string>
        <string>run</string>
    </array>

    <key>WorkingDirectory</key>
    <string>${SHIM_CWD}</string>

    <!-- Restart on ANY exit: crash, kill -9, or clean shutdown. -->
    <key>KeepAlive</key>
    <true/>

    <!-- Restart again if it dies within 10s of starting (catches boot loops). -->
    <key>ThrottleInterval</key>
    <integer>10</integer>

    <key>RunAtLoad</key>
    <true/>

    <key>StandardOutPath</key>
    <string>${LOG_LAUNCHD}</string>
    <key>StandardErrorPath</key>
    <string>${LOG_LAUNCHD}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key>
        <string>${HOME}</string>
        <key>LANG</key>
        <string>en_US.UTF-8</string>
    </dict>

    <key>ProcessType</key>
    <string>Interactive</string>
</dict>
</plist>
PLIST_EOF

  # Modern bootstrap API; `load` is deprecated on current macOS.
  if launchctl bootout "gui/$UID/$LABEL" 2>/dev/null; then
    log "unloaded any previous $LABEL"
  fi
  launchctl bootstrap "gui/$UID" "$PLIST" || {
    log "FATAL: launchctl bootstrap failed"
    exit 1
  }

  log "installed $LABEL"
  sleep 3
  cmd_status
}

cmd_uninstall() {
  launchctl bootout "gui/$UID/$LABEL" 2>/dev/null && log "unloaded $LABEL" || log "not loaded"
  rm -f "$PLIST"
  log "removed $PLIST"
  rm -f "$SHIM"
  log "removed $SHIM"
  # Remove the project log symlink to the internal log only if it points
  # at one of our paths (don't clobber a real log the user kept around).
  if [[ -L "$WEB_ROOT/logs/web.log" ]]; then
    target=$(readlink "$WEB_ROOT/logs/web.log")
    if [[ "$target" == *"$LABEL"* ]]; then
      rm -f "$WEB_ROOT/logs/web.log"
      log "removed logs/web.log symlink"
    fi
  fi
  rm -rf "$SHIM_CWD"
  log "removed $SHIM_CWD"
  # Stop anything still holding the port. The `|| true` is REQUIRED here:
  # web-env.sh re-enables `set -e` in the parent scope when sourced, and
  # `pids=$(lsof …)` would inherit lsof's non-zero exit and abort the
  # whole script. We don't want that — the uninstall path is idempotent.
  pids=$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null) || true
  if [[ -n "${pids:-}" ]]; then
    log "stopping stray pids on $PORT: $pids"
    kill -9 $pids 2>/dev/null
  fi
}

cmd_status() {
  echo "── launchd ─────────────────────────────"
  if launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; then
    launchctl list | grep "$LABEL" || true
  else
    echo "$LABEL is NOT loaded"
  fi
  echo
  echo "── port $PORT ──────────────────────────"
  if port_busy; then
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN
  else
    echo "nothing listening on $PORT"
  fi
  echo
  echo "── health ──────────────────────────────"
  if healthy; then
    echo "OK  $(curl -fsS -m 5 "$HEALTH_URL")"
  else
    echo "FAIL  no answer from $HEALTH_URL"
  fi
  echo
  echo "── log tail ────────────────────────────"
  [[ -f "$LOG" ]] && tail -n 15 "$LOG" || echo "no $LOG yet"
}

case "${1:-status}" in
  run)       cmd_run ;;
  install)   cmd_install ;;
  uninstall) cmd_uninstall ;;
  status)    cmd_status ;;
  logs)      tail -f "$LOG" ;;
  restart)   cmd_uninstall; cmd_install ;;
  *)
    echo "usage: $0 {run|install|uninstall|restart|status|logs}" >&2
    exit 2
    ;;
esac