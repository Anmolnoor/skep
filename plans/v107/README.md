# v107 — the kept worktree: iterate in place, diagnose in place, verify in place

The first plan on the public tree (the L10 split retired the private `plans/`
history; the convention continues here). Forensic base: the 2026-08-03
dogfood arc — runs `019fc70f`, `019fc719`, `019fc724`, `019fc72c`,
`019fc74a` on this store, and the acceptance log kept beside the session.

## The observed failures

1. **Same work over again.** A failed or unconfirmed external-engine run
   tears down its worktree; the retry pays the whole toolchain cost again
   (`yarn install` ≈ minutes per attempt across `019fc70f→019fc724`; five
   attempts, five cold workspaces). Crash-shaped runs already keep their
   tree and resume in place (v72-F8) — failed and completed-but-unconfirmed
   runs, the shapes the dogfood arc actually produced, do not.
2. **The operator diagnoses blind.** When `019fc74a` landed unconfirmed
   (re-verify exit 1), the only way to ask "which test failed, show me" was
   a terminal — the Queen could read the record but not run one bounded
   command in the kept evidence. `run_shell` deliberately refuses repo
   cwds (`tools.py:2404`), and nothing else fills the gap.
3. **G10 cannot confirm skep-shaped work.** Re-verify of `019fc72c` ran the
   pinned `uv run pytest` to completion and failed honestly (exit 1) — but
   for the wrong reason: inside the sandbox `TMPDIR` is unset, Python falls
   back to `/tmp`, and the nested bwrap that skep's own tests spawn masks it
   (the CLAUDE.md TMPDIR rule, reproduced inside the wall). Verified
   2026-08-03: with TMPDIR moved off `/tmp`, the ENTIRE suite passes inside
   `--unshare-net`. The worker's "blocked by network isolation" report was a
   plausible misdiagnosis — bwrap's `loopback_setup()` already brings `lo`
   up (empirically confirmed: 127.0.0.1 bind/listen/connect work in-sandbox).

## Invariants touched

I1 (landing unchanged — kept trees never bypass the patch gate), I2 (F3
makes the pin *confirmable* for socket-suite repos; nothing starts trusting
worker reports), I3+I6 (F2 is a carded side effect, human-resolved, never
auto-allowed), I5 (F2 executes through the existing sandbox profile
machinery, no private allow-logic), I8 (kept/swept states are recorded and
the TTL sweep cannot strand a lie), I9 (every new refusal names what would
work), I12 (TMPDIR moves *inside* the wall; no wall weakens), I10 (all
three fixes anchored above and reproduced live).

Checklist answers: no step reaches a branch without a verdict (F1 resumes
still exit through patch→approval); no self-report becomes a verdict; the
Queen gains a *carded* side effect only; no grant touches the git guards;
no private permission logic; no timeout/model output confirms anything
(diagnose cards deny on timeout like every card); no surface shows false
state (worktree list already exists, `list_worktrees` stays truthful).

---

## F1 — failed and unconfirmed runs keep their worktree; a TTL sweeps them

**Root cause anchors.** The keep decision is two twin blocks
(`dispatch.py:648-652` live, `:866-870` recovery) gated on
`_RESUMABLE_CRASH_STATES` (`dispatch.py:183`) + checkpoint ≥2; the keep-set
query `crashed_run_workspaces` (`store.py:3281-3292`) spares only
crashed/timed-out states; `actions.py:2276` holds a second, independent
copy of the state list. For COMPLETED runs the removal at `:652` fires
*before* `reverify_run` writes `confirmed` — the keep answer is unknowable
at the decision point.

**Change.**
- `dispatch.py`: add `"failed"` to the resumable states (rename the tuple
  `_RESUMABLE_STATES`); for `completed` runs, do NOT remove the worktree at
  the terminal block — re-verify runs first, then the same block removes it
  only when the fresh reverification is `confirmed`; unconfirmed/failed
  keep the tree. Recovery path (`:866`) mirrors it.
- `store.py`: widen `crashed_run_workspaces` → `preserved_run_workspaces`:
  crashed/timed-out/failed without successor, plus completed runs whose
  reverification row is absent or `confirmed=0`.
