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

1. Creates a fresh worktree at the original repo baseline.
2. Applies the worker's patch artifact.
3. Reads the verification command list from the worker's `verify.result` event.
4. Re-runs those commands from the clean worktree.
5. Records the outcome, exit codes, and detail message.

The replay uses only `PATH` and `HOME` from the supervisor environment. When the
supervisor sandbox is available, the replay command is run with deny-all network
and writes confined to the clean worktree.

## Outcomes

| Outcome | Meaning |
| --- | --- |
| `passed` | Patch applied and every recorded command exited `0`. |
| `failed` | Patch did not apply, a command timed out, or at least one command exited non-zero. |
| `unavailable` | Skep could not honestly re-run the check, usually because there was no patch, no recorded verification command, or the command was not installed. |

A run is `confirmed` only when the worker reported verification `passed` and
Skep's replay outcome is also `passed`.

## What Review Shows

`skep review <task_id>` includes the worker claim and the independent replay:

```text
task 019...
  state:        completed
  verification: passed (worker says tests passed)
  re-verify:    passed [confirmed] (G10): re-ran clean: all exit 0
                re-ran ['python -m pytest'] -> exit [0]
```

If replay disagrees with the worker, review marks it loudly:

```text
  re-verify:    failed [NOT CONFIRMED - DO NOT TRUST] (G10): re-run exit codes [1]
```

That disagreement blocks auto-approval. You can still inspect the evidence and
decide what to do manually, but Skep will not treat the run as safe to land by
policy.

## Why This Matters

Without re-verification, a supervisor would still be trusting the agent's own
summary. Re-verification changes the trust boundary: the agent supplies a patch
and a command, but Skep checks whether that patch actually satisfies that command
on a clean copy.

This catches common failures:

- the worker changed files but did not run tests
- the worker reported success after a stale or partial test run
- the patch depends on untracked local files
- the patch does not apply cleanly to the original baseline
- the worker claimed success but the replayed command fails

## Current Limits

- Re-verification is only as strong as the recorded command. If a worker records
  `true`, Skep can confirm only that `true` exits successfully.
- Toolchain mismatch is reported as `unavailable`, not `failed`. For example, if
  the worker recorded `pytest` but the supervisor environment cannot find it,
  Skep cannot honestly confirm the run.
- Skep does not re-verify failed or pending runs as successful work. There is no
  completed patch claim to confirm.
- Long-running verification commands are bounded by the supervisor timeout.

For the broader run lifecycle, see [`how-it-works.md`](how-it-works.md).
