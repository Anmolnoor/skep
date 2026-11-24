#!/usr/bin/env bash
# Phase 09 (Test 9.1): seed the four Dependabot PR shapes into a THROWAWAY fixture.
#
#   seed-dependabot-fixture.sh DIR                           local only: DIR/repo + DIR/origin.git
#   seed-dependabot-fixture.sh DIR --github OWNER/NAME       also push and open the 4 PRs there
#   seed-dependabot-fixture.sh DIR --move-head [--github R]  shape (d): push one more commit to it
#
# The shapes, each a branch off main bumping the vendored `fakelib`:
#   (a) dependabot/pip/fakelib-1.0.1        clean bump: tests pass
#   (b) dependabot/pip/fakelib-2.0.0        breaking bump: test_add_negative fails
#   (c) dependabot/pip/fakelib-2.0.0-tests  the breaking bump + a skipped test (V1 tamper)
#   (d) dependabot/pip/fakelib-1.0.2        clean bump whose head --move-head moves
#
# Safety: DIR must be missing, empty, or a fixture this script made (it holds
# .skep-dependabot-fixture); a re-run rebuilds only DIR/repo and DIR/origin.git.
# Nothing is pushed anywhere but DIR/origin.git without --github, and --github
# needs a repo you created yourself (gh repo create OWNER/NAME --private) whose
# name contains "fixture" and whose main is empty or this fixture's. Commits are
# dated and authored fixedly, so a re-run gives the same SHAs (given one uv).
set -euo pipefail

MARKER=.skep-dependabot-fixture
SHAPES=(
  "a|dependabot/pip/fakelib-1.0.1|Bump fakelib from 1.0.0 to 1.0.1|verified: G10 confirms base...head, merge_pr pins the head"
  "b|dependabot/pip/fakelib-2.0.0|Bump fakelib from 1.0.0 to 2.0.0|fails: G10 failed, nothing merges"
  "c|dependabot/pip/fakelib-2.0.0-tests|Bump fakelib from 1.0.0 to 2.0.0 and skip the failing test|tampered: V1 flags the skip across base...head"
  "d|dependabot/pip/fakelib-1.0.2|Bump fakelib from 1.0.0 to 1.0.2|verify it, propose merge_pr, run --move-head, confirm: GitHub refuses"
)
MOVE_MSG="Bump fakelib from 1.0.0 to 1.0.3"

refuse() { echo "seed-dependabot-fixture: refused: $*" >&2; exit 2; }
usage() {
  echo "usage: seed-dependabot-fixture.sh DIR [--github OWNER/NAME] [--move-head]" >&2
  exit 2
}

DIR="" GITHUB="" MOVE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --github) [ $# -ge 2 ] || usage; GITHUB="$2"; shift 2 ;;
    --move-head) MOVE=1; shift ;;
    -*) usage ;;
    *) [ -z "$DIR" ] || usage; DIR="$1"; shift ;;
  esac
done
[ -n "$DIR" ] || usage

