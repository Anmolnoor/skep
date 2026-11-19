# ADR 0066 — new projects run sandboxed when the host can sandbox

**Status:** accepted (v127-F8)
**Related:** ADR 0047 (CLI-agent engines: the sandbox is their only wall),
ADR 0061 (unlocked mode forces sandbox).
Invariants touched: I4, I8, I12.

## Context

The 2026-11-18 store copy had 49 coding runs; 41 ran `workspace`, with no
sandbox. Nobody chose that: every first-party strategy default in
`_STRATEGY_DEFAULT_POLICY` (`projects.py`) says `default_execution_mode:
workspace`, so every project setup wrote it and every run inherited it. The
builtin worker's capability layer still gated each side effect, but reads
outside the worktree, a mistaken allowlist entry, or a capability bug had
nothing behind them.

## Decision

1. **A NEW project gets `default_execution_mode: sandbox` when
   `sandbox.availability()` says this host can enforce a profile.** When it
   cannot, the project gets `workspace` and the setup result carries the
   probe's reason and the fix (`skep doctor`, then
   `skep project setup <repo> --execution-mode sandbox`). Applied in one
   helper (`registry._default_execution_mode`) at setup, preview and
   `set_project_phase`, so every face (CLI, REST, chat, `/workon`, first-run)
   writes the same thing.
2. **Not in `_STRATEGY_DEFAULT_POLICY`.** Pack manifests are built from that
   table (`packs.py`), and a host-dependent value does not belong in a
   manifest. A pack that already says `sandbox` is never downgraded.
3. **An existing project's stored mode is carried forward, never
   rewritten** — by a re-setup, a phase change, or a re-probe. Before v127,
   `_carry_forward_existing` let the strategy default win over the stored
   mode on re-setup, and `set_project_phase` re-derived it from the phase
   defaults; both now keep what was stored. An explicit mode (CLI
   `--execution-mode`, `policy_overrides.default_execution_mode`) always wins.
4. **`skep doctor` names the projects still on `workspace`** with the one
   command that moves one, so the operator migrates on purpose.
5. **A sandbox that later stops working fails closed.** The dispatch
   chokepoint (ES-F1, v94-F4) refuses a `sandbox` run the host cannot
   confine; a scheduled run records it as the schedule's outcome
   (`dispatch_error: execution mode 'sandbox' needs a usable sandbox …`).
   It is never silently run unconfined.

## Consequences

- New projects pay the sandbox's costs: writes outside the worktree and
  unlisted hosts are refused at the OS layer, and toolchains that write to
  `~` need their cache paths. That is the point.
- Existing projects do not change until the operator runs the doctor's
  command. The live `~/.skep` is untouched by this release.
- The test suite pins the probe off (`tests/conftest.py`) so no test's
  expectation depends on the machine; the default itself is tested with the
  probe patched both ways.
