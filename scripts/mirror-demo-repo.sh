#!/usr/bin/env bash
# Mirror examples/skep-demo into the public skep-demo repo (v37-F1).
#
# Operator script: it clones the target, replaces its content with the
# current examples/skep-demo tree, and commits — but it NEVER pushes unless
# --push is given. --dry-run lists what would mirror and touches nothing.
set -euo pipefail

usage() {
  echo "usage: mirror-demo-repo.sh <public-repo-url> [--push]"
  echo "       mirror-demo-repo.sh --dry-run"
}

URL=""
PUSH=0
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --push) PUSH=1 ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      echo "mirror-demo-repo: unknown argument '$arg'" >&2
      exit 2
      ;;
    *) URL="$arg" ;;
  esac
done

HERE="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$HERE/examples/skep-demo"
if [ ! -d "$SRC" ]; then
  echo "mirror-demo-repo: $SRC not found" >&2
  exit 1
fi

if [ "$DRY_RUN" -eq 1 ]; then
  echo "would mirror examples/skep-demo:"
  (cd "$HERE" && find examples/skep-demo -type f ! -path '*/__pycache__/*' | sort)
  exit 0
fi

if [ -z "$URL" ]; then
  usage >&2
  exit 2
fi

SHA="$(git -C "$HERE" rev-parse --short HEAD)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

git clone --quiet "$URL" "$TMP/mirror"
find "$TMP/mirror" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
(cd "$SRC" && find . -type d -name __pycache__ -prune -o -type f -print) | while read -r f; do
  mkdir -p "$TMP/mirror/$(dirname "$f")"
  cp "$SRC/$f" "$TMP/mirror/$f"
done

git -C "$TMP/mirror" add -A
if git -C "$TMP/mirror" diff --cached --quiet; then
  echo "mirror is already up to date with skep@$SHA"
  exit 0
fi
git -C "$TMP/mirror" commit --quiet -m "mirror examples/skep-demo from skep@$SHA"

if [ "$PUSH" -eq 1 ]; then
  git -C "$TMP/mirror" push
  echo "pushed skep-demo mirror (skep@$SHA)"
else
  echo "commit ready (skep@$SHA); to publish, re-run with --push:"
  echo "  scripts/mirror-demo-repo.sh $URL --push"
fi
