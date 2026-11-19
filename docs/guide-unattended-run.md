# Guide: hand Claude Code or Codex a task and walk away

<!-- Commands in this guide were run on skep 1.4.2 (Fedora, bubblewrap, Landlock ABI 10)
     on 2026-11-18 with a throwaway SKEP_HOME and throwaway repositories under ~/.cache,
     with no API key and no engine login. Output is pasted as printed, with three
     rewrites: the temporary repository path became /home/you/code/calc, the temporary
     home became /home/you/.skep, and the OS user name became "you". Lines left out are
     marked "...". Steps that need an engine credential were not run: their output is
     described from the print statements, with the source line in a comment beside each. -->

This guide is for someone who wants to give an external coding agent one task,
leave, and come back to a patch they can trust or reject. At the end you will
have:

- a run of Claude Code or Codex that happened inside skep's OS sandbox,
- skep's own re-run of your project's test command on a clean checkout,
- the patch held for your approval, and after approval a branch named
  `skep/<task_id>` (and a pull request if you ask for one).

Your default branch does not move at any point in this guide.

## What you need first

- skep 1.4.2 or newer: `pipx install skep`, then `skep --version`.
- macOS or Linux. On Linux the sandbox needs bubblewrap:
  `sudo dnf install bubblewrap` or `sudo apt install bubblewrap`.
- The agent's own CLI on your `PATH`: `claude` (Claude Code) or `codex`.
- A credential for that agent that you can put in an environment variable
  (step 2).
- A git repository with at least one commit, and a command that proves the
  work: one that exits non-zero when the code is broken and that runs on a
  clean checkout of the repository (`pytest`, `npm test`, `make test`).

The examples use a repository at `~/code/calc` whose test suite has one failing
test.

## 1. Check the sandbox and the engine

```sh
skep doctor
```

On a fresh home with nothing configured, the relevant lines are:

```text
- engine claude_code: NO AUTH ENV — set one of CLAUDE_CODE_OAUTH_TOKEN/ANTHROPIC_API_KEY — /home/you/.local/bin/claude (sandbox-confined; needs verify_command)
- engine codex: NO AUTH ENV — set one of OPENAI_API_KEY — /home/you/.nvm/versions/node/v24.15.0/bin/codex (sandbox-confined; needs verify_command)
...
Runtime checks:
- sandbox: ready - bubblewrap backend is usable. Landlock ABI 10: a worker connects only to unix sockets in its own roots or ones it made itself.
```

Three things to read:

- **`sandbox: ready`.** If it says anything else, stop here. An external agent
  runs only inside the sandbox, and skep refuses the run rather than start it
  unconfined. On Linux the usual cause is that `bwrap` is not installed.
- **The engine line.** `MISSING` means the agent's CLI is not on `PATH`.
  `NO AUTH ENV` is what step 2 fixes. `(sandbox-confined; needs
  verify_command)` is what step 3 fixes.
- **`Skep: blocked` and `profile: blocked`** at the top are about the chat
  assistant's own model. The runs in this guide do not use it; every command
  below was run on a home where those lines were still showing.

## 2. Put the agent's credential in the environment

The agent's normal login (the keychain, `~/.claude/.credentials.json`,
`~/.codex/auth.json`) is hidden from every sandboxed worker, so the credential
has to arrive as an environment variable in the shell you run `skep` from.

| Engine | Set one of | Where it comes from |
|---|---|---|
| `claude_code` | `CLAUDE_CODE_OAUTH_TOKEN` | `claude setup-token` prints it (uses your Claude plan) |
| `claude_code` | `ANTHROPIC_API_KEY` | an API key (billed per token) |
| `codex` | `OPENAI_API_KEY` | an API key |

```sh
export CLAUDE_CODE_OAUTH_TOKEN=paste-the-token-here
```

Codex can instead use a ChatGPT plan login, which is a per-project opt-in:
see [`codex-chatgpt-login.md`](codex-chatgpt-login.md).

Run `skep doctor` again. The engine line should now start with `ok`.

<!-- not run (needs a credential). The engine rows are printed by
     src/skep/status.py:855-875; the auth env names come from
     src/skep/supervisor/engines.py:103-150. -->

## 3. Register the project with its engine and its verify command

An external agent needs a project that names the engine and pins the command
that proves the work. Give the strategy in the same command. Without it, on a
repository that is not yet a project, setup fails:

```sh
skep project setup ~/code/calc --engine claude_code
```

```text
error: project setup requires a pack or strategy
```

The order that works is one command with `--strategy`:

```sh
skep project setup ~/code/calc --strategy trusted_local_dev \
  --engine claude_code --verify-command "python -m pytest -q"
```

```text
saved: calc (trusted_local_dev/build)
  pack: -
  bindings: repo_path=/home/you/code/calc
  seeded templates: 0
  seeded schedules: 1
    calc-maintain-weekly
  seeded shell commands: 2
    uv run pytest
    pytest
  verify command: python -m pytest -q
  coding engine: claude_code
  loopback ports: closed (--loopback-port PORT opens one)
```

What this saved:

