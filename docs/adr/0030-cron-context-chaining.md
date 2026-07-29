# ADR 0030 — Cron context chaining (v53-F5)

Date: 2026-07-17 · Status: accepted

## Context

Every tick was fire-and-forget: a disk-check script's finding could not
reach the morning digest; composing them meant one monolithic script or
hand-copying output. Hermes chains cron jobs; the operator asked for the
same.

## Decision

1. **`chain` names a source schedule; `last_output` is what a tick
   produced.** Synchronous castes only record output (note: its text;
   script: its stdout+stderr; digest: the composed summary — all capped
   at 4KB). A task-caste schedule can CONSUME a chain but not source one:
   its output lands after the tick, asynchronously (recorded bound; a
   post-run hook is future work if a field test wants it).

2. **Context, never instructions.** The chained output arrives labeled —
   `[Context from schedule 'X']:` — before the schedule's own
   instructions (task castes get the explicit `[Your instructions]:`
   separator; scripts get it as stdin: data into an operator-vetted
   command, never a new command). The memory-injection posture, applied
   to ticks.

3. **Acyclic, depth ≤ 3, validated at creation.** `validate_chain` walks
   the chain at every creation surface (POST /api/schedules and the
   carded propose_schedule); a cycle, an unknown source, or a fourth
   level is a 400/tool error with the reason. Deeper chains are a sign
   the user should write one script.

4. **The per-job `model` override from the draft plan was CUT** (the v53
   review): three of four castes would store it unused, and it existed
   only for a hypothetical future conversation caste — stored-but-unused
   config is against the house YAGNI law (the v42 caste scar). It
   arrives WITH that caste, if that caste ever ships.

## Consequences

- `script A → digest B` works out of the box: B's digest opens with A's
  latest finding. Trust is unchanged — chaining moves DATA between
  schedules the operator already created through gated surfaces; the
  tick-time policy gates on dispatching castes are untouched.
- `schedule_view` (asdict) exposes `chain` and `last_output` on every
  schedule surface for free.
