#!/usr/bin/env bash
# Release gate: build the Skep Docker image, then run the real Linux bubblewrap
# sandbox smoke inside that Linux image. This lets a macOS maintainer exercise
# the Linux-only sandbox gate before relying on Ubuntu CI.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${SKEP_DOCKER_IMAGE:-skep:linux-sandbox-smoke}"
BUILD="${SKEP_DOCKER_BUILD:-1}"

if [[ "$BUILD" != "0" ]]; then
  docker build -f "$ROOT/Dockerfile" -t "$IMAGE" "$ROOT"
fi

docker run --rm --entrypoint bash "$IMAGE" -lc \
  "cd /opt/skep && scripts/linux-sandbox-smoke.sh"
