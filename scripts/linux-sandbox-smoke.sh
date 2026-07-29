#!/usr/bin/env bash
# Manual release gate: exercise a real Linux bubblewrap sandbox run against a
# disposable repository. Run this on Fedora/Ubuntu with `bwrap` installed.
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "linux-sandbox-smoke requires Linux with bubblewrap; this host is $(uname -s)." >&2
  exit 2
fi

if ! command -v bwrap >/dev/null 2>&1; then
  echo "bubblewrap not found. Install it first: apt install bubblewrap or dnf install bubblewrap." >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UV_CACHE_DIR="${UV_CACHE_DIR:-$ROOT/.uv-cache}"
WORK_DIR="$(mktemp -d "$ROOT/.skep-linux-sandbox.XXXXXX")"
REPO_DIR="$WORK_DIR/repo"
HOME_DIR="$WORK_DIR/home"
KEEP="${SKEP_LINUX_SANDBOX_SMOKE_KEEP:-0}"

cleanup() {
  if [[ "$KEEP" != "1" ]]; then
    rm -rf "$WORK_DIR"
  else
    echo "kept smoke workspace: $WORK_DIR"
  fi
}
trap cleanup EXIT

if [[ -x "$ROOT/.venv/bin/skep" && -x "$ROOT/.venv/bin/python" ]]; then
  SKEP_CMD=("$ROOT/.venv/bin/skep")
  PYTHON_CMD=("$ROOT/.venv/bin/python")
else
  SKEP_CMD=(env UV_CACHE_DIR="$UV_CACHE_DIR" uv run skep)
  PYTHON_CMD=(env UV_CACHE_DIR="$UV_CACHE_DIR" uv run python)
fi

mkdir -p "$REPO_DIR" "$HOME_DIR"
cat > "$REPO_DIR/README.md" <<'EOF'
# Linux sandbox smoke

This disposable repo is created by scripts/linux-sandbox-smoke.sh.
EOF

git -C "$REPO_DIR" init -q -b main 2>/dev/null || git -C "$REPO_DIR" init -q
git -C "$REPO_DIR" config user.email linux-sandbox@skep.local
git -C "$REPO_DIR" config user.name "Skep Linux Sandbox"
git -C "$REPO_DIR" add README.md
git -C "$REPO_DIR" commit -qm seed

echo "smoke workspace: $WORK_DIR"
echo "bwrap: $(command -v bwrap)"

"${SKEP_CMD[@]}" --home "$HOME_DIR" run "$REPO_DIR" \
  "Create a simple hello world in Python." \
  --execution-mode sandbox \
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
assert run.execution_mode == "sandbox", run.execution_mode
print(run.task_id)
PY
)"

"${SKEP_CMD[@]}" --home "$HOME_DIR" review "$TASK_ID"
echo "LINUX SANDBOX SMOKE PASS: $TASK_ID"
