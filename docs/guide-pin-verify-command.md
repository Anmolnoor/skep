# Guide: pin the command that proves the work

<!-- Every command and transcript in this guide was run on skep 1.4.2 (Fedora,
     bubblewrap) on 2026-11-18 with a throwaway SKEP_HOME and throwaway repositories
     under ~/.cache, using the built-in worker with no model configured (it can only
     write hello.py, which is enough to exercise re-verification). Output is pasted as
     printed, with these rewrites: temporary repository paths became /home/you/code/...,
     the temporary home became /home/you/.skep, and the interpreter path in the worker's
     own command became "python". Lines left out are marked "...". -->

This guide is for someone who has a repository under skep and wants skep's
independent check to mean something. At the end you will have a project whose
verify command is pinned, you will know how to read the four results it can
produce, and you will have seen what an unpinned project gets instead.

## Why pin it

When a worker finishes, skep applies its patch to a clean checkout and re-runs
a verification command itself. Which command?

- **Pinned:** the project's `verify_command`. You chose it. The worker cannot
  change it.
- **Not pinned:** the command the worker reported running. A worker that ran
  something trivial gets a trivial check confirmed.

The record always says which one was used.

## What you need first

- skep 1.4.2 or newer and a usable sandbox (`skep doctor` shows
  `sandbox: ready`).
- A git repository with a command that exits non-zero when the code is broken.

The examples use two small Python repositories: `~/code/notes`, whose tests
pass, and `~/code/calc`, which has one failing test.

## 1. See what setup would pin

```sh
skep project preview ~/code/notes --strategy trusted_local_dev
```

```text
preview: notes (trusted_local_dev/build)
  pack: -
  bindings: repo_path=/home/you/code/notes
  warnings: allowed_shell_commands, auto_dispatch_allowed
  dispatch: dispatch.auto_allowed.project_policy_match
  landing: landing.require_approval.project_policy_disabled_auto_apply
  seeded templates: 0
  seeded schedules: 0
  seeded shell commands: 2
    uv run pytest
    pytest
  verify command: pytest
  loopback ports: closed (--loopback-port PORT opens one)
```

`preview` saves nothing. The `verify command:` line is what setup inferred from
the repository. The rules, first match wins:

<!-- src/skep/supervisor/serve/registry.py:844-860 -->

| The repository has | and this is on `PATH` | Pinned |
|---|---|---|
| a `Makefile` with a `test` target | `make` | `make test` |
| `pyproject.toml`, tests, and `uv.lock` | `uv` | `uv run pytest` |
| `pyproject.toml` and tests | `pytest` | `pytest` |
| a `package.json` with a `test` script | `npm` | `npm test` |
| none of these | | nothing |

Nothing is pinned when nothing is detected. That is deliberate: a guess that
cannot run would fail every good patch.

## 2. Pin it

Accept the inferred command:

```sh
skep project setup ~/code/notes --strategy trusted_local_dev
```

Or name your own:

```sh
skep project setup ~/code/calc --strategy trusted_local_dev \
  --verify-command "python -m pytest -q"
```

On a project that already exists, `--verify-command` alone changes only the
pin:

```sh
skep project setup ~/code/notes --verify-command "pytest"
```

```text
saved: notes (trusted_local_dev/build)
  ...
  verify command: pytest
  loopback ports: closed (--loopback-port PORT opens one)
```

Each `project setup` creates the project's weekly maintenance schedule if it
does not exist; add `--no-seed-schedules` when you do not want it.

## 3. Check it

```sh
skep project show notes
```

```text
project notes
  name:      notes
  strategy:  trusted_local_dev
  phase:     build
  ...
      "verify_command": "pytest"
    }
```

`skep doctor` lists every project that has no pin:

```text
Advisories:
- 1 project(s) pin no verify_command, so re-verification (G10) re-runs the command the worker nominated for itself and the maintain lane will not auto-land: bare. Re-run `skep project setup <repo>` to seed one from the repo's toolchain, or set verify_command in the project policy overlay.
```