- TTL: `preserved_run_workspaces(max_age_seconds=…)` excludes rows with
  `updated_at < cutoff` (the `pending_cards_older_than` pattern,
  `store.py:3692`); module constant `PRESERVED_WORKTREE_TTL_SECONDS =
  86_400`. The ticker (`serve/ticker.py:230`) gains a guarded step calling
  `cleanup_orphans` so expiry actually collects (today nothing sweeps after
  the recovery path at all — `dispatch.py:897`).
- `actions.py:2276`: the duplicate state set imports dispatch's, and
  `resume_crashed_run` accepts the widened states; for runs with no
  salvaged checkpoint (every external engine), resume means: fresh
  instructions, same preserved worktree, `resume_of` recorded — the
  existing `_resume_workspace` reuse path (`dispatch.py:402-417`), minus
  the checkpoint gate for the no-checkpoint case. Tool description
  (`tools.py:787`) retold accordingly (I9).

**Tests.** Extend `test_crash_resume.py`: a `failed` run keeps its tree and
is resumable in place; a completed-unconfirmed run's tree survives the
post-reverify sweep while a confirmed one's does not; a TTL-expired
preserved tree is collected by the ticker step; the two state sets are one
object.

## F2 — `diagnose_run`: one bounded, carded command in the kept evidence

**Anchors.** Carded-verb template: spec in `MUTATING_TOOL_SPECS`
(`tools.py:650`), arm in `_execute_mutation` (`tools.py:3212`), card text
via `cards._SHELL_TOOLS` (`cards.py:58`), parity gate
(`test_surface_parity.py:259`) satisfied by an operator face in
`serve/app.py`. Execution shape: `reverify._run_command` (`reverify.py:83`)
— `/bin/sh -c` + `sandbox.wrap_command` + timeout.

**Change.** New `actions.diagnose_run(store, config, task_id, command,
timeout_seconds)`: resolve `get_run(task_id).workspace`; refuse (teaching
the alternative — I9) when absent/swept ("worktree swept — dispatch a fresh
run; preserved worktrees live {TTL}"); write a DENY_ALL_NETWORK profile
over the kept worktree (`git_metadata_writable_roots`), run the command,
return exit code + output capped at `RUN_SHELL_OUTPUT_CAP` (10k). Tool spec
`diagnose_run(task_id, command, timeout_seconds≤600, default 120)`;
category `runs`; **no** `mutation_execution_decision` arm — it always
cards (I6); `cards._SHELL_TOOLS += diagnose_run` so the card shows the
command verbatim; REST face `POST /api/runs/{task_id}/diagnose` (parity).
Not in the command deck for now (chat + REST are the two faces).

**Tests.** Card flow (proposed → operator confirm → output in chat), the
swept-worktree refusal text, the sandbox profile is DENY_ALL (a command
that curls fails; one that reads workspace succeeds), parity gate passes
with no CHAT_ONLY entry, timeout cap enforced.

## F3 — TMPDIR lives inside the wall

**Anchors.** `_toolchain_env` (`dispatch.py:207-225`) sets npm/engine homes
but not TMPDIR; worker env baseline is PATH+HOME (`spawner.py:56-60`);
reverify env is PATH+HOME only (`reverify.py:292`). Empirical: suite green
in-sandbox with TMPDIR off `/tmp`; ~40+ failure cluster reproduced with
TMPDIR=/tmp.

**Change.** `_toolchain_env` adds `TMPDIR=<workspace>/.toolchain/tmp`
(created with the others, patch-excluded, swept with the tree). Reverify
builds `TMPDIR=<its worktree>/.reverify-tmp` into both prime and verify
env. Seatbelt note recorded, not fixed: macOS `(deny network*)`
(`sandbox.py:292`) blocks loopback binds — one-line allow, but unverified
without a Mac; seed for a macOS field test (I10 forbids fixing it blind).

**Tests.** Toolchain env pins TMPDIR (extend the F1/envdump dispatch test);
reverify env unit-test pins TMPDIR inside the worktree; the slug-pin
reverify test keeps passing (no /tmp reliance).

## Acceptance (live, after all three land)

Dispatch a real skep-repo dogfood run (claude_code): it completes, G10
re-runs `uv run pytest` in-sandbox and — for the first time —
**confirms**. Then `diagnose_run` on the kept worktree of an earlier
unconfirmed run returns a real test output through a card. Then resume a
deliberately-failed run in place and watch it skip the cold install.
