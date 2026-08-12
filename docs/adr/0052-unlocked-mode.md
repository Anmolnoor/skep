# 0052 — unlocked: the per-project zero-ask rung of the trust ramp

## Status

Accepted (unlocked-F1…F6, branch `unlocked`).

## Question

The operator asked for a mode where skep stops asking for permissions,
justified by "everything runs in the sandbox". I6 forbids exactly one shape
of that wish — a global don't-ask toggle (v23-F6 deprecated one; v81-F14
made the leftover setting inert) — and blesses another: the per-project
trust ramp the operator explicitly climbs. What is the top rung's shape,
and where does its honesty end?

## Decision

One boolean project-policy key, `unlocked`, ungroupable like every other
trust-ramp key (ADR 0048). Its scope is exactly what the walls actually
contain — the worker path end to end — and nothing else:

| Ask | Under unlocked | Mechanism |
|---|---|---|
| Dispatch-family cards (dispatch_run, run_code, quick_edit, batch members) | run in-turn | `project_policy_dispatch_decision` returns `dispatch.auto_allowed.unlocked`; the born-resolved row (v61-F1) and receipt (v90-F3) record it |
| Worker escalations (shell, capability, network host) | answer themselves | the same `resume_past_gate` path a click takes, actor `auto:unlocked`, granted ONCE — never the session or durable tiers |
| Patch landing | zero clicks | unlocked implies `auto_apply_verified_patch` (RSoP provenance `unlocked`); the untouched v90-F4 lane — G10-confirmed with the PINNED verify_command, onto `skep/*` only |
| Outbound content (open_pr, merge_pr, pushes, messenger sends) | still cards | ADR 0044: cards by mechanism; the sandbox argument does not cover what leaves the machine |
| Policy/trust mutations (setup_project, set_operator_policy, promote_skill_pack, …) | still card | otherwise the model widens its own cage — the thing I6 exists to prevent |
| Queen host shell (run_shell, start_process) | unchanged gates | the Queen's shell runs on the REAL host, not in a sandbox — unlocked's justification simply does not apply |
| Hard git denies, sudo, catastrophic floor | still denied | I4: no override reaches them; they are denies, not asks |
| Explicit run overrides (engine=, network=, budgets) | still card | v95-F3 (I6/I7): an explicit model choice gets eyes; the worker can still earn what it needs via recorded escalation |

**Preconditions, enforced twice.** `unlocked=true` refuses to SAVE without
a pinned `verify_command` (I2 — the only lane that lands without a human
refuses worker-nominated verifies) and a usable sandbox (I12). The dispatch
path re-checks fail-closed: the resolver coerces every unlocked run to
sandbox mode (the v94-F4 shape), `run_task` refuses mismatches and a
sandbox that broke since the policy was saved, and the spawner learned
required-vs-best-effort — `sandbox_enabled=True` raises instead of the
silent naked-host fallback that had been there since v94.

**Every auto verdict is a ledger row (I8, I13).** `auto:unlocked` on
escalation gates, `auto:verified-patch` on landings,
`dispatch.auto_allowed.unlocked` as `decided_by` on born-resolved cards.
The policy strip renders a standing `UNLOCKED` pill, and `skep doctor`
names every unlocked project. "Nothing asked" must always read as the mode
the operator enabled, never as "nothing happened".

## The trade stated out loud

Auto-granting network-host escalations means a worker can buy itself egress
to any host it asks for — each grant a recorded approval row, but nobody
pre-approved the host. Two things keep that read-mostly in practice: the
G2 env allowlist still strips credentials (an agent that can reach an API
cannot authenticate to it), and nothing it does inside the worktree reaches
a branch without G10 + the skep/* landing rule (I1). An operator who wants
zero-ask *except* egress turns unlocked off and uses the per-host
allow-and-resume lane instead.

## Consequences

- The dogfood loop on a trusted repo collapses to: say the task, read the
  landed diff on `skep/<task_id>`. Every decision skep made on the way is
  in the approvals ledger and the chat receipts.
- A phase change re-derives policy and drops `unlocked` (like every
  non-carried key) — the mode fails toward asking, which is the safe
  direction; re-enable it explicitly.
- `main` never advances, G10 never trusts a worker's word, and the guard
  list did not move: I1, I2, I4, I5, I6, I7, I8, I12, I13 all hold by
  construction, not by promise.