- **`trusted_local_dev/build`.** The strategy for your own repository, in the
  `build` phase, where landing a patch needs your approval.
- **`verify command`.** The command skep itself re-runs after the agent
  finishes. Leave `--verify-command` out and setup infers one from the
  repository (`make test`, `uv run pytest`, `pytest` or `npm test`); check the
  line says what you expect. More in
  [`guide-pin-verify-command.md`](guide-pin-verify-command.md).
- **`seeded schedules: 1`.** Setup also created a weekly maintenance schedule.
  It runs only if you run `skep tick` or `skep serve`. If you do not want it,
  pass `--no-seed-schedules` to setup, or remove it:
  `skep schedule remove calc-maintain-weekly`.

Once the project exists, later changes need no `--strategy`:
`skep project setup ~/code/calc --engine codex` changes only the engine.
`skep project show calc` prints the saved policy.

## 4. Know what a home without a policy baseline does

```sh
skep policy baseline status
```

```text
baseline: none approved
```

A home created by a fresh install answers `ask` to policy questions until you
approve a baseline. For this guide that matters less than it sounds:

- A plain `skep run`, as in step 5, **is not held up**. It was run on a home
  with no baseline for this guide and dispatched normally.
- A run with `--role` or `--delegated-by` is refused off a terminal, because
  nothing could answer its questions once it is going:

  ```text
  error: this home has no approved policy baseline, so this run would ask about files.pythonhosted.org, proxy.golang.org, pypi.org, registry.npmjs.org and nothing could answer once it runs; name the hosts to answer for this run only in --answer-host HOST (repeatable), or approve a baseline (skep policy approve)
  ```

- `skep project setup --unlocked` is refused until a baseline is approved.
- `skep policy why`, the Claude Code hook, the ACP governor and the chat
  assistant all answer `ask` with the reason `no_baseline`.

To approve one, read a template and lock it:

```sh
skep policy explain builder
skep policy approve --template builder
```

`approve` prints the same table and waits for you to type `yes`
(`--yes` skips the prompt). Afterwards:

```text
baseline: builder approved 2026-11-19T00:25:43Z sha256 01db10ac4fc3
```

`skep onboard` does the same as one of its five steps
([`onboarding.md`](onboarding.md)).

## 5. Run the task and leave

```sh
skep run ~/code/calc "make the failing test pass" --execution-mode sandbox
```

An external agent always runs in the sandbox: for a project that pins one,
`--execution-mode workspace` is turned into `sandbox`.

If the credential from step 2 is not in this shell, nothing starts:

```text
error: coding_engine 'claude_code': NO AUTH ENV — set one of CLAUDE_CODE_OAUTH_TOKEN/ANTHROPIC_API_KEY in the environment the worker inherits (the daemon reads <home>/serve.env at startup; `skep run` uses your shell) — the line `skep doctor` prints for this engine
  next:     skep doctor  # engines, auth env, sandbox
```

The exit code is 2 and no run was recorded. This is the furthest this guide's
author could go without a credential. What follows is what the code prints
once the credential is there.

<!-- from code, not run. Live tail and summary: src/skep/supervisor/cli_cmds.py:377-388
     (the re-verify line), _verification_lines (task / state / verification / worker claim /
     evidence / next, and the exit code; v127-T3). The same code path printed the real builtin-worker
     transcripts in guide-pin-verify-command.md. -->

While the agent works, skep prints one line per phase: the worker starting
(with the project, the dispatch decision and `landing: require_approval`), then
what the agent reported, then skep's own re-verification:

```text
  → re-verify: passed [confirmed]  worker passed  re-ran clean: all exit 0 [command from the project's pinned verify_command]  re-ran python -m pytest -q  -> exit 0
```

and finally a summary block:

```text
task <task_id>
  state:        completed (<seconds>s)
  verification: confirmed by re-verify (G10) — re-ran python -m pytest -q → exit 0
  worker claim: passed — <the agent's summary>
  evidence:     /home/you/.skep/supervisor/audit/<task_id>
  next:         skep review <task_id>
```

You do not have to watch. The run is bounded by its budget
(`--budget-wall-clock SECONDS` changes the wall-clock limit), and
`--quiet` drops the live lines and keeps the summary block. To start it from
cron instead of a terminal, see
[`guide-dispatch-from-cron.md`](guide-dispatch-from-cron.md).

Exit codes of `skep run`: `0` completed, `2` refused before starting, `3`
failed, `4` waiting for an approval, `5` rejected, `6` timed out, `7` the
worker crashed. `0` means the agent finished, not that skep confirmed the
work: read the `re-verify` line.

## 6. Read the evidence

```sh
skep review <task_id>
```

<!-- from code for an external engine: src/skep/supervisor/cli_cmds.py:1302-1345. The
     field list below is the one a real builtin run printed on 2026-11-18. -->

`review` changes nothing. It prints the state, what the agent claimed
(`verification:`), the project and the dispatch and landing decisions,
`re-verify:` with the command skep re-ran and its exit code, the files changed,
and then the whole diff.

The line to read first is `re-verify:`.

