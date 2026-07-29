#!/usr/bin/env bash
# skep installer (v27-F3).
#
# The real one-line install once skep is on PyPI is just `uv tool install skep`
# (or `uvx skep` to try it without installing). This script's honest value-add:
# the source-checkout path, the Linux bubblewrap system-dependency nudge that
# no pip install can carry, and printing the first-run commands.
set -euo pipefail

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      echo "usage: install.sh [--dry-run]"
      exit 0
      ;;
    *)
      echo "skep install: unknown argument '$arg'" >&2
      exit 2
      ;;
  esac
done

# SKEP_INSTALL_OS exists so tests can exercise the refusal path.
OS="${SKEP_INSTALL_OS:-$(uname -s)}"
case "$OS" in
  Linux|Darwin) ;;
  *)
    echo "skep install: unsupported OS '$OS' — skep supports Linux and macOS" >&2
    exit 1
    ;;
esac

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "would run: $*"
  else
    "$@"
  fi
}

HERE="$(cd "$(dirname "$0")/.." && pwd)"
SKEP="skep"
if [ -f "$HERE/pyproject.toml" ] && [ -d "$HERE/src/skep" ]; then
  echo "source checkout detected: $HERE"
  if ! command -v uv >/dev/null 2>&1; then
    echo "skep install: the source path needs uv — https://docs.astral.sh/uv/" >&2
    exit 1
  fi
  run uv sync --directory "$HERE"
  SKEP="uv run skep"
elif command -v uv >/dev/null 2>&1; then
  run uv tool install skep
elif command -v pipx >/dev/null 2>&1; then
  run pipx install skep
else
  echo "skep install: install uv (https://docs.astral.sh/uv/) or pipx first" >&2
  exit 1
fi

if [ "$OS" = "Linux" ] && ! command -v bwrap >/dev/null 2>&1; then
  echo ""
  echo "NOTE: bubblewrap is not installed. Linux sandboxing requires it:"
  echo "  Fedora:        sudo dnf install bubblewrap"
  echo "  Ubuntu/Debian: sudo apt install bubblewrap"
fi

echo ""
echo "next steps:"
echo "  1. $SKEP setup --personal"
echo "  2. $SKEP doctor"
echo "  3. $SKEP serve   # access token prints in the boot log; UI at http://127.0.0.1:8765/"
