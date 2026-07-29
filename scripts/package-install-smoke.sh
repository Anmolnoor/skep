#!/usr/bin/env bash
# Release gate: install the built wheel into a disposable venv and prove the
# packaged console script starts. Run `uv build` first.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UV_CACHE_DIR="${UV_CACHE_DIR:-$ROOT/.uv-cache}"
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/skep-package-smoke.XXXXXX")"
VENV_DIR="$WORK_DIR/venv"

cleanup() {
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

wheels=("$ROOT"/dist/skep-*-py3-none-any.whl)
if [[ ! -f "${wheels[0]}" ]]; then
  echo "No built wheel found under dist/. Run: UV_CACHE_DIR=.uv-cache uv build" >&2
  exit 2
fi

WHEEL="${wheels[0]}"
echo "wheel: $WHEEL"

UV_CACHE_DIR="$UV_CACHE_DIR" uv venv "$VENV_DIR" --python "$PYTHON_VERSION"
UV_CACHE_DIR="$UV_CACHE_DIR" uv pip install --python "$VENV_DIR/bin/python" "$WHEEL"

echo "skep --version"
VERSION_OUTPUT="$("$VENV_DIR/bin/skep" --version)"
echo "$VERSION_OUTPUT"

case "$VERSION_OUTPUT" in
  skep\ *"worker contract"*) ;;
  *)
    echo "unexpected skep --version output: $VERSION_OUTPUT" >&2
    exit 1
    ;;
esac

"$VENV_DIR/bin/python" -c "import skep; print(skep.__version__)"
echo "PACKAGE INSTALL SMOKE PASS"