| It says | Meaning |
|---|---|
| `passed [confirmed]` | The agent said its checks passed, and your pinned command passed when skep re-ran it on a clean checkout with the patch applied. |
| `failed [NOT CONFIRMED — DO NOT TRUST]` | Your pinned command failed on the clean checkout. The tail of its output is printed, and the full output is in `reverify.output.txt`. |
| `unavailable [NOT CONFIRMED — DO NOT TRUST]` | skep could not run the check (command not found, no sandbox, dependencies could not be installed). Nothing was proven either way. |
| `nothing to re-verify` | The run changed no files. |

The files behind it are in the evidence directory. A run that produced a patch
leaves:

```text
<task_id>.ndjson
<task_id>.patch
events.ndjson
result.json
reverify.profile.sb
sandbox.profile.sb
task.json
worker.log
```

`task.json` is the contract the agent was given (permissions, budget, the
dispatch decision). `sandbox.profile.sb` is the sandbox it ran in.

`skep policy decisions` prints the decision log. Know what is and is not in it:
an external agent's own tool calls happen inside the sandbox and are not logged
one by one. What skep records for such a run is the dispatch decision, the
events, the patch, the re-verification and the approval. A plain `skep run`
added no rows to the decision log when this guide was written; a refused
`--role` run added one row per question it would have asked.

## 7. Approve, and open the pull request

```sh
skep review <task_id> --approve
```

<!-- captured from a builtin-worker run on 2026-11-18; the approve path is the same for
     every engine: src/skep/supervisor/cli_cmds.py:1527-1538. -->

```text
approved: task 01a1f131-513d-7650-873b-16247605c45b (by you)
  patch applied on branch skep/01a1f131-513d-7650-873b-16247605c45b in /home/you/code/calc
  next: git -C /home/you/code/calc switch skep/01a1f131-513d-7650-873b-16247605c45b
```

The patch is now one commit on a new local branch. `main` is where it was:

```text
* main
  skep/01a1f131-513d-7650-873b-16247605c45b
```

Variations:

- `--branch NAME` lands on a branch you name. The repository's default branch
  is refused.
- `--pr` also pushes that branch to `origin` and opens a pull request with
  `gh`, with the re-verification result in the body. It needs the `gh` CLI
  logged in; without it skep prints
  `note: gh CLI not found — push the branch and open the PR manually`.
  `--pr-base` sets the base branch (default `main`).
- `--deny` rejects the patch.

<!-- --pr not run (it pushes to a remote). src/skep/supervisor/cli_cmds.py:1535-1552,
     src/skep/supervisor/github.py:89-125. -->

Approving a run that was not confirmed is allowed, and skep says so. This was
captured from a run whose pinned command failed:

```text
approved: task 01a1f131-5eeb-7c0d-85ec-8a2239d4e924 (by you)
  patch applied on branch skep/01a1f131-5eeb-7c0d-85ec-8a2239d4e924 in /home/you/code/calc
  warning: re-verification RAN and FAILED (exit codes [1]): the patch does not pass the verify command — review it before relying on it
```

## What goes wrong, and how to tell

| What you see | Cause | Fix |
|---|---|---|
| `error: project setup requires a pack or strategy` | `--engine` on a repository that is not yet a project | add `--strategy trusted_local_dev` (step 3) |
| `NO AUTH ENV — set one of …` | the credential is not in the environment `skep` was started from | step 2; under cron see the cron guide |
| `coding_engine 'claude_code' is not installed here ('claude' not on PATH)` | the agent's CLI is not on `PATH` | install it, or fix `PATH` |
| `… built-in verification is \`git diff --check\` (whitespace only), so G10 would re-run a check that cannot fail. Pin the command …` | the project has no verify command | `skep project setup ~/code/calc --verify-command "<command>"` |
| `execution mode 'sandbox' needs a usable sandbox backend and this host has none` | no bubblewrap (Linux), or the host forbids it | install bubblewrap; `skep doctor` names the reason |
| `re-verify: unavailable … verification command not found on the supervisor` | the pinned command is not installed where skep runs | install the tool, or pin a command that exists; then run the task again |
| `re-verify: failed [NOT CONFIRMED]` | the patch does not pass your command on a clean checkout | read `reverify.output.txt`; deny, or run again with clearer instructions |

<!-- The sandbox-backend refusal was not reproduced (this host has bubblewrap):
     src/skep/supervisor/dispatch.py:501-507. Every other row was reproduced. -->

The refusal for a missing verify command ends with
`next: skep run <repo> "<instructions>" --execution-mode workspace`. Ignore
that hint for an external agent: workspace mode does not apply to it, and the
fix is the verify command.

## Limits to keep in mind

- The sandbox confines writes to the run's worktree. Reads outside it are
  mostly open, apart from a list of credential paths that are hidden. The
  agent can reach its model provider, and whatever hosts the policy's default
  network lists (the `builder` baseline lists the package registries).
  Details: [`sandboxing.md`](sandboxing.md).
- skep switches the agent's own permission prompts off inside the sandbox, so
  the sandbox replaces that layer; it does not add to it.
- Re-verification is only as strong as the command you pinned.
