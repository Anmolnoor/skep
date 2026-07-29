#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK_DIR="${SKEP_DEMO_WORK_DIR:-/tmp/skep-demo-recording}"
REPO_DIR="$WORK_DIR/repo"
HOME_DIR="$WORK_DIR/home"
ASSET_DIR="${SKEP_DEMO_ASSET_DIR:-$ROOT/docs/assets}"
CAST_PATH="$ASSET_DIR/skep-demo.cast"
GIF_PATH="$ASSET_DIR/skep-demo.gif"
WORKER_SCRIPT="$ROOT/scripts/demo_worker.py"
UV_CACHE_DIR="${UV_CACHE_DIR:-$ROOT/.uv-cache}"
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON_BOOT=("$ROOT/.venv/bin/python")
else
  PYTHON_BOOT=(env UV_CACHE_DIR="$UV_CACHE_DIR" uv run python)
fi

shell_quote() {
  printf "%q" "$1"
}

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  DEMO_WORKER_CMD="$(shell_quote "$ROOT/.venv/bin/python") $(shell_quote "$WORKER_SCRIPT")"
else
  DEMO_WORKER_CMD="env UV_CACHE_DIR=$(shell_quote "$UV_CACHE_DIR") uv --project $(shell_quote "$ROOT") run python $(shell_quote "$WORKER_SCRIPT")"
fi

mkdir -p "$ASSET_DIR"
rm -rf "$WORK_DIR"
mkdir -p "$REPO_DIR/scripts" "$HOME_DIR"

cat > "$REPO_DIR/app.py" <<'PY'
def hello():
    return "hello"
PY

cat > "$REPO_DIR/scripts/check.py" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app

assert app.hello() == "hello"
if hasattr(app, "health"):
    assert app.health() == {"status": "ok"}
if hasattr(app, "goodbye"):
    assert app.goodbye() == "goodbye"
PY

git -C "$REPO_DIR" init -q -b main 2>/dev/null || git -C "$REPO_DIR" init -q
git -C "$REPO_DIR" config user.email demo@skep.local
git -C "$REPO_DIR" config user.name "Skep Demo"
git -C "$REPO_DIR" add .
git -C "$REPO_DIR" commit -qm "demo seed"

PYTHONPATH="$ROOT/src" "${PYTHON_BOOT[@]}" - "$HOME_DIR" "$REPO_DIR" <<'PY'
from pathlib import Path
import sys
from skep.supervisor import RunStore

store = RunStore(Path(sys.argv[1]) / "supervisor" / "supervisor.sqlite3")
try:
    store.set_setting("trusted_workspace_roots", [str(Path(sys.argv[2]))])
finally:
    store.close()
PY

SESSION_SCRIPT="$WORK_DIR/session.sh"
cat > "$SESSION_SCRIPT" <<SH
#!/usr/bin/env bash
set -euo pipefail
cd "$ROOT"
if [[ -x "$ROOT/.venv/bin/skep" ]]; then
  SKEP_CMD=("$ROOT/.venv/bin/skep")
else
  SKEP_CMD=(env UV_CACHE_DIR="$UV_CACHE_DIR" uv run skep)
fi
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON_CMD=("$ROOT/.venv/bin/python")
else
  PYTHON_CMD=(env UV_CACHE_DIR="$UV_CACHE_DIR" uv run python)
fi
clear
echo "$ skep run $REPO_DIR \"add a health endpoint\""
echo "When the inline approval prompt appears, press: b"
"\${SKEP_CMD[@]}" --home "$HOME_DIR" run "$REPO_DIR" "add a health endpoint" --execution-mode workspace --worker-cmd "$DEMO_WORKER_CMD"
TASK_ID="\$("\${PYTHON_CMD[@]}" - "$HOME_DIR" <<'PY'
from pathlib import Path
import sys
from skep.supervisor import RunStore
store = RunStore(Path(sys.argv[1]) / "supervisor" / "supervisor.sqlite3")
try:
    print(store.recent_runs(1)[0].task_id)
finally:
    store.close()
PY
)"
echo
echo "$ skep review \$TASK_ID"
"\${SKEP_CMD[@]}" --home "$HOME_DIR" review "\$TASK_ID"
echo
echo "$ skep review \$TASK_ID --approve"
"\${SKEP_CMD[@]}" --home "$HOME_DIR" review "\$TASK_ID" --approve --actor demo
echo
echo "$ skep run $REPO_DIR \"add a goodbye endpoint\""
"\${SKEP_CMD[@]}" --home "$HOME_DIR" run "$REPO_DIR" "add a goodbye endpoint" --execution-mode workspace --worker-cmd "$DEMO_WORKER_CMD" --quiet
echo
echo "Demo complete. The second run reused the remembered shell command."
SH
chmod +x "$SESSION_SCRIPT"

if command -v asciinema >/dev/null 2>&1; then
  asciinema rec --overwrite --cols 96 --rows 28 "$CAST_PATH" -c "$SESSION_SCRIPT"
  echo "wrote $CAST_PATH"
  if command -v agg >/dev/null 2>&1; then
    agg "$CAST_PATH" "$GIF_PATH"
    echo "wrote $GIF_PATH"
  else
    echo "agg not found; install it to export GIF:"
    echo "  cargo install --locked agg"
    echo "  agg $CAST_PATH $GIF_PATH"
  fi
else
  echo "Install asciinema to record $CAST_PATH."
  if [[ -t 0 && -t 1 ]]; then
    echo "Running the demo session without recording."
    "$SESSION_SCRIPT"
  else
    echo "Not running the interactive session because stdin/stdout is not a TTY."
  fi
fi
