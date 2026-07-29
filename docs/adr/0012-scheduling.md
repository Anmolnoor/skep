# ADR 0012 — Recurring schedules: cron drives a stateless tick (v3)

Date: 2026-06-11 · Status: accepted

## Context

U1 (the nightly dependency/audit bot) needs *recurring* dispatch — "a cron job
checks my GitHub projects." v3 has to own schedule definitions (which repo, which
caste, how often, with what network scope) and decide what's due. The open
question was the trigger: should skep run a long-lived scheduler daemon, or be
invoked?

## Decision

**skep is not a daemon.** It persists schedule definitions in the (single-writer,
G4) store and exposes a stateless `skep tick` that dispatches every schedule whose
`next_run_at` has arrived, then advances each one. An external `cron` / `launchd`
entry supplies the wakeup.

- *Why not a daemon.* A daemon is a process to supervise, restart, and reason
  about for a personal tool that already leans on the OS for everything else. cron
  is battle-tested, observable, and already how the user thinks about "nightly."
  The decision record literally frames U1 as "a cron job checks my projects" —
  matching that is the least surprising design.
- *Recurrence model.* A fixed interval (`30s`/`5m`/`2h`/`1d`) with a stored
  `next_run_at`. On a tick, due schedules dispatch and `next_run_at` advances to
  `tick_time + interval` (scheduled from the tick, not the prior target, so a
  missed window never produces a catch-up storm). A full cron expression was
  deliberately not built — interval covers "nightly" and is trivial to reason
  about; calendar scheduling can arrive later additively.
- *One spine.* `run_due` dispatches through the *same* `run_task` as a manual run,
  sharing the single-writer store. So a scheduled run inherits the entire boundary
  for free: contract envelope, Seatbelt sandbox, D1 network allowlist, G10
  re-verification, and (when rules are configured) D3 auto-approval. Scheduling
  added no parallel dispatch path — it is just an automated caller of the spine.

## Consequences

- The "nightly" half of U1 works today: `skep schedule add … --every 1d --caste
  audit` then a cron line `*/… skep tick`. Proven end-to-end — a due audit
  schedule dispatches, the run completes, G10 confirms it, and the schedule
  advances exactly one interval.
- A broken schedule (bad repo, dispatch error) is recorded and the schedule still
  advances, so one bad entry never hot-loops or aborts the rest of the tick.
- No always-on process, no scheduler state machine to crash. The cost: resolution
  is bounded by how often cron calls `tick`, and a machine that's asleep at the
  scheduled minute runs late (acceptable for a personal nightly bot; a daemon
  wouldn't help an asleep machine either).
- Per-schedule budgets and calendar (cron-expression) scheduling are recorded as
  additive follow-ups, not silently implied.
