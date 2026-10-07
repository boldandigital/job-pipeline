#!/usr/bin/env bash
# ╔═══════════════════════════════════════════════════════════════════════╗
# ║  bin/captain-uninstall.sh — remove the captain launchd agent        ║
# ║                                                                     ║
# ║  Counterpart to captain-install.sh. Idempotent — safe to run if     ║
# ║  the agent isn't installed (exits 0 with a notice).                ║
# ║                                                                     ║
# ║  Usage:                                                              ║
# ║    bash bin/captain-uninstall.sh            # remove                 ║
# ║    bash bin/captain-uninstall.sh --dry-run  # print actions, no-op  ║
# ║    bash bin/captain-uninstall.sh --purge    # also delete the plist ║
# ╚═══════════════════════════════════════════════════════════════════════╝

set -euo pipefail

LABEL="com.captainapply.daily"
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"
DRY_RUN=0
PURGE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --purge)   PURGE=1;   shift ;;
    -h|--help)
      sed -n '2,12p' "$0"; exit 0 ;;
    *)
      echo "unknown flag: $1 (try --help)" >&2; exit 2 ;;
  esac
done

if ! launchctl list | grep -q "$LABEL"; then
  echo "$LABEL is not currently loaded (nothing to do)"
  if (( PURGE == 1 )); then
    if (( DRY_RUN == 1 )); then
      if [[ -f "$PLIST_PATH" ]]; then
        echo "── DRY RUN — would delete $PLIST_PATH ──"
      else
        echo "── DRY RUN — would delete (plist $PLIST_PATH not present) ──"
      fi
    else
      if [[ -f "$PLIST_PATH" ]]; then
        rm -f "$PLIST_PATH"
        echo "deleted $PLIST_PATH"
      else
        echo "(no plist to delete at $PLIST_PATH)"
      fi
    fi
  fi
  exit 0
fi

if (( DRY_RUN == 1 )); then
  echo "── DRY RUN — would: launchctl unload $PLIST_PATH ──"
  if (( PURGE == 1 )); then
    echo "── DRY RUN — would: rm $PLIST_PATH ──"
  fi
  exit 0
fi

echo "unloading $LABEL"
launchctl unload "$PLIST_PATH" 2>/dev/null || true

if (( PURGE == 1 )); then
  rm -f "$PLIST_PATH"
  echo "deleted $PLIST_PATH"
fi

if launchctl list | grep -q "$LABEL"; then
  echo "✗ $LABEL still listed — check launchd logs" >&2
  exit 1
fi

echo "✓ $LABEL removed"