if [ -n "$GITHUB" ]; then
  case "$GITHUB" in */*fixture*) ;; *) refuse "--github $GITHUB: give OWNER/NAME with 'fixture' in NAME (a throwaway repo only)" ;; esac
fi
if [ -e "$DIR" ]; then
  [ -d "$DIR" ] || refuse "$DIR is not a directory"
  if [ ! -f "$DIR/$MARKER" ] && [ -n "$(ls -A "$DIR")" ]; then
    refuse "$DIR is not empty and is not a fixture this script made (no $MARKER)"
  fi
fi
mkdir -p "$DIR"
DIR="$(cd "$DIR" && pwd)"
REPO="$DIR/repo"

export GIT_AUTHOR_NAME="skep fixture" GIT_AUTHOR_EMAIL="fixture@example.invalid"
export GIT_COMMITTER_NAME="skep fixture" GIT_COMMITTER_EMAIL="fixture@example.invalid"
g() { git -C "$REPO" -c commit.gpgsign=false -c core.hooksPath=/dev/null "$@"; }
commit() {  # commit DATE MESSAGE — everything staged, at a fixed date
  g add -A
  GIT_AUTHOR_DATE="$1" GIT_COMMITTER_DATE="$1" g commit -q -m "$2"
}
fakelib() {  # fakelib VERSION EXPR — the vendored dependency at one version
  cat > "$REPO/fakelib/__init__.py" <<EOF
"""fakelib: a vendored stand-in dependency the fixture's PRs bump."""

__version__ = "$1"


def add(a, b):
    return $2
EOF
}

remote_url() {
  if [ -n "$GITHUB" ]; then
    gh repo view "$GITHUB" --json url --jq .url \
      || refuse "gh cannot see $GITHUB — create it first: gh repo create $GITHUB --private"
  else
    echo "$DIR/origin.git"
  fi
}

if [ "$MOVE" -eq 1 ]; then
  [ -f "$DIR/$MARKER" ] && [ -d "$REPO/.git" ] || refuse "$DIR holds no seeded fixture; seed it first"
  branch="$(echo "${SHAPES[3]}" | cut -d'|' -f2)"
  g checkout -q "$branch"
  if [ "$(g log -1 --format=%s)" = "$MOVE_MSG" ]; then
    echo "shape (d) already moved: $branch at $(g rev-parse HEAD)"
    exit 0
  fi
  fakelib 1.0.3 "a + b"
  commit "2026-01-02T00:00:00Z" "$MOVE_MSG"
  g push -q origin "$branch"
  echo "moved shape (d): $branch is now $(g rev-parse HEAD) — confirm its merge_pr card: GitHub must refuse it"
  exit 0
fi

command -v uv >/dev/null || refuse "uv is not on PATH (the fixture commits a uv.lock)"
URL="$(remote_url)"
if [ -n "$GITHUB" ] && [ -n "$(git ls-remote "$URL" refs/heads/main)" ]; then
  probe="$(mktemp -d)"
  git -C "$probe" init -q
  git -C "$probe" fetch -q "$URL" main
  git -C "$probe" cat-file -e "FETCH_HEAD:$MARKER" 2>/dev/null \
    || { rm -rf "$probe"; refuse "$GITHUB has a main that is not this fixture's (no $MARKER)"; }
  rm -rf "$probe"
fi

rm -rf "$REPO" "$DIR/origin.git"
touch "$DIR/$MARKER"
git init -q -b main "$REPO"
mkdir -p "$REPO/fakelib" "$REPO/tests"
echo "seeded by skep scripts/seed-dependabot-fixture.sh — a throwaway fixture" > "$REPO/$MARKER"
printf '__pycache__/\n.venv/\n.pytest_cache/\n' > "$REPO/.gitignore"
cat > "$REPO/README.md" <<'EOF'
# dependabot-fixture

A throwaway repo seeded by skep's `scripts/seed-dependabot-fixture.sh` for
Phase 09 Test 9.1. Its branches play Dependabot bumps of the vendored
`fakelib`. Verify: `uv run pytest`.
EOF
cat > "$REPO/pyproject.toml" <<'EOF'
[project]
name = "dependabot-fixture"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = []

[dependency-groups]
dev = ["pytest>=8"]

[tool.pytest.ini_options]
pythonpath = ["."]
EOF
cat > "$REPO/app.py" <<'EOF'
from fakelib import add


def total(values):
    result = 0
    for value in values:
        result = add(result, value)
    return result
EOF
cat > "$REPO/tests/test_app.py" <<'EOF'
from app import total
from fakelib import add


def test_total():
    assert total([1, 2, 3]) == 6


def test_add_negative():
    assert add(-1, 1) == 0
EOF
fakelib 1.0.0 "a + b"
(cd "$REPO" && uv lock -q)
commit "2026-01-01T00:00:00Z" "dependabot fixture: main"

for shape in "${SHAPES[@]}"; do
  IFS='|' read -r key branch title _ <<< "$shape"
  g checkout -q -b "$branch" main
  case "$key" in
    a) fakelib 1.0.1 "a + b" ;;
    b) fakelib 2.0.0 "abs(a) + abs(b)" ;;
    c) fakelib 2.0.0 "abs(a) + abs(b)"
       cat > "$REPO/tests/test_app.py" <<'EOF'
import pytest

from app import total
from fakelib import add


def test_total():
    assert total([1, 2, 3]) == 6


@pytest.mark.skip(reason="fakelib 2 changed add")
def test_add_negative():
    assert add(-1, 1) == 0
EOF
       ;;
    d) fakelib 1.0.2 "a + b" ;;
  esac
  commit "2026-01-01T00:00:00Z" "$title"
done
g checkout -q main

[ -n "$GITHUB" ] || git init -q --bare "$DIR/origin.git"
g remote add origin "$URL"
branches=()
for shape in "${SHAPES[@]}"; do branches+=("$(echo "$shape" | cut -d'|' -f2)"); done
g push -q --force origin main "${branches[@]}"

echo "fixture: $REPO (origin $URL)"
for shape in "${SHAPES[@]}"; do
  IFS='|' read -r key branch title expect <<< "$shape"
  if [ -n "$GITHUB" ] && [ "$(gh pr list --repo "$GITHUB" --head "$branch" --state open --json number --jq length)" = "0" ]; then
    gh pr create --repo "$GITHUB" --base main --head "$branch" --title "$title" \
      --body "Seeded by skep's seed-dependabot-fixture.sh, shape ($key). Expected: $expect." >/dev/null
  fi
  echo "  ($key) $branch $(g rev-parse --short=12 "$branch") — $expect"
done
[ -n "$GITHUB" ] || echo "local only: these are branches, not PRs; Test 9.1 needs --github OWNER/NAME"
