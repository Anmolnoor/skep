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

# The container runs as root, and bwrap run as root creates its namespaces
# directly instead of through a user namespace: that needs CAP_SYS_ADMIN (and
# CAP_NET_ADMIN for --unshare-net's loopback), which Docker drops, and Docker's
# default seccomp/AppArmor profiles refuse the mounts. Since ES-F1 skep fails
# closed on that instead of running the worker unconfined, so this one
# throwaway container gets exactly those, short of --privileged.
docker run --rm --security-opt seccomp=unconfined --security-opt apparmor=unconfined \
  --cap-add SYS_ADMIN --cap-add NET_ADMIN \
  --entrypoint bash "$IMAGE" -lc \
  "cd /opt/skep && scripts/linux-sandbox-smoke.sh"
