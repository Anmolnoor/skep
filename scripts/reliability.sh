#!/usr/bin/env bash
# Reliability gate: consecutive first-party worker runs with no manual cleanup.
# Asserts, per iteration, that the previous run left zero worktrees, zero worker
# processes, and zero stray git worktree registrations — then, at the end, all
# run records completed with passed verification.
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"
RUNS="${RELIABILITY_RUNS:-10}"

# Portable form (GNU mktemp rejects -t templates without X's; BSD allowed it).
BASE="$(mktemp -d "${TMPDIR:-/tmp}/skep-reliability.XXXXXX")"
trap 'rm -rf "$BASE"' EXIT
HOME_DIR="$BASE/home"
TOY="$BASE/toyrepo"
MARKER="reliability-$$"

uv run python - "$BASE" <<'PY'
import sys
from pathlib import Path

from tests.fixtures.toy_repo import create_toy_repo

base = Path(sys.argv[1])
create_toy_repo(base / "toyrepo")
PY

sweep() {
  local label="$1"
  local worktrees="$HOME_DIR/supervisor/worktrees"
  if [[ -d "$worktrees" ]] && [[ -n "$(ls -A "$worktrees" 2>/dev/null)" ]]; then
    echo "FAIL($label): leftover worktrees:" >&2
    ls -la "$worktrees" >&2
    exit 1
  fi
  local tracked
  tracked="$(git -C "$TOY" worktree list --porcelain | grep -c '^worktree ')"
  if [[ "$tracked" -ne 1 ]]; then
    echo "FAIL($label): toy repo tracks $tracked worktrees (expected 1)" >&2
    git -C "$TOY" worktree list >&2
    exit 1
  fi
  if pgrep -f "$MARKER" >/dev/null 2>&1; then
    echo "FAIL($label): zombie worker process still running" >&2
    pgrep -fl "$MARKER" >&2
    exit 1
  fi
}

for i in $(seq 1 "$RUNS"); do
  sweep "before-run-$i"
  uv run skep --home "$HOME_DIR" run "$TOY" \
    "Create a simple hello world in Python. ($MARKER #$i)" \
    --execution-mode workspace \
    --quiet >/dev/null
  sweep "after-run-$i"
  echo "run $i/$RUNS: ok"
done

uv run python - "$HOME_DIR" "$RUNS" <<'PY'
import sys
from pathlib import Path

from skep.supervisor import RunStore

home = Path(sys.argv[1])
expected = int(sys.argv[2])
store = RunStore(home / "supervisor" / "supervisor.sqlite3")
try:
    runs = store.recent_runs(expected * 2)
finally:
    store.close()
assert len(runs) == expected, f"expected {expected} run records, found {len(runs)}"
bad = [r.task_id for r in runs if r.state != "completed" or r.verification_outcome != "passed"]
assert not bad, f"unverified or incomplete runs: {bad}"
for record in runs:
    audit = home / "supervisor" / "audit" / record.task_id
    for name in ("task.json", "result.json", "events.ndjson"):
        assert (audit / name).is_file(), f"missing evidence {name} for {record.task_id}"
print(f"{expected} run records: all completed, all verification=passed, evidence complete")
PY

echo "RELIABILITY GATE: ${RUNS}/${RUNS} PASS"
