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

# bwrap needs to create user namespaces, which Docker's default seccomp and
# AppArmor profiles refuse inside a container. Since ES-F1 skep fails closed
# on that instead of running the worker unconfined, so the smoke lifts both
# profiles for this one throwaway container.
docker run --rm --security-opt seccomp=unconfined --security-opt apparmor=unconfined \
  --entrypoint bash "$IMAGE" -lc \
  "cd /opt/skep && scripts/linux-sandbox-smoke.sh"
