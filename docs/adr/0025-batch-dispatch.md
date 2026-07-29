# ADR 0025 — Batch dispatch as N independent governed runs (v51-F5)

Date: 2026-07-16 · Status: accepted

## Context

Hermes runs up to 3 parallel subagents. skep's `dispatch_run` is single;
parallel work from chat meant serial cards. The temptation was a "batch
execution model"; the decision is that no such model may exist.

## Decision

`batch_dispatch(tasks)` (cap 3) is a **submission convenience**: N
independent `dispatch_run`s submitted together to the existing thread
pool. Each task gets its own worktree, its own policy compile, its own
audit trail, and its own G10 re-verification. Workers do not know they
are in a batch.

Gating:

- One card shows every task — no hidden dispatch; one confirm/deny for
  the batch.
- Auto-resolve requires EVERY member to match its project's auto-dispatch
  policy; the first gated member names itself on the card
  (`dispatch.require_approval.batch_member_gated`, `task i/N`).

**Known v1 limitation, recorded:** no partial approval — the operator
approves all or none. Revisit if a field test wants per-task resolution.

Hermes's other half — subagents with isolated conversation contexts — is
rejected: skep workers are disposable, headless, and contract-governed;
the Queen's context IS the context.

## Consequences

Parallel work from chat with zero new execution semantics; the audit
trail reads as N ordinary runs that happen to share a birthday.
