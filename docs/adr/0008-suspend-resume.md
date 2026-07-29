# ADR 0008 — True suspend/resume of pending_approval (Q8, v2)

Date: 2026-06-11 · Status: accepted

## Context

The contract froze the task-state enum at v0.1 and split Q8 in two: v1 ships the
*schema* (`pending_approval` is a state; `resume_of` and `approval_verdict` are
reserved fields), and v1 *behaviour* is stop-and-rerun — approving a suspended
task meant launching a brand-new task with `--resume-of`, which re-did all the
work and re-hit the same gate. v2 owes true suspend/resume **with zero schema
change** — the fields are already there.

## Decision

The worker is disposable and stateless, so "resume" cannot reattach to a dead
process. It means: continue the work *with the approval granted*. Approving a
suspended task dispatches a fresh worker run that carries:

- `resume_of` — the suspended task's id (the audit link, already in v1); and
- `approval_verdict` — an `ApprovalVerdict(approved=True, actor, ts)` (reserved
  at v0.1, populated now).

The worker honours the verdict: a granted verdict runs the orchestrator in a
bounded auto mode instead of manual mode, so it **proceeds past the policy gate**
that stopped the original run instead of re-stopping. The mode is deliberately
not blanket auto-approval: commit, network, and outside-workspace actions still
require explicit approval and will re-suspend — the resume is bounded, and
bounded further by the Stage A sandbox and patch-as-approval.

On the supervisor side, `review --approve` branches on state: a `completed` task
means "apply the patch" (ADR 0002, unchanged); a `pending_approval` task means
"resume". The resume inherits the original's permissions and budget from the
audited task envelope, the original's approval row is resolved `approved` with a
note linking the resume it produced, and the resumed run is re-verified (ADR
0006) like any other completion.

## Consequences

- Zero schema change: `resume_of` + `approval_verdict` + the frozen state enum
  carried it, exactly as Q8 predicted. No contract bump, no fixture regen.
- The difference from v1 is real and proven: the *same* plan that stops
  `pending_approval` with no verdict runs to `completed` with one — shown both
  with a contract worker (a benign gated action) and end-to-end through
  `review --approve`.
- Granular per-action approval is out of scope: a stateless re-run re-plans, so
  the grant is "proceed past gates (except commit/network/outside-workspace)",
  not "approve action id X". Recorded as the deliberate v2 semantics.
- `run --resume-of` (v1 stop-and-rerun) still works for the cases where starting
  over is what you want.