## 4. Watch it decide

```sh
skep run ~/code/notes "Write a Python hello world script" --execution-mode sandbox
```

```text
dispatching task against /home/you/code/notes
  → worker started (vcoding-minimal-0.1.0)  project: notes (trusted_local_dev/build)  dispatch: allow dispatch.allow.run_request_resolved  landing: require_approval landing.require_approval.project_policy_disabled_auto_apply
  → plan: create hello.py
  ...
  → verification: passed
  → terminal: completed
  → re-verify: passed [confirmed]  worker passed  re-ran clean: all exit 0 [command from the project's pinned verify_command]  re-ran pytest  -> exit 0
task 01a1f131-513d-7650-873b-16247605c45b
  state:        completed (1.6s)
  verification: confirmed by re-verify (G10) — re-ran pytest → exit 0
  worker claim: passed — created hello.py and verified it runs.
  evidence:     /home/you/.skep/supervisor/audit/01a1f131-513d-7650-873b-16247605c45b
  next:         skep review 01a1f131-513d-7650-873b-16247605c45b
```

The worker's own check was "run `hello.py`". skep ignored that and re-ran
`pytest`, the project's command, and the record says so:
`[command from the project's pinned verify_command]`.

`skep review <task_id>` shows the same result with the exit codes:

```text
  re-verify:    passed [confirmed] (G10): re-ran clean: all exit 0 [command from the project's pinned verify_command]
                re-ran ['pytest'] → exit [0]
```

## 5. What skep does with the command

<!-- src/skep/supervisor/reverify.py:150-380 (the re-run), :395-514 (the record). -->

1. It creates a new worktree at the commit the run started from, and remembers
   that commit.
2. If the command starts with `uv` and the repository has a `pyproject.toml`,
   it runs `uv sync` there first, **with network access and before the patch is
   applied**, so only code already in your repository runs online.
3. It applies the worker's patch. If the patch changes `uv.lock` or
   `pyproject.toml`, it runs `uv sync` once more so the new dependencies are
   the ones tested.
4. It runs your command inside the sandbox with **no network**, writes confined
   to that worktree, and only `PATH` and `HOME` passed through from skep's
   environment. Loopback ports the project opened with `--loopback-port` stay
   reachable.
5. It records the result, the exit codes, the tail of the output if the
   command failed, and which command it was.

The time limit for the command is the run's wall-clock budget, and never less
than 300 seconds.

So the command must work offline on a clean checkout. A test suite that
downloads fixtures, or needs a file that is not committed, will fail here even
when it passes for you.

## 6. The four results

| Result | Shown as | When |
|---|---|---|
| `passed` | `passed [confirmed]` | The patch applied and every command exited 0, and the worker had also reported `passed`. |
| `failed` | `failed [NOT CONFIRMED — DO NOT TRUST]` | The patch did not apply to the clean checkout, a command timed out, or a command exited non-zero. |
| `unavailable` | `unavailable [NOT CONFIRMED — DO NOT TRUST]` | skep could not run the check: the command is not installed (exit 127), the sandbox could not be built, `uv sync` failed, the worker claimed changes but left no patch, or nothing recorded a command. |
| `not_applicable` | `nothing to re-verify` | The run changed no files. |

A run is **confirmed** only when the worker said `passed` and skep's re-run
also passed.

### A worker claim that the pinned command contradicts

`~/code/calc` has a failing test. The worker wrote `hello.py`, ran it, and
reported `passed`:

```sh
skep run ~/code/calc "Write a Python hello world script" --engine builtin --execution-mode sandbox
```

```text
  → verification: passed
  → terminal: completed
  → re-verify: failed [NOT CONFIRMED]  worker passed  re-run exit codes [1] (expected all 0); full output: /home/you/.skep/supervisor/audit/01a1f131-5eeb-7c0d-85ec-8a2239d4e924/reverify.output.txt
--- re-run output (tail) ---
=========================== short test summary info ============================
FAILED tests/test_calc.py::test_add - assert -1 == 5
1 failed in 0.02s [command from the project's pinned verify_command]  re-ran python -m pytest -q  -> exit 1
task 01a1f131-5eeb-7c0d-85ec-8a2239d4e924
  state:        completed (1.6s)
  verification: passed
```

