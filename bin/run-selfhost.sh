#!/usr/bin/env bash
# Run the CaptainApply self-host image locally for smoke-testing.
#
# Usage:
#   bin/run-selfhost.sh                  # default port 18742
#   bin/run-selfhost.sh 19000            # custom port
#   LARS_PASS=hunter2 bin/run-selfhost.sh
#
# Equivalent of:
#   docker run -d --name captainapply-test -p 18742:8742 \
#     -v captainapply-data:/app/data captainapply/selfhost:latest

set -euo pipefail

PORT="${1:-${PORT:-18742}}"
NAME="${NAME:-captainapply-test}"
VOLUME="${VOLUME:-captainapply-data}"
IMAGE="${IMAGE:-captainapply/selfhost:latest}"

if ! command -v docker >/dev/null 2>&1; then
    echo "✗ docker not found on PATH" >&2
    exit 1
fi

# Reuse an existing container if it's stopped, otherwise start a new one.
if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
    echo "▶ Reusing existing container '$NAME'"
    if docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
        echo "  already running — restarting"
        docker stop "$NAME" >/dev/null
    fi
    docker rm "$NAME" >/dev/null 2>&1 || true
fi

echo "▶ Starting $NAME on http://127.0.0.1:${PORT} (data volume: $VOLUME)"
docker run -d \
    --name "$NAME" \
    -p "${PORT}:8742" \
    -v "${VOLUME}:/app/data" \
    -e LARS_USER="${LARS_USER:-lars}" \
    -e LARS_PASS="${LARS_PASS:-captain}" \
    -e CAPTAIN_PROD="${CAPTAIN_PROD:-1}" \
    "$IMAGE" >/dev/null

# Wait for /api/health
HEALTH_OK=0
for i in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
        HEALTH_OK=1
        break
    fi
    sleep 1
done

if [[ $HEALTH_OK -ne 1 ]]; then
    echo "✗ /api/health did not return 200 within 30s" >&2
    echo "--- container log tail ---" >&2
    docker logs --tail 40 "$NAME" >&2 || true
    exit 1
fi

echo "✓ Self-host stack live on http://127.0.0.1:${PORT}"
echo "  user: ${LARS_USER:-lars}"
echo "  pass: (set via LARS_PASS env var, default: captain)"
echo "  stop with:  docker stop $NAME && docker rm $NAME"
