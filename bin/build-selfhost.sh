#!/usr/bin/env bash
# Build the CaptainApply self-host Docker image and verify it boots.
#
# Usage:
#   bin/build-selfhost.sh           # tag = today's date (YYYYMMDD)
#   bin/build-selfhost.sh v1.5.0    # explicit tag
#
# Produces:
#   - Image captainapply/selfhost:<tag>
#   - Boots the image, hits /api/health, prints image ID + size + test result

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

# Tag = date (UTC) by default so each day is a fresh layer
TAG="${1:-$(date -u +%Y%m%d)}"
IMAGE="captainapply/selfhost:${TAG}"
LATEST="captainapply/selfhost:latest"

if ! command -v docker >/dev/null 2>&1; then
    echo "✗ docker not found on PATH — install Docker Desktop or docker-ce first." >&2
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    echo "✗ docker daemon not reachable — is Docker Desktop running?" >&2
    exit 1
fi

# Use a dockerignore that doesn't conflict with the project's other files.
# We swap the project's .dockerignore (if any) for our selfhost one during the
# build, then restore it. --ignore-file isn't supported by `docker buildx`,
# which is the default on modern Docker Desktop.
SWAP_FILE="$HERE/.dockerignore"
BACKUP_FILE=""
if [[ -f "$SWAP_FILE" && ! -f "$HERE/.dockerignore.selfhost" ]]; then
    echo "✗ missing $HERE/.dockerignore.selfhost" >&2
    exit 1
fi
if [[ -f "$SWAP_FILE" ]]; then
    BACKUP_FILE="$(mktemp -t dockerignore.bak.XXXXXX)"
    cp "$SWAP_FILE" "$BACKUP_FILE"
fi
cp "$HERE/.dockerignore.selfhost" "$SWAP_FILE"

echo "▶ Building $IMAGE (tag=$TAG)"
START=$(date +%s)

docker build \
    --progress=plain \
    -f Dockerfile.selfhost \
    -t "$IMAGE" \
    -t "$LATEST" \
    "$HERE" 2>&1 | tail -40

BUILD_EXIT=${PIPESTATUS[0]}
END=$(date +%s)
ELAPSED=$((END - START))

if [[ $BUILD_EXIT -ne 0 ]]; then
    echo "✗ docker build failed (exit=$BUILD_EXIT) after ${ELAPSED}s" >&2
    exit "$BUILD_EXIT"
fi

# Capture image ID + size
IMAGE_ID=$(docker images --no-trunc -q "$IMAGE" | head -n1)
SIZE_BYTES=$(docker image inspect "$IMAGE" --format='{{.Size}}' | head -n1 | tr -d '\n')
SIZE_MB=$(awk -v b="$SIZE_BYTES" 'BEGIN{printf "%.1f", b/1024/1024}')

# Compressed size (what you'd actually push / pull)
# .VirtualSize is deprecated in Docker Desktop and may be empty/missing;
# fall back to on-disk size if so. `|| true` is needed because docker
# inspect exits non-zero when the field is absent under `set -e + pipefail`.
COMPRESSED_BYTES=$(docker image inspect "$IMAGE" --format='{{.VirtualSize}}' 2>/dev/null | head -n1 | tr -d '\n' || true)
COMPRESSED_BYTES="${COMPRESSED_BYTES:-}"
if [[ -z "$COMPRESSED_BYTES" || "$COMPRESSED_BYTES" == "0" ]]; then
    COMPRESSED_BYTES="$SIZE_BYTES"
fi
COMPRESSED_MB=$(awk -v b="$COMPRESSED_BYTES" 'BEGIN{printf "%.1f", b/1024/1024}')

echo
echo "▶ Built $IMAGE in ${ELAPSED}s"
echo "  image id : $IMAGE_ID"
echo "  on-disk  : ${SIZE_MB} MB"
echo "  (virtual): ${COMPRESSED_MB} MB"

# Sanity check: size budget
SIZE_LIMIT_MB=500
if awk -v s="$COMPRESSED_MB" -v l="$SIZE_LIMIT_MB" 'BEGIN{exit !(s>l)}'; then
    echo "⚠ image ${COMPRESSED_MB}MB exceeds ${SIZE_LIMIT_MB}MB target" >&2
fi

# ---------------------------------------------------------------------------
# Boot test
# ---------------------------------------------------------------------------

TEST_CONTAINER="captainapply-selfhost-boot-${TAG}-$$"
TEST_PORT=28742
LOG_FILE="$(mktemp -t captainapply-boot.XXXXXX.log)"

cleanup() {
    docker rm -f "$TEST_CONTAINER" >/dev/null 2>&1 || true
    [[ -n "${LOG_FILE:-}" ]] && rm -f "$LOG_FILE" || true
    if [[ -n "${BACKUP_FILE:-}" ]]; then
        cp "$BACKUP_FILE" "$SWAP_FILE"
        rm -f "$BACKUP_FILE"
    else
        rm -f "$SWAP_FILE"
    fi
}
trap cleanup EXIT

echo
echo "▶ Booting container for health-check (port ${TEST_PORT})"
docker run -d \
    --name "$TEST_CONTAINER" \
    -p "${TEST_PORT}:8742" \
    -e CAPTAIN_PROD=1 \
    -e LARS_USER="${LARS_USER:-lars}" \
    -e LARS_PASS="${LARS_PASS:-captain}" \
    "$IMAGE" >/dev/null

HEALTH_OK=0
for i in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${TEST_PORT}/api/health" >/dev/null 2>&1; then
        HEALTH_OK=1
        break
    fi
    sleep 1
done

if [[ $HEALTH_OK -eq 1 ]]; then
    HEALTH_BODY=$(curl -fsS "http://127.0.0.1:${TEST_PORT}/api/health")
    echo "  ✓ /api/health → $HEALTH_BODY"
else
    echo "  ✗ /api/health did not return 200 within 30s" >&2
    echo "  --- container log tail ---" >&2
    docker logs --tail 40 "$TEST_CONTAINER" >&2 || true
    echo "  --- end log ---" >&2
    exit 1
fi

# Auth round-trip
LOGIN_BODY=$(curl -fsS -X POST \
    -H 'Content-Type: application/json' \
    -d "{\"user_id\":\"${LARS_USER:-lars}\",\"password\":\"${LARS_PASS:-captain}\"}" \
    "http://127.0.0.1:${TEST_PORT}/api/v1/auth/login" 2>/dev/null || true)
if [[ -n "$LOGIN_BODY" ]] && echo "$LOGIN_BODY" | grep -q '"ok":true'; then
    echo "  ✓ /api/v1/auth/login → ok"
else
    echo "  ✗ /api/v1/auth/login failed: $LOGIN_BODY" >&2
    exit 1
fi

# Stop the boot-test container (the trap will rm it)
echo
echo "▶ Tearing down boot-test container"
docker stop "$TEST_CONTAINER" >/dev/null 2>&1 || true

echo
echo "============================================================"
echo "  Image:    $IMAGE"
echo "  ID:       $IMAGE_ID"
echo "  Size:     ${COMPRESSED_MB} MB (virtual)"
echo "  Build:    ${ELAPSED}s"
echo "  Health:   PASS"
echo "  Login:    PASS"
echo "  Tests:    PASS (boot + health + login)"
echo "============================================================"
