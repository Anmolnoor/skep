#!/usr/bin/env bash
# Release gate: build the Docker image and prove the server boots with token
# auth. Set SKEP_DOCKER_BUILD=0 to smoke an image that was already built.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${SKEP_DOCKER_IMAGE:-ghcr.io/anmolnoor/skep:dev}"
CONTAINER="${SKEP_DOCKER_CONTAINER:-skep-image-smoke}"
HOST="${SKEP_DOCKER_HOST:-127.0.0.1}"
PORT="${SKEP_DOCKER_PORT:-18765}"
BUILD="${SKEP_DOCKER_BUILD:-1}"
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/skep-docker-smoke.XXXXXX")"

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

if [[ "$BUILD" != "0" ]]; then
  docker build -f "$ROOT/Dockerfile" -t "$IMAGE" "$ROOT"
fi

docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
container_id="$(docker run -d --name "$CONTAINER" -p "$HOST:$PORT:8765" "$IMAGE")"
base_url="http://$HOST:$PORT"

ready=0
for _ in $(seq 1 30); do
  if curl -fsS "$base_url/" > "$WORK_DIR/index.html" 2>/dev/null; then
    ready=1
    break
  fi
  sleep 1
done

if [[ "$ready" != "1" ]]; then
  docker logs "$CONTAINER" >&2 || true
  echo "container did not serve $base_url/" >&2
  exit 1
fi

grep -qi skep "$WORK_DIR/index.html"

status_code="$(
  curl -s -o "$WORK_DIR/status-unauth.json" -w "%{http_code}" "$base_url/api/status"
)"
if [[ "$status_code" != "401" ]]; then
  cat "$WORK_DIR/status-unauth.json" >&2
  echo "expected unauthenticated /api/status to return 401, got $status_code" >&2
  exit 1
fi

container_logs="$(docker logs "$CONTAINER")"
grep -q "access token:" <<< "$container_logs"
token="$(docker exec "$CONTAINER" cat /data/skep/supervisor/serve-token)"
curl -fsS -H "X-Skep-Token: $token" "$base_url/api/status" > "$WORK_DIR/status-auth.json"
curl -fsS -H "X-Skep-Token: $token" "$base_url/api/runs" > "$WORK_DIR/runs-auth.json"

echo "container: $container_id"
echo "image: $IMAGE"
echo "unauthenticated /api/status: $status_code"
echo "authenticated /api/status: $(cat "$WORK_DIR/status-auth.json")"
echo "authenticated /api/runs: $(cat "$WORK_DIR/runs-auth.json")"
echo "DOCKER IMAGE SMOKE PASS"
