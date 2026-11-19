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
3. Takes the project's pinned `verify_command` (v88-F4); only an unpinned
   project falls back to the command list from the worker's `verify.result`
   event. The record says which of the two it re-ran.
4. Re-runs those commands from the clean worktree.
5. Records the outcome, exit codes, and detail message.

The replay uses only `PATH` and `HOME` from the supervisor environment. When the
supervisor sandbox is available, the replay command is run with deny-all network
and writes confined to the clean worktree.

Because the replay has no network, Skep first installs the project's
dependencies from the baseline's lockfile, then again from the patched lockfile
when the patch changes it: `uv` projects (`uv.lock`) and Node projects whose
pin starts with `node`, `npm`, `npx`, `pnpm` or `yarn` (`package-lock.json`,
`pnpm-lock.yaml`, `yarn.lock`). Other ecosystems are not primed: their pin
re-runs only with what the host already has.

**A patched lockfile is installed before anyone approves the patch.** That
install never gets open network: it reaches the package registries alone
(`pypi.org` and `files.pythonhosted.org` for uv; `registry.npmjs.org` and
`registry.yarnpkg.com` for Node; plus any registry host the baseline lockfile
already resolves from) through Skep's filtering proxy. Node installs run with
lifecycle scripts off and corepack offline, and the shared Node cache is
read-only while the patch's code runs. Before a patched Node lockfile is
installed, Skep checks it: every package must resolve from those hosts with an
integrity hash (npm, pnpm); no git, `github:`, URL, `user/repo` or out-of-tree
`file:`/`link:` dependency, no workspace outside the repository, no change to
`packageManager`, and no change to any `.npmrc`, `.yarnrc`, `.yarnrc.yml`,
`.pnpmfile.cjs`, `.pnpmfile.mjs`, `pnpm-workspace.yaml`, `.yarn/` or the
baseline's `yarn-path` target. A patch that fails the check is not installed
and the replay is `unavailable`, with the reason. uv still runs a package's
build step when the lockfile pins a source distribution; its network is the
same registry-only proxy.

A `node` pin in a repository that declares dependencies but has no lockfile runs
unprimed, and the detail says so; an `npm`, `npx`, `pnpm` or `yarn` pin there is
`unavailable` ("no lockfile to prime from"). A patch that adds the first
dependencies with a lockfile is primed from it (after the same check); without
one it is `unavailable`, never `failed`.

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

That disagreement blocks auto-approval, and so does a confirmed replay of a
known-vacuous check (below). You can still inspect the evidence and
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
  `true`, Skep can confirm only that `true` exits successfully. Such a run is
  never auto-landed: every auto-landing rule (`--auto-approve` included) needs
  the project's pinned `verify_command`, never the worker's own choice, and
  refuses a replay whose commands
  are all known-vacuous (`true`, `:`, `exit 0`, `test`/`[`, `echo`, `printf`,
  `ls`, `cat`, `git diff` with no flags or only `--check`, `git status`,
  `git log`, npm's `no test specified` placeholder, `sh`/`bash`/`zsh -c` of
  those, any of those behind `env` or `KEY=value`; empty), names it on the
  run's `auto_apply_blocked` record, and leaves the patch for a human. The pin
  runs through `/bin/sh -c`, so a chain joined by `&&` is vacuous only when
  every part is (`true && pytest` is a real check), and a pin containing an
  operator that can hide the exit code (`||`, `;`, `|`, a background `&`,
  backticks, `$(`) is refused for auto-landing outright. A patch that changes
  what the pin runs never auto-lands either: `conftest.py` (any directory),
  `pytest.ini`, `tox.ini`, `setup.cfg`, `noxfile.py`, `Makefile`, the pinned
  script itself, the test-runner tables of `pyproject.toml` (`[tool.pytest*]`,
  `[tool.hatch.envs]`, ...) or `package.json`'s `scripts` (a dependency bump in
  those two files still lands). Skep reads this from the patch, not the worker.
  Known-vacuous is a list of shapes, not proof that any other command can fail.
  Project setup turns the maintain phase's auto-apply default off on a missing
  or known-vacuous pin, and refuses an explicit one.
- Toolchain mismatch is reported as `unavailable`, not `failed`. For example, if
  the worker recorded `pytest` but the supervisor environment cannot find it,
  Skep cannot honestly confirm the run.
- Skep does not re-verify failed or pending runs as successful work. There is no
  completed patch claim to confirm.
- Long-running verification commands are bounded by the supervisor timeout.

For the broader run lifecycle, see [`how-it-works.md`](how-it-works.md).
