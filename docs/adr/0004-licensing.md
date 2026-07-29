# ADR 0004 — Licensing (G1, as corrected)

Date: 2026-06-11 · Status: superseded on the license choice (2026-07-29, see
note below); the worker-contract boundary decisions remain in force

> **Superseding note (LAUNCH-1-L1, 2026-07-29).** skep is now
> **MIT-licensed**. AGPL was chosen below for its copyleft-over-a-network
> property; MIT is chosen now for adoption — the operator is the sole author,
> so no contributor agreement is disturbed. The decision of record is master
> plan D1. Everything in this ADR about the worker contract boundary and
> no-worker-source-imports still stands. Ported Apache-2.0 code (e.g. buzz
> CSS, when v102-C lands) keeps its license, with `NOTICE` as the
> attribution — Apache code may live in an MIT project; it cannot be
> relicensed by porting.

## Context

Skep combines supervisor code, worker-contract models, and subprocess worker
integrations. The repository intentionally uses a copyleft license for the
supervisor and a narrow contract boundary for worker interoperability.

## Decision

- **skep: AGPL-3.0-or-later** — a conscious choice for the supervisor and
  dashboard. AGPL §13's network-service terms apply if this is ever hosted;
  revisit before any public hosted offering.
- **Worker contract boundary** — worker implementations interact with Skep
  through JSON task envelopes, NDJSON events, and result files. The process
  boundary keeps worker implementation licensing separate from supervisor code.
- **No worker source imports** — do not import external worker implementation
  source into this repository just to supervise it. Add an adapter process
  instead.

## Consequences

- The subprocess bridge is load-bearing for licensing, not just isolation.
  Replacing it with an in-process import of an external worker implementation
  needs a licensing re-review.
- Contributions to this repository land under AGPL-3.0-or-later.