The run's state is `completed` and its exit code is 0: the worker did finish.
The `re-verify` line is where the claim was checked. `skep status --personal`
keeps listing it until you deal with it:

```text
⚠ 2 completed run(s) NOT confirmed by re-verification (G10):
  skep review 01a1f131-5eeb-7c0d-85ec-8a2239d4e924  # worker claimed passed; supervisor re-run disagreed
```

### A pinned command that is not installed

```text
  → re-verify: unavailable [NOT CONFIRMED]  worker passed  verification command not found on the supervisor (toolchain mismatch) [command from the project's pinned verify_command]  re-ran no-such-test-runner  -> exit 127
```

`unavailable` is not a verdict on the patch. Fix the pin or install the tool,
then run the task again: a run has one re-verification record, and
`skep review <task_id> --reverify` on a run that already has one answers

```text
error: already re-verified (G10): unavailable — one record per run; `skep review 01a1f131-762b-77f5-86a7-9cc9de4a8342` shows it
```

`--reverify` is for a completed run that has no record at all, because skep
stopped before it could re-verify.

## 7. What an unpinned project gets

`~/code/bare` has no test entry point, so setup pinned nothing:

```text
  verify command: none detected — G10 re-runs the worker's own verify step
```

```sh
skep run ~/code/bare "Write a Python hello world script" --execution-mode sandbox
```

```text
  → re-verify: passed [confirmed]  worker passed  re-ran clean: all exit 0 [command from the worker's own verify step]  re-ran python hello.py  -> exit 0
```

It still says `confirmed`, and it is true as far as it goes: the patch applies
to a clean checkout and the worker's command passes there. But the worker
chose the command. Read the bracket.

Two things refuse to work without a pin:

- **An external agent** (Claude Code, Codex, Aider, pi) is not dispatched at
  all:

  ```text
  error: coding_engine 'claude_code' is an external agent whose built-in verification is `git diff --check` (whitespace only), so G10 would re-run a check that cannot fail. Pin the command that actually proves the work: `skep project setup <repo> --verify-command "<command>"` (chat: setup_project verify_command=...).
  ```

- **Auto-landing of ordinary patches** does not fire; the run waits for you
  instead. See [`guide-auto-landing.md`](guide-auto-landing.md).

A run on a repository that is not a project at all also uses the worker's
command.

## What goes wrong, and how to tell

| What you see | Cause | Fix |
|---|---|---|
| `verify command: none detected` | no rule in step 1 matched, or the tool is not on `PATH` | pass `--verify-command` |
| `unavailable … command not found` | the pinned command is not installed where skep runs | install it or change the pin, then run again |
| `unavailable … could not prime the baseline uv environment` | `uv sync` failed (no network, broken lockfile) | run `uv sync` in the repository yourself and fix what it reports |
| `failed … patch did not apply to a clean worktree` | the patch depends on files that are not in the commit the run started from | commit or stash local work, run again |
| `failed` on a suite that passes for you | the suite needs the network or uncommitted files | make it pass on `git stash -u && <command>` offline |
| `[command from the worker's own verify step]` on a project you thought was pinned | the pin is missing | `skep project show <id>`; a home set up before skep inferred pins was not migrated |

<!-- The uv-prime and patch-did-not-apply rows are from code, not reproduced:
     src/skep/supervisor/reverify.py:255-270, :318-330. The other rows were reproduced. -->

## Limits

- The check is only as strong as the command. A suite that does not cover the
  change will confirm a wrong patch.
- The command runs inside the sandbox. When the sandbox cannot be built the
  result is `unavailable`; the command is not run on the bare host instead.
- More detail on every case: [`verification.md`](verification.md).
