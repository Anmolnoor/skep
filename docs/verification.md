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
3. For a `uv` or Node project, installs its dependencies from the baseline's
   lockfile in that worktree first, before the patch is applied (see the
   dependency priming below).
4. Applies the worker's patch artifact. If the patch changes the lockfile, the
   dependencies are installed once more so the new ones are the ones tested.
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

Because the replay has no network, Skep first installs the project's
dependencies from the baseline's lockfile, then again from the patched lockfile
when the patch changes it: `uv` projects (`uv.lock`) and Node projects whose
pin runs `node`, `npm`, `npx`, `pnpm` or `yarn` (`package-lock.json`,
`pnpm-lock.yaml`, `yarn.lock`), including after `VAR=value`, `env` or another
command (`cd web && npm test`). Other ecosystems are not primed: their pin
re-runs only with what the host already has.

A Node pin is primed in the directory its first Node command runs in: the
`cd X` steps before that command (`cd web && npm test`,
`cd apps && cd web && node --test`), then that tool's own directory flag
before its script arguments: `--prefix X`, `--prefix=X` or `-C X` for npm and
npx, `-C X` for pnpm (`npm --prefix web test`, `pnpm -C web test`). `node` and
`yarn` have no such flag here, and a flag after `--` belongs to the script. The
`package.json`, the lockfile, the hosts that lockfile names, the install, the
kept `node_modules` and the re-install after a patched lockfile all come from
that directory. Each part must be a literal path (letters, digits, `.`, `_`,
`@`, `+`, `-` and `/`; no `$`, `~`, glob or quote) and the result, symlinks
followed, must sit inside the worktree. Otherwise nothing is primed: an `npm`,
`npx`, `pnpm` or `yarn` pin is `unavailable` and a bare `node` pin runs
unprimed, and the detail says why. Not read: yarn's `--cwd`, pnpm's `--dir`,
`npm -w`/`--workspace`, `pushd`, a `cd` inside `sh -c`, and every Node command
after the first (a pin that runs Node in two directories primes only the
first).

**A patched lockfile is installed before anyone approves the patch.** That
install never gets open network: it reaches the package registries alone
(`pypi.org` and `files.pythonhosted.org` for uv; `registry.npmjs.org` and
`registry.yarnpkg.com` for Node; plus any registry or git host the baseline
lockfile already resolves from) through Skep's filtering proxy, so a patched
lockfile that names any other host fails to install. Node installs run with
lifecycle scripts off and corepack offline: where `yarn` or `pnpm` is a
corepack launcher, corepack reads the host's own corepack cache
(`$COREPACK_HOME`, else `~/.cache/node/corepack`) and never writes it, so
only a package manager the host already has runs. With none cached the
replay is `unavailable` and names the fix (`corepack prepare yarn@<version>
--activate`). A patch that changes the package
manager itself is not installed and the replay is `unavailable`, with the
reason: a new `packageManager`, or any change to `.npmrc`, `.yarnrc`,
`.yarnrc.yml`, `.pnpmfile.cjs`, `.pnpmfile.mjs`, `pnpm-workspace.yaml`,
`.yarn/` or the baseline's `yarn-path` target.

Registry-only holds only when the supervisor sandbox is on. With the sandbox
off, the installers are only *told* to use the proxy (through their proxy
environment); nothing stops a tool that ignores it, and the replay itself runs
unconfined on the host.

Known residual: uv still builds source distributions, and the project itself,
during the prime and the re-prime. A build step from a patched lockfile or
`pyproject.toml` therefore runs before approval, with the same registry-only
network. It cannot write re-verify's shared dependency cache: every install
after the patch applies uses a throwaway cache inside the replay and a sandbox
profile with no shared cache writable, so it downloads from cold.

The dependency caches live under the project's cache root
(`~/.skep/cache/projects/<project>/`). A sandboxed worker writes only its
`worker/` half. Re-verify primes from its `reverify/` half, which no worker's
sandbox mounts (a worker in `workspace` mode has no sandbox, so this does not
hold for it).
The replay of the patch's own commands cannot write either: it reads the Node
cache, gets a throwaway uv cache, and its packages are copies, not links into
the cache.

A `node` pin in a repository that declares dependencies but has no lockfile runs
unprimed, and the detail says so; an `npm`, `npx`, `pnpm` or `yarn` pin there is
`unavailable` ("no lockfile to prime from"). A patch that adds the first
dependencies with a lockfile is primed from it (same rules); without
one it is `unavailable`, never `failed`.

## Outcomes

