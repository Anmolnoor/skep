#!/usr/bin/env bash
# Release gate: run the non-account-bound launch checks from this checkout.
# Live Claude Code, PyPI publish/install-from-PyPI, GitHub release, and hosted
# landing-page checks remain explicit external gates in docs/release-checklist.md.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$ROOT/.uv-cache}"
cd "$ROOT"

run() {
  printf '+'
  printf ' %q' "$@"
  printf '\n'
  "$@"
}

run make all
run make smoke
run ./scripts/reliability.sh
if [[ "$(uname -s)" == "Linux" ]] && command -v bwrap >/dev/null 2>&1; then
  run ./scripts/linux-sandbox-smoke.sh
else
  echo "+ ./scripts/linux-sandbox-smoke.sh # skipped: requires Linux with bubblewrap"
fi
run ./scripts/release-hygiene-scan.sh
run make docs-link-smoke
run uv build
run uvx twine check dist/*
run ./scripts/package-install-smoke.sh
run env SKEP_DOCKER_IMAGE="${SKEP_DOCKER_IMAGE:-skep:release-local}" \
  ./scripts/docker-image-smoke.sh
run env SKEP_DOCKER_IMAGE="${SKEP_LINUX_SANDBOX_DOCKER_IMAGE:-skep:linux-sandbox-smoke}" \
  ./scripts/linux-sandbox-docker-smoke.sh

echo "LOCAL RELEASE GATES PASS"
