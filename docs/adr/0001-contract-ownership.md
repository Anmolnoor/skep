# ADR 0001 — The contract owns itself (Q2-A)

Date: 2026-06-11 · Status: accepted

## Context

The supervisor and worker implementations evolve independently and meet only at a
schema: JSON task in, NDJSON events out, verified result at the end. Independent
processes connected by an unversioned schema are the classic way such systems
die. Candidate owners: the supervisor, the worker, or neither.

## Decision

Neither side owns the boundary; the boundary owns itself. A third, tiny,
dependency-light package — `agent-task-contract` (pydantic only) — holds the
Pydantic v2 models, the JSON Schema exports, the golden NDJSON fixtures, and
the version-skew helper. The supervisor and worker packages depend on it and run
its fixtures in CI.
`contract_version` rides in every envelope and every event from message one.
Schema changes: additive optional fields are a minor bump; removal/rename/
semantic change is a major bump; fixtures are regenerated on every bump.

## Consequences

- Envelope models are never redefined locally in either repo (standing guard
  rail); a drift is a CI failure, not a runtime surprise.
- The supervisor rejects dispatch on version skew with a doctor-style error
  (G5), and the worker self-rejects unsupported envelopes via `task.rejected`.
- The package must stay small: anything not needed by v1–v2 is reserved in
  spec §9, not specified.
