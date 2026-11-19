# Re-Verification

Skep treats verification as evidence, not a worker promise. A worker can report
that tests passed, but Skep independently replays the patch on a clean worktree
before presenting the run as confirmed.

## When It Runs

Re-verification runs after a worker completes and produces patch evidence. It is
recorded separately from the worker's own result so the audit trail shows both:

- what the worker claimed
- what Skep confirmed by replaying the patch

`skep status`, `skep review`, the serve API, and pull request bodies can surface
that independent result.

## What Skep Replays

For a completed run, Skep:

1. Chooses the command. The project's pinned `verify_command` comes first.
   The command list from the worker's `verify.result` event is the fallback,
   used only when the project pins nothing or the run belongs to no project.
2. Creates a fresh worktree at the commit the run started from, and records
   that commit.
3. For a `uv` command in a repository with a `pyproject.toml`, runs `uv sync`
   in that worktree first: with network access, and before the patch is
   applied, so only code already in the repository runs online.
4. Applies the worker's patch artifact. If the patch changes `uv.lock` or
   `pyproject.toml`, `uv sync` runs once more so the new dependencies are the
   ones tested.
5. Re-runs the command from the clean worktree.
6. Records the outcome, exit codes, a detail message, and which command it
   was: `[command from the project's pinned verify_command]` or
   `[command from the worker's own verify step]`.

The replay passes only `PATH` and `HOME` through from the supervisor
environment. The command runs inside the sandbox with deny-all network and
writes confined to the clean worktree; loopback ports the project opened
(`--loopback-port`) stay reachable. If the sandbox cannot be built, the command
is not run on the bare host: the outcome is `unavailable`. Each command is
bounded by the run's wall-clock budget, and never by less than 300 seconds.

To pin the command, see
[`guide-pin-verify-command.md`](guide-pin-verify-command.md).

## Outcomes

| Outcome | Meaning |
| --- | --- |
| `passed` | Patch applied and every command exited `0`. |
| `failed` | Patch did not apply, a command timed out, or at least one command exited non-zero. The tail of the output is kept, and the full output is written to `reverify.output.txt` in the run's evidence directory. |
| `unavailable` | Skep could not honestly re-run the check: the command was not installed (exit 127), the sandbox could not be built, `uv sync` failed, the worker claimed changed files but left no patch, or there was no command to run. |
| `not_applicable` | The run changed no files or produced no patch, so there is nothing to re-verify. Shown as `nothing to re-verify`, without an alarm. |

A run is `confirmed` only when the worker reported verification `passed` and
Skep's replay outcome is also `passed`. A run has one re-verification record.

## What Review Shows

`skep review <task_id>` includes the worker claim and the independent replay:

```text
task 019...
  state:        completed
  verification: passed (worker says tests passed)
  re-verify:    passed [confirmed] (G10): re-ran clean: all exit 0 [command from the project's pinned verify_command]
                re-ran ['pytest'] → exit [0]
```

If replay disagrees with the worker, review marks it loudly:

```text
  re-verify:    failed [NOT CONFIRMED — DO NOT TRUST] (G10): re-run exit codes [1] (expected all 0); full output: <evidence dir>/reverify.output.txt
```

That disagreement blocks auto-landing. You can still inspect the evidence and
approve by hand; `skep review <task_id> --approve` then lands the patch and
prints a warning that re-verification failed.

## Why This Matters

Without re-verification, a supervisor would still be trusting the agent's own
summary. Re-verification changes the trust boundary: the agent supplies a
patch, the project supplies the command, and Skep checks whether that patch
satisfies that command on a clean copy.

This catches common failures:

- the worker changed files but did not run tests
- the worker reported success after a stale or partial test run
- the patch depends on untracked local files
- the patch does not apply cleanly to the original baseline
- the worker claimed success but the replayed command fails

## Current Limits

- Re-verification is only as strong as the command. A pinned command that does
  not cover the change confirms a wrong patch. On an unpinned project the
  worker chose the command: if it recorded `true`, Skep can confirm only that
  `true` exits successfully. External engines and the `verified-patch`
  auto-landing lane refuse to work without a pin for that reason.
- Toolchain mismatch is reported as `unavailable`, not `failed`. For example, if
  the command is `pytest` but the supervisor environment cannot find it,
  Skep cannot honestly confirm the run.
- The `uv sync` step uses the network before the offline re-run. Other
  toolchains are not primed: their dependencies must already be available
  offline.
- Skep does not re-verify failed or pending runs as successful work. There is no
  completed patch claim to confirm.
- Long-running verification commands are bounded by the supervisor timeout.

For the broader run lifecycle, see [`how-it-works.md`](how-it-works.md).
