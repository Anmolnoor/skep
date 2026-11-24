# 5-minute quickstart

This is the shortest path from a new machine to a supervised coding run.

## 1. Install and onboard

```sh
pipx install skep && skep onboard
```

You need Python 3.12 or newer and Git. `skep onboard` is the guided first
run: it connects a model, registers a work directory (default `~/workspace`)
as a project, records the sandbox backend, locks a policy baseline after you
type `yes`, records a role, then starts `skep serve` and opens `skep chat`.
A fresh home asks before every governed action until that baseline is
approved. See [onboarding.md](onboarding.md) for each step and its flags, and
run `skep onboard --check` at any time to see where the setup stands.

On Debian/Ubuntu the system Python refuses bare `pip install` (PEP 668) —
pipx is the way around it, and the PATH step is part of the install, not an
optional extra:

```sh
sudo apt install pipx   # Debian/Ubuntu (Fedora: sudo dnf install pipx)
pipx ensurepath         # puts ~/.local/bin on your PATH for future sessions
source ~/.bashrc        # reload so the PATH change takes effect now
pipx install skep
skep --version          # verify it resolves
```

`uv tool install skep` works the same way. To upgrade: `pipx upgrade skep`
(or `uv tool upgrade skep`), then restart `skep serve` and run `skep doctor`.
An upgrade never rewrites a project's stored policy, so the doctor lists the
projects a newer default left behind (see below).

The Linux sandbox backend needs `bubblewrap`; macOS ships Seatbelt:

```sh
# Fedora
sudo dnf install bubblewrap

# Ubuntu/Debian
sudo apt install bubblewrap
```

## Check the setup

```sh
skep doctor
```

The report has three sections, and the command exits 0 only when Health
passes:

- **Health: pass** (or **fail**): the profile, storage and provider checks,
  the coding worker, each engine, approvals and memory. The provider and
  coding worker lines send the model one authenticated live completion, not
  just a model-list request (ollama.com lists models to anyone), so a key the
  provider refuses (HTTP 401/403) fails Health here instead of your first run.
- **Security posture**: the sandbox and how it confines reads, the provider
  key relay, and the policy baseline lock. On Linux without bubblewrap the
  sandbox line says `bwrap was not found on PATH` and gives the install
  command above.
- **Projects needing upgrade**: each project that still runs unsandboxed,
  pins no verify command, or reads the whole disk, followed by the one
  `skep project setup …` line that fixes it. Run that line as printed. If it
  ends in `--verify-command '<your test command>'`, skep found no test entry
  point in the repository, so replace the placeholder with your own command.
  A project bound by name to a managed clone gets that clone's path; when no
  checkout can be found, the line says so and what to run once you have one.

`skep doctor --offline` makes no network call (not even the GitHub Copilot
token exchange); the provider lines then say `skipped (--offline)`. `skep doctor --json` prints the same report as JSON.

## 2. Run a task

```sh
skep run ~/workspace "add a README section for local setup" --execution-mode sandbox
```

Skep creates an isolated worktree, spawns the worker, streams progress, records
events, captures the patch, and re-verifies the claimed result.

`--execution-mode sandbox` runs the worker behind the OS sandbox, and the run
does not start when no backend can be applied. `--execution-mode workspace`
has no OS sandbox: the worker still runs in a disposable worktree, through
skep's capability layer, with re-verification and approval, and only skep's
own worker can run that way.

The default worker is skep's own coding worker. It plans with the model the
assistant is configured with (onboard step ①) and runs each step through the
capability layer. With no model configured it falls back to a fixed routine
that handles only a Python hello-world task, and any other run fails.

If a worker needs a gated shell command, skep can prompt inline:

```text
approval needed: shell.run
  reason:       shell.run requires approval for command: python scripts/check.py
  [a] approve once  [b] approve + remember  [d] deny  [s] skip
```

Choose `b` when the command is normal for this kind of task. It saves the
command as a grant at once (`remembered: <command> → <tier>`); `[b]` is offered
only when the command can be remembered. If the resumed run then finishes
successfully, Skep also saves or updates a learned template for this repo, and
the next similar run can auto-match it and pre-grant the remembered
permissions. `skep policy grants` lists what was saved; `skep policy revoke`
removes one.

If you skip the inline prompt, use `skep review <task_id> --allow-command` to
approve a pending shell command later.

## 3. Review and approve

```sh
skep status --personal
skep review <task_id>
skep review <task_id> --approve
```

Approval applies the patch on a branch:

```text
skep/<task_id>
```

A worker can never push, and your default branch does not move. Review, merge,
or discard the branch using your normal Git workflow. `--pr` on the approval
pushes that branch and opens a pull request.

Re-verification uses the project's pinned verify command. Project setup infers
one from the repository's own entry point; when it finds none, re-verification
falls back to the command the worker chose and the record says so. Pin one:

```sh
skep project setup ~/workspace --verify-command "uv run pytest"
```

## 4. Pick another engine

To run Claude Code through skep, pin it as the project's engine (`codex`,
`aider` and `pi` work the same way). An external agent's commands skip skep's
capability layer, so the sandbox is its only wall: it always runs in sandbox
mode, and the project must pin the command that proves its work:

```sh
skep project setup ~/workspace \
  --engine claude_code \
  --verify-command "uv run pytest"
# the sandbox masks the keychain: authenticate from the env instead
export CLAUDE_CODE_OAUTH_TOKEN=paste-it-here  # printed by: claude setup-token
skep run ~/workspace "fix the failing test" --execution-mode sandbox
```

The work directory from onboarding is already a project. A repository skep
has not seen yet needs a strategy the first time; without one setup is refused
with `project setup requires a pack or strategy`:

```sh
skep project setup /path/to/repo --strategy trusted_local_dev \
  --engine claude_code --verify-command "uv run pytest"
```

Codex authenticates from `OPENAI_API_KEY`, or from your ChatGPT plan login
for a project that opted in with `--codex-chatgpt-login` (see
[codex-chatgpt-login.md](codex-chatgpt-login.md)). Without its auth variable
an external engine's run is refused with `NO AUTH ENV`; `skep doctor` shows
each engine's state.

The adapter expects `claude` on `PATH`. If needed:

```sh
SKEP_CLAUDE_CODE_CMD="/path/to/claude" \
skep run \
  --execution-mode sandbox \
  --env-allow SKEP_CLAUDE_CODE_CMD \
  ~/workspace \
  "add input validation"
```

A one-off run can pick the engine with `--engine claude_code` instead. A
`--worker-cmd` or `SKEP_WORKER_CMD` that runs an external agent's module
(`python -m skep.workers.claude_code`) is that engine: it runs in the
sandbox whatever `--execution-mode` says, and needs the same auth env and
pinned `verify_command`.

## Serve UI

`skep onboard` starts the daemon for you. To start it yourself:

```sh
skep serve
```

Copy the printed access token, open `http://127.0.0.1:8765`, and use Settings
to configure the assistant connection for the default coding worker.

## From a source checkout

What contributors use. You also need
[`uv`](https://docs.astral.sh/uv/getting-started/installation/):

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/Anmolnoor/skep.git
cd skep
uv sync --frozen
```

Every command above then runs as `uv run skep …`:

```sh
uv run skep onboard
uv run skep run /path/to/your-repo "fix the failing test" --execution-mode sandbox
uv run skep review <task_id> --approve
uv run skep serve --host 127.0.0.1 --port 8765
```