| Outcome | Meaning |
| --- | --- |
| `passed` | Patch applied and every command exited `0`. |
| `failed` | Patch did not apply, a command timed out, or at least one command exited non-zero. The tail of the output is kept (from pytest's short summary on, when there is one), and the whole output, up to its last 200 KB, is written to `reverify.output.txt` in the run's evidence directory. When that output shows a failed name lookup (`EAI_AGAIN`, `ENOTFOUND`, `Temporary failure in name resolution`), the detail adds that the replay runs offline by design, so a check that installs or downloads for itself fails there. |
| `unavailable` | Skep could not honestly re-run the check: the command was not installed (exit 127), the sandbox could not be built, the dependency install failed (the detail then starts `could not install dependencies` and names the installer and its exit code; no exit code is recorded, because the command never ran) or was refused (see above), the worker claimed changed files but left no patch, or there was no command to run. |
| `not_applicable` | The run changed no files or produced no patch, so there is nothing to re-verify. Shown as `nothing to re-verify`, without an alarm. |

A run is `confirmed` only when the worker reported verification `passed` and
Skep's replay outcome is also `passed`. A run has one re-verification record.

## What Run Shows

`skep run` (and a resumed run) prints Skep's replay verdict as its
verification line once the replay ran, and the worker's own check below it
as the worker's claim (v127-T3) — an external engine's own check is only
`git diff --check`, so its "passed" is never shown as the verdict:

```text
  verification: NOT CONFIRMED by re-verify (G10) — re-ran npm test → exit 1
  worker claim: passed — codex produced a verified patch.
```

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

That disagreement blocks auto-landing, and so does a confirmed replay of a
known-vacuous check (below). You can still inspect the evidence and approve by
hand; `skep review <task_id> --approve` then lands the patch and prints a
warning that re-verification failed.

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
  `true` exits successfully. External engines refuse to work without a pin for
  that reason, and such a run is never auto-landed: every auto-landing rule needs
  the project's pinned `verify_command`, never the worker's own choice, and
  refuses a replay whose commands
  are all known-vacuous (`true`, `:`, `exit 0`, `test`/`[`, `echo`, `printf`,
  `ls`, `cat`, `git diff` with no flags or only `--check`, `git status`,
  `git log`, npm's `no test specified` placeholder, `sh`/`bash`/`zsh -c` of
  those, any of those behind `env` or `KEY=value`; empty), names it on the
  run's `auto_apply_blocked` record, and leaves the patch for a human. The pin
  runs through `/bin/sh -c`, and Skep reads it the way the shell does: after
  `;` or a newline only the last command decides the exit code
  (`uv sync; uv run pytest` and `cd web; npm test` are real checks,
  `pytest; true` is not); a chain joined by `&&` is vacuous only when every
  part is (`true && pytest` is a real check) and one joined by `||` when any
  part is (`pytest || true`); only the `-c` argument of `sh`/`bash`/`zsh` is
  read as a nested command (`python -c "import app; app.check()"` is a real
  check). A pipe `|`, a background `&`, backticks or `$(` anywhere is refused
  for auto-landing outright. A patch that changes
  what the pin runs never auto-lands either. Any touch of a test-runner config
  file blocks it: `conftest.py`, `pytest.ini`, `.pytest.ini`, `pytest.toml`,
  `.pytest.toml`, `tox.ini`, `setup.cfg`, `setup.py`, `noxfile.py`, `Makefile`,
  `.coveragerc`, `.npmrc`, `.yarnrc*`, `jest.config.*`, `vitest.config.*`,
  `vitest.workspace.*`, `vite.config.*`, `.mocharc*` (in any directory); so does
  any file a word of the pin names (`uv run python scripts/check.py` makes
  `scripts/check.py` a control file), and any file named by the `package.json`
  script the pin runs (`npm test` → `scripts.test` → `node test/run.js`).
  `pyproject.toml` blocks when its `[tool.pytest*]`, `[tool.coverage*]`,
  `[tool.tox]`, `[tool.nox]`, `[tool.poe]`, `[tool.hatch.envs]`,
  `[tool.pdm.scripts]`, `[project.scripts]`, `[project.gui-scripts]`, any
  `[project.entry-points.*]` table or `[build-system]` change (a console script
  named `pytest` would replace the tool the pin runs);
  `package.json` blocks on any change other than its dependency lists,
  `overrides`, `resolutions` or `version`, so a dependency bump still lands.
  Skep reads this from the patch, not the worker. This blocks global switches
  of the test setup, not test edits: a patch that edits the test files
  themselves still auto-lands if its own tests pass. Re-verification proves
  the pin passes on the patched tree, not that the patch is good.
  Known-vacuous is a list of shapes, not proof that any other command can fail.
  Every save face (setup, preview, phase change, the web policy editor) turns
  an auto-apply it did not ask for (a phase default or a stored value) off on
  a missing or known-vacuous pin, with a note, and refuses a request that
  itself turns auto-apply on with such a pin.
- Toolchain mismatch is reported as `unavailable`, not `failed`. For example, if
  the command is `pytest` but the supervisor environment cannot find it,
  Skep cannot honestly confirm the run.
- The dependency install (uv and Node) reaches the package registries before
  the offline re-run. Other toolchains are not primed: their dependencies must
  already be available offline.
- Skep does not re-verify failed or pending runs as successful work. There is no
  completed patch claim to confirm.
- Long-running verification commands are bounded by the supervisor timeout.

For the broader run lifecycle, see [`how-it-works.md`](how-it-works.md).
