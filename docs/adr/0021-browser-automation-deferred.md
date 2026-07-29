# ADR 0021 — Interactive browser automation: deferred, with named triggers (v37-F5)

Date: 2026-07-11 · Status: accepted

## Context

Hermes and OpenClaw both ship browser automation, and the project-comparison
review (2026-07-10) listed it as an open gap. skep already ships the read
half: v29's `network.read` capability gives workers governed page reading —
fetches only through the per-task domain allowlist, stdlib HTML-to-text, full
event evidence. What skep does not have is *interactive* browsing: clicking,
forms, sessions. Building it means a real browser dependency, a much larger
attack surface, and a new class of side effects to govern.

## Decision

No interactive browser automation now. `network.read` covers the
research/read use case that field tests have actually surfaced; nothing
observed so far needs page interaction.

Revisit only when a named trigger fires:

1. A Hermes-replacement field test (the v36 Stage F acceptance ritual)
   records a concrete inventoried action that cannot be governed without
   page interaction; or
2. a recurring worker task class fails that a read-only fetch cannot
   complete.

A trigger reopens the question; it does not pre-approve the feature.

## Constraints binding any future implementation

- A worker **capability** behind the domain allowlist and the policy engine,
  with full event evidence — never a free tool.
- The browser process runs inside the OS sandbox with the same egress
  enforcement as the worker itself.
- Never available to the chat Queen directly; only to dispatched workers
  under a contract.

## Consequences

The comparison page keeps an honest "no" in the browser-automation row, and
the recurring "should skep browse?" debate has a recorded answer with exit
conditions instead of a vibe.
