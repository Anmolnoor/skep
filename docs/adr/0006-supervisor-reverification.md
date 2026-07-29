# ADR 0006 — Supervisor-side re-verification (G10, v2)

Date: 2026-06-11 · Status: accepted

## Context

v1 recorded a deliberate trust gap (G10): the run record's verification outcome
was whatever the worker *reported*. A buggy or dishonest worker could claim
`completed` / `passed` and the supervisor would believe it. The patch-as-approval
model (ADR 0002) means a human reviews evidence before applying — but "review
evidence, not promises" is hollow if the central piece of evidence (did
verification actually pass?) is itself an unchecked promise.

## Decision

After a run the worker reports `completed`, the supervisor re-verifies it
**independently** (`skep/supervisor/reverify.py`):

1. Create a fresh, clean git worktree at the same baseline (`repo@ref`).
2. `git apply` the worker's patch artifact to it.
3. Re-run the worker's *own recorded* verification command(s) under the Seatbelt
   sandbox (deny-all network, writes confined to the throwaway worktree).
4. Compare exit codes: all zero ⇒ `passed`; any non-zero ⇒ `failed`; command not
   found (127) ⇒ `unavailable` (the supervisor lacks the toolchain — it cannot
   honestly confirm or deny).

The worker surfaces the verification command(s) in the `verify.result` event
payload (`commands: list[str]`). The contract `Event` keeps unknown payload keys
verbatim, so this is **additive — not a contract change**, and no golden fixture
regenerates.

The result is stored in its own `reverifications` row alongside the worker's
claim, with `confirmed = (worker said passed AND re-run passed)`. The canonical
run state stays exactly what the worker reported; re-verification is a *parallel,
equally-durable* supervisor judgment. When they disagree, `status` and `review`
say so loudly ("NOT CONFIRMED — DO NOT TRUST"), and (ADR 0007) auto-approval
refuses to fire.

## Why not a "validator" agent

The decision record was explicit: the validator is a ~plain function, not a
third agent. Re-verification trusts only the exit code of a command the worker
already recorded — there is nothing for an LLM to decide. Adding an agent would
re-introduce the very trust problem it exists to close.

## Consequences

- The G10 gap is closed for `completed` claims with a patch and a recorded
  command. A lying worker (claims passed, patch fails the command) is caught —
  proven by a `MODE:liar` fake worker and by the real worker re-running
  `pytest` on a clean worktree.
- Re-verification reuses the Stage A sandbox, so the supervisor's own re-run is
  itself network-denied and write-confined.
- Honest limits: re-verification fidelity assumes the supervisor shares the
  worker's toolchain (true in v2 — co-located on one Mac, G3); a missing tool is
  reported as `unavailable`, never a false `failed`. It does not re-verify
  `failed`/`pending_approval`/no-patch runs (nothing was claimed verified).
