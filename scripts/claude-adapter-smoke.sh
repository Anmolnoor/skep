#!/usr/bin/env bash
# Manual release gate: exercise the real Claude Code adapter against a
# disposable repository. This invokes the logged-in `claude` CLI and may consume
# provider quota, so it is intentionally not part of the default test suite.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UV_CACHE_DIR="${UV_CACHE_DIR:-$ROOT/.uv-cache}"
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/skep-claude-smoke.XXXXXX")"
REPO_DIR="$WORK_DIR/repo"
HOME_DIR="$WORK_DIR/home"
CLAUDE_CMD="${SKEP_CLAUDE_CODE_CMD:-claude}"
KEEP="${SKEP_CLAUDE_SMOKE_KEEP:-0}"

cleanup() {
  if [[ "$KEEP" != "1" ]]; then
    rm -rf "$WORK_DIR"
  else
    echo "kept smoke workspace: $WORK_DIR"
  fi
}
trap cleanup EXIT

read -r -a CLAUDE_PARTS <<< "$CLAUDE_CMD"
if [[ "${#CLAUDE_PARTS[@]}" -eq 0 ]] || ! command -v "${CLAUDE_PARTS[0]}" >/dev/null 2>&1; then
  echo "Claude Code executable not found. Set SKEP_CLAUDE_CODE_CMD=/path/to/claude." >&2
  exit 2
fi
CLAUDE_EXECUTABLE="${CLAUDE_PARTS[0]}"

set +e
CLAUDE_LOGIN_OUTPUT="$("$CLAUDE_EXECUTABLE" auth status 2>&1)"
CLAUDE_LOGIN_STATUS=$?
set -e
if [[ "$CLAUDE_LOGIN_STATUS" -ne 0 ]] \
  || grep -q '"loggedIn": false' <<< "$CLAUDE_LOGIN_OUTPUT" \
  || grep -qi "not logged in" <<< "$CLAUDE_LOGIN_OUTPUT"; then
  echo "$CLAUDE_LOGIN_OUTPUT" >&2
  if grep -q '"loggedIn": false' <<< "$CLAUDE_LOGIN_OUTPUT" \
    || grep -qi "not logged in" <<< "$CLAUDE_LOGIN_OUTPUT"; then
    echo "Not logged in: Claude Code auth is required before running this smoke." >&2
    echo "Please run: claude auth login" >&2
  else
    echo "Claude Code auth status check failed." >&2
  fi
  exit 2
fi

set +e
CLAUDE_PRINT_PROBE_OUTPUT="$(
  "${CLAUDE_PARTS[@]}" \
    --print \
    --max-budget-usd 0.01 \
    --no-session-persistence \
    "Reply with exactly: skep claude adapter smoke ready" 2>&1
)"
CLAUDE_PRINT_PROBE_STATUS=$?
set -e
if [[ "$CLAUDE_PRINT_PROBE_STATUS" -ne 0 ]]; then
  echo "$CLAUDE_PRINT_PROBE_OUTPUT" >&2
  echo "Claude Code print-mode preflight failed." >&2
  if grep -qi "not logged in\\|authenticate\\|invalid authentication" \
    <<< "$CLAUDE_PRINT_PROBE_OUTPUT"; then
    echo "Please run: claude auth login" >&2
  fi
  echo "Set SKEP_CLAUDE_CODE_CMD to a working Claude Code command if the default model is unavailable." >&2
  exit 2
fi

if [[ -x "$ROOT/.venv/bin/skep" && -x "$ROOT/.venv/bin/python" ]]; then
  SKEP_CMD=("$ROOT/.venv/bin/skep")
  PYTHON_CMD=("$ROOT/.venv/bin/python")
else
  SKEP_CMD=(env UV_CACHE_DIR="$UV_CACHE_DIR" uv run skep)
  PYTHON_CMD=(env UV_CACHE_DIR="$UV_CACHE_DIR" uv run python)
fi

shell_quote() {
  printf "%q" "$1"
}

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  CLAUDE_WORKER_CMD="$(shell_quote "$ROOT/.venv/bin/python") -m skep.workers.claude_code"
else
  CLAUDE_WORKER_CMD="env UV_CACHE_DIR=$(shell_quote "$UV_CACHE_DIR") uv --project $(shell_quote "$ROOT") run python -m skep.workers.claude_code"
fi

mkdir -p "$REPO_DIR" "$HOME_DIR"
cat > "$REPO_DIR/README.md" <<'EOF'
# Claude adapter smoke

This disposable repo is created by scripts/claude-adapter-smoke.sh.
EOF

git -C "$REPO_DIR" init -q -b main 2>/dev/null || git -C "$REPO_DIR" init -q
git -C "$REPO_DIR" config user.email claude-smoke@skep.local
git -C "$REPO_DIR" config user.name "Skep Claude Smoke"
git -C "$REPO_DIR" add README.md
git -C "$REPO_DIR" commit -qm seed

echo "smoke workspace: $WORK_DIR"
echo "claude command: $CLAUDE_CMD"

env SKEP_CLAUDE_CODE_CMD="$CLAUDE_CMD" \
  "${SKEP_CMD[@]}" --home "$HOME_DIR" run "$REPO_DIR" \
  "Create a new file named claude_smoke.txt containing exactly one line: claude adapter smoke ok" \
  --execution-mode workspace \
  --worker-cmd "$CLAUDE_WORKER_CMD" \
  --env-allow SKEP_CLAUDE_CODE_CMD \
  --quiet

TASK_ID="$(
  "${PYTHON_CMD[@]}" - "$HOME_DIR" <<'PY'
from pathlib import Path
import sys

from skep.supervisor import RunStore

home = Path(sys.argv[1])
store = RunStore(home / "supervisor" / "supervisor.sqlite3")
try:
    run = store.recent_runs(1)[0]
finally:
    store.close()

assert run.state == "completed", f"expected completed, got {run.state}"
assert run.verification_outcome == "passed", run.verification_outcome
assert run.worker_version == "claude-code-adapter-0.1.0", run.worker_version
print(run.task_id)
PY
)"

"${SKEP_CMD[@]}" --home "$HOME_DIR" review "$TASK_ID"
echo "CLAUDE ADAPTER SMOKE PASS: $TASK_ID"
