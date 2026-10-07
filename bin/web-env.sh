#!/bin/bash
# Shared environment loader for the Captain's Bridge webapp.
#
# Sourced by bin/web-start.sh and bin/web-watch.sh.
#
# Why not `set -a; source ~/.hermes/.env`:
# ~/.hermes/.env has a line whose value contains an unescaped space
# (OBSIDIAN_VAULT_PATH), which is not valid shell. `source` fails with
# "No such file or directory" and, combined with `set -e`, kills the server
# before uvicorn ever boots. So parse KEY=VALUE line by line instead of
# sourcing, and skip malformed lines instead of dying on them.
#
# Exports:
#   WEB_ROOT     repo root (not bin/)
#   PYBIN        absolute path to the project venv python
#   HOST, PORT   bind address for the webapp
#   LARS_USER    HTTP Basic user
#   LARS_PASS    HTTP Basic password

set -euo pipefail

# When sourced via `bash -s` (stdin), ${BASH_SOURCE[0]} is empty/unset. When
# sourced normally, $0 is bash and BASH_SOURCE points at this file. Either
# way, $0 here is the calling process name — which we do NOT want, because
# for `bash < web-env.sh` it is just "bash" and yields the wrong WEB_ROOT.
# Therefore callers MUST pre-set WEB_ROOT in this stdin case; the launchd
# shim does so. Otherwise derive from BASH_SOURCE[0].
if [[ -z "${WEB_ROOT:-}" ]]; then
  src="${BASH_SOURCE[0]:-}"
  if [[ -n "$src" && "$src" != "bash" && "$src" != "sh" && "$src" != "-" ]]; then
    WEB_ROOT="$(cd "$(dirname "$src")/.." && pwd)"
  else
    echo "web-env.sh: WEB_ROOT not set and BASH_SOURCE empty — caller must export WEB_ROOT" >&2
    return 1 2>/dev/null || exit 1
  fi
fi
export WEB_ROOT

# Strip Python env that may have leaked in from the parent shell (e.g. an
# AI agent process whose PYTHONPATH points at a different venv's
# site-packages). Without this, the project's venv python imports
# pydantic_core from whatever PYTHONPATH points at first, which may be a
# different ABI and crash on import (`ModuleNotFoundError: No module named
# 'pydantic_core._pydantic_core'`).
#
# The project's venv python resolves site-packages relative to itself, so
# an empty PYTHONPATH is correct (matches the venv's own default). We
# therefore drop *all* entries — better to lose an exotic caller-supplied
# path than to risk a silent cross-ABI import. Add explicit allowlist paths
# here if a real caller's PYTHONPATH ever needs to survive.
unset PYTHONHOME
if [[ -n "${PYTHONPATH:-}" ]]; then
  unset PYTHONPATH
fi

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8742}"
PYBIN="${PYBIN:-$WEB_ROOT/.venv/bin/python}"
export HOST PORT PYBIN

ENV_FILE="${ENV_FILE:-$HOME/.hermes/.env}"

# Regexes kept in variables: they must be UNQUOTED at [[ =~ ]] time, which is
# where inline bash quoting rules get treacherous.
re_comment='^[[:space:]]*(#|$)'
re_dquoted='^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)="([^"]*)"[[:space:]]*$'
re_squoted="^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)='([^']*)'[[:space:]]*\$"
re_bare='^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=([^[:space:]]+)[[:space:]]*$'
# Fast bare: KEY=VALUE, no whitespace and no quotes inside the value.
# Combined into one pass so the slow regex ladder only runs for oddities.
re_fast='^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=([^[:space:]"'\'']+)$'

# Load the env file as DATA, not as shell. Malformed lines are skipped.
# Fast path: most lines are `KEY=VALUE` with no whitespace, no quotes, no
# spaces. We try the regex matches only when the cheap tests fail.
if [[ -f "$ENV_FILE" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    # Skip blanks and comments without ever invoking the regex engine.
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue

    # Fast bare path: KEY=VALUE with no whitespace and no quotes.
    if [[ "$line" =~ $re_fast ]]; then
      export "${BASH_REMATCH[2]}=${BASH_REMATCH[3]}"
      continue
    fi

    # Fall back to the full regex ladder for quoted or unusual values.
    if [[ "$line" =~ $re_dquoted ]]; then
      export "${BASH_REMATCH[2]}=${BASH_REMATCH[3]}"
    elif [[ "$line" =~ $re_squoted ]]; then
      export "${BASH_REMATCH[2]}=${BASH_REMATCH[3]}"
    elif [[ "$line" =~ $re_bare ]]; then
      export "${BASH_REMATCH[2]}=${BASH_REMATCH[3]}"
    fi
    # Anything else (e.g. unquoted value containing a space) is skipped.
  done < "$ENV_FILE"
fi

export LARS_USER="${LARS_USER:-lars}"
export LARS_PASS="${LARS_PASS:-captain}"

mkdir -p "$WEB_ROOT/logs"