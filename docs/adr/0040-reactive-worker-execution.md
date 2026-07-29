# 0040 — Bounded reactive worker execution (R2, v69)

## Status

Accepted (planned v69; invariants doc R2).

## Question

The coding worker commits to a complete plan before seeing any output.
The field record is unambiguous about where that dies: verify commands
chosen blind (three runs), plans that read but never write (the v68
hollow pass), and a repair-pass lineage (v19-F7 → v59-F5 → v64-F1 →
v68-F1) that patches one failure class per version. Should the worker
instead act and observe in a loop — and can it do so without weakening
a single guard?

## Decision

A second planning protocol, `react`, beside the existing `plan`:

- **The loop.** Each round, the provider returns ONE next action
  (`{"action": {"tool", "args"}}`) or a final block
  (`{"done": {"summary", "verify"}}`). The action executes through the
  SAME `CapabilityRegistry.invoke` as plan steps — every deny, guard,
  approval gate, and event applies unchanged (I4, I5). The result
  (exit code, output tail, error text) is appended to the conversation
  and the model decides the next action seeing it.
- **Bounds.** The existing budgets bind: `max_iterations` caps rounds,
  `max_provider_calls` caps model calls, `wall_clock_seconds` caps the
  run. A malformed action is fed back for repair on the shared
  v59-F5 counter. An all-reads trace at `done` fails exactly like a
  hollow plan (v68-F1) — observation is not work in either protocol.
- **The trace is the record (I8).** Every step already emits
  `command.start`/`command.result` through the registry; at terminal,
  a `plan.created` event carries the REALIZED trace, so the audit
  trail keeps one shape across both protocols.
- **Verification and landing unchanged (I1, I2).** The final block
  names the verify command (or the last executed step is
  purpose=verify); the run's verify gate and the supervisor's G10
  re-verification are byte-identical to the plan path, and landing
  remains patch-as-approval.
- **Approvals suspend the loop (I6).** A step that raises the approval
  gate suspends the run exactly like today, with the conversation and
  step cursor checkpointed (the resume_checkpoint plugin, extended);
  approval resumes the loop where it stopped. The model never holds
  the trigger — a gated step waits for the human in both protocols.
- **Selection is a policy ramp, not a flip.** `planning_protocol` is a
  per-task contract field (default `plan`), set from a project policy
  knob (`worker_protocol`). The default stays `plan` until the field
  record says otherwise; trusted build-phase projects opt in first.
  The `plan` protocol is not deprecated by this ADR.

## Consequences

- The plan stops being a contract and becomes a trace; repair passes
  stop being the only feedback channel; mid-run steering (R12a) gets
  its natural seam (a steering note is one more observation between
  rounds).
- Cost: more provider calls per run (one per action instead of one per
  plan). Budgets already price this; `max_provider_calls` is the knob.
- Risk: a wandering loop. Bounded by iterations, wall clock, the
  hollow rule, and the v59-F7-style repeat-read nudge carried over.
- The worker contract gains one optional field; version bumps minor.
  Old workers ignore it and plan as before.
