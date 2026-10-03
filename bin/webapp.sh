#!/bin/bash
# Start the Job Approval UI (Captain's Bridge dashboard).
#
# Usage:
#   bin/webapp.sh          # start on default port 8742
#   PORT=9000 bin/webapp.sh  # start on custom port
#
# Auth: HTTP Basic (user=lars, pass=captain). Override via LARS_USER / LARS_PASS env vars.

set -e
cd "$(dirname "$0")/.."

PORT="${PORT:-8742}"
HOST="${HOST:-127.0.0.1}"
PYBIN=".venv/bin/python"

# Load env if present
if [[ -f "$HOME/.hermes/.env" ]]; then
  set -a; source "$HOME/.hermes/.env"; set +a
fi
export LARS_USER="${LARS_USER:-lars}"
export LARS_PASS="${LARS_PASS:-captain}"

echo "⚓ Starting Captain's Bridge on http://${HOST}:${PORT}"
echo "   user: $LARS_USER"
echo "   data: data/jobs.db (real SQLite pipeline state)"
echo "   PDFs: data/batches/*/CV_*.pdf, CL_*.pdf"
echo
exec "$PYBIN" -m uvicorn src.web.app:app \
  --host "$HOST" --port "$PORT" \
  --no-access-log