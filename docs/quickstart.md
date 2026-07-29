# 5-minute quickstart

This is the shortest path from a fresh checkout to a supervised coding run.

## 1. Prerequisites

- Git
- Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/)
- `bubblewrap` on Linux if you want the Linux sandbox backend

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Linux sandbox package:

```sh
# Fedora
sudo dnf install bubblewrap

# Ubuntu/Debian
sudo apt install bubblewrap
```

## 2. Install from source

```sh
git clone https://github.com/Anmolnoor/skep.git
cd skep
uv sync --frozen
```

## 3. Pick a worker

The default worker is the in-repo deterministic coding worker. To run Claude
Code through skep, use the adapter:

```sh
uv run skep run \
  --execution-mode workspace \
  --worker-cmd "python -m skep.workers.claude_code" \
  /path/to/your-repo \
  "fix the failing test"
```

The adapter expects `claude` on `PATH`. If needed:

```sh
SKEP_CLAUDE_CODE_CMD="/path/to/claude" \
uv run skep run \
  --execution-mode workspace \
  --worker-cmd "python -m skep.workers.claude_code" \
  --env-allow SKEP_CLAUDE_CODE_CMD \
  /path/to/your-repo \
  "add input validation"
```

You can also set `SKEP_WORKER_CMD` once:

```sh
export SKEP_WORKER_CMD="python -m skep.workers.claude_code"
```

## 4. Run a task

```sh
uv run skep run /path/to/your-repo "add a README section for local setup" --execution-mode workspace
```

Skep creates an isolated worktree, spawns the worker, streams progress, records
events, captures the patch, and re-verifies the claimed result.

If a worker needs a gated shell command, skep can prompt inline:

```text
approval needed: shell.run
  reason:       shell.run requires approval for command: python scripts/check.py
  [a] approve once  [b] approve + remember  [d] deny  [s] skip
```

Choose `b` when the command is normal for this kind of task. If the resumed run
finishes successfully, Skep saves or updates a learned template for this repo.
The next similar run can auto-match that template and pre-grant the remembered
permissions.

If you skip the inline prompt, use `skep review <task_id> --allow-command` to
approve a pending shell command later.

## 5. Review and approve

```sh
uv run skep status --personal
uv run skep review <task_id>
uv run skep review <task_id> --approve
```

Approval applies the patch on a branch:

```text
skep/<task_id>
```

The agent does not push to main. Review, merge, or discard the branch using your
normal Git workflow.

## Serve UI

For the browser dashboard:

```sh
uv run skep serve --host 127.0.0.1 --port 8765
```

Copy the printed access token, open `http://127.0.0.1:8765`, and use Settings
to configure the assistant connection for the default coding worker.
