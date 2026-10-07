#!/usr/bin/env bash
# ╔═══════════════════════════════════════════════════════════════════════╗
# ║  bin/captain-install.sh — register bin/captain.sh with launchd       ║
# ║                                                                     ║
# ║  Installs a launchd agent that runs `captain.sh once` every day at  ║
# ║  09:00 Europe/Brussels (UTC+01 in winter, UTC+02 in summer). The    ║
# ║  launchd calendar interval handles DST automatically.              ║
# ║                                                                     ║
# ║  Usage:                                                              ║
# ║    bash bin/captain-install.sh           # install (default 09:00)  ║
# ║    bash bin/captain-install.sh --dry-run  # print plist, don't write║
# ║    bash bin/captain-install.sh --hour 8  # 08:00 Brussels instead   ║
# ║                                                                     ║
# ║  Per memory: per-action GO approval. This script *writes and loads* ║
# ║  the plist — that is an install action, not just a write. Default  ║
# ║  behavior is to install. If you want to inspect the plist first,  ║
# ║  pass --dry-run.                                                    ║
# ║                                                                     ║
# ║  No-conflict guarantee:                                              ║
# ║    - coexists with cron a2cb1c5e982a (23:00 Brussels) for the       ║
# ║      trading bot — different label, different time                  ║
# ║    - coexists with com.boldandigital.jobpipeline.web (KeepAlive)    ║
# ║      — different label, different role (web vs. captain)           ║
# ╚═══════════════════════════════════════════════════════════════════════╝

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CAPTAIN_BIN="$PROJECT_DIR/bin/captain.sh"

LABEL="com.captainapply.daily"
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"
HOUR=9
MINUTE=0
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --hour)    HOUR="${2:-9}"; shift 2 ;;
    --minute)  MINUTE="${2:-0}"; shift 2 ;;
    -h|--help)
      sed -n '2,28p' "$0"; exit 0 ;;
    *)
      echo "unknown flag: $1 (try --help)" >&2; exit 2 ;;
  esac
done

# Validate inputs
if ! [[ "$HOUR" =~ ^[0-9]+$ ]] || (( HOUR < 0 || HOUR > 23 )); then
  echo "bad --hour: $HOUR (must be 0..23)" >&2; exit 2
fi
if ! [[ "$MINUTE" =~ ^[0-9]+$ ]] || (( MINUTE < 0 || MINUTE > 59 )); then
  echo "bad --minute: $MINUTE (must be 0..59)" >&2; exit 2
fi

if [[ ! -x "$CAPTAIN_BIN" ]]; then
  echo "missing executable: $CAPTAIN_BIN" >&2
  echo "run: chmod +x $CAPTAIN_BIN" >&2
  exit 3
fi

mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$PROJECT_DIR/logs"

# Build plist. StartCalendarInterval handles DST automatically (launchd
# uses local time, so the 09:00 fire-time stays 09:00 year-round).
# Using $(cat <<EOF) avoids the EOF exit-code problem that read -d has
# under set -e.
PLIST_XML="$(cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${CAPTAIN_BIN}</string>
        <string>once</string>
    </array>

    <key>WorkingDirectory</key>
    <string>${PROJECT_DIR}</string>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>${HOUR}</integer>
        <key>Minute</key>
        <integer>${MINUTE}</integer>
    </dict>

    <!-- Do not run again if a previous instance is still going -->
    <key>RunAtLoad</key>
    <false/>

    <key>StandardOutPath</key>
    <string>${PROJECT_DIR}/logs/captain-launchd.log</string>
    <key>StandardErrorPath</key>
    <string>${PROJECT_DIR}/logs/captain-launchd.log</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key>
        <string>/Users/lars</string>
        <key>LANG</key>
        <string>en_US.UTF-8</string>
    </dict>

    <key>ProcessType</key>
    <string>Standard</string>
</dict>
</plist>
EOF
)"

if (( DRY_RUN == 1 )); then
  echo "── DRY RUN — would write: $PLIST_PATH ──"
  echo "$PLIST_XML"
  echo "── DRY RUN — would: launchctl unload $PLIST_PATH (if present) ──"
  echo "── DRY RUN — would: launchctl load $PLIST_PATH ──"
  echo "── DRY RUN — would: launchctl list | grep captainapply ──"
  exit 0
fi

# If a previous version is loaded, unload it first so the new one takes
# effect cleanly. launchctl unload returns non-zero if not loaded; ignore.
if launchctl list | grep -q "$LABEL"; then
  echo "unloading existing $LABEL"
  launchctl unload "$PLIST_PATH" 2>/dev/null || true
fi

echo "writing $PLIST_PATH"
printf '%s\n' "$PLIST_XML" > "$PLIST_PATH"

echo "loading via launchctl"
launchctl load "$PLIST_PATH"

# Verify
if launchctl list | grep -q "$LABEL"; then
  echo "✓ $LABEL is loaded"
  echo
  echo "Verify with:"
  echo "  launchctl list | grep captainapply"
  echo
  echo "Next fire: today/tomorrow at $(printf "%02d:%02d" "$HOUR" "$MINUTE") (local)"
  echo "Logs:      $PROJECT_DIR/logs/captain-launchd.log"
  echo "Run now:   bash $CAPTAIN_BIN once"
else
  echo "✗ launchctl load did not register the agent" >&2
  echo "  check: launchctl error log (Console.app → launchd filter)" >&2
  exit 4
fi
