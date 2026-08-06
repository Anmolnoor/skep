# ADR 0051 — the provider shelf: presets, per-profile keys, no borrowed identity

**Status:** accepted (v108)
**Supersedes:** nothing. **Related:** ADR 0019 (the llm-secret exception to
G2), ADR 0011 (network allowlist proxy), ADR 0050 (one verb, three faces).

## Context

skep's provider registry (v14) validated and stored profiles nobody could
create: no CLI, REST, or chat verb wrote to it, `allowed_network_hosts` was
read by nothing, and every inference path collapsed onto the single
`llm-secret` file — a second provider's credential had nowhere to live.
Meanwhile the operator's previous assistant (Hermes, archived per v84)
reached ~28 providers by treating "provider" as data over a small set of
wire adapters. v108 adopts that shape under skep's own constraints.

## Decision

1. **Presets are data, not code** (`provider_presets.py`): one row per
   provider — registry protocol, endpoint, the NAME of its key env var, a
   default model, explicit extra egress hosts, an auth note. Two new wire
   protocols carry the rows that need them: `openai-responses` (v108-F5)
   and `bedrock` (v108-F6, hand-rolled SigV4, no boto3).
2. **Every profile can hold its own credential** (v108-F4): a 0600
   `llm-secret-<provider_id>` file beside `llm-secret` — the ADR 0019
   exception extended per profile. Resolution everywhere is the profile's
   named env var → its own file → the legacy secret. Values arrive only
   via stdin (`skep provider set-key`) or the write-only
   `PUT /api/providers/{id}/key`; never argv, never sqlite, never a GET,
   never chat.
3. **No borrowed identity.** skep ships NO OAuth client id and never
   presents another app's registration to a provider. Subscription
   providers are reachable by paste-token; `skep provider login` (v108-F8)
   runs the RFC 8628 device flow with the OPERATOR'S OWN client id. The
   one automated subscription auth is the GitHub Copilot token exchange
   (v108-F7), which needs no client id — the operator's own GitHub token
   is exchanged for a short-lived in-memory bearer.
4. **Egress stays explicit** (I5/I12): a preset's endpoint host and every
   extra host (token exchange, Bedrock's per-region control plane) land in
   `allowed_network_hosts`, which v108-F1 wired into the ONE v19-F2 merge
   (`configured_provider_hosts`). Regional hosts are enumerated — never a
   `*.amazonaws.com`. Every registration prints its egress truth (the
   voice.py pattern, I8), and profiles record provenance
   (`source=preset:<id>`).

## Consequences

- ~30 providers register with one carded/CLI/REST verb each; the health
  probe honestly reports whether the chosen model exists (I8).
- The chat floor pin moved 24KB → 24.5KB for the four registry verbs +
  two protocol enum values (test_tool_index carries the arithmetic).
- The legacy single-secret path stays as the compatibility floor
  (v19-F9): a registry-free install behaves exactly as before.

Invariants touched: I5, I6 (provider switches card), I8, I9, I12; G2 via
the ADR 0019 exception, extended; G8 (new adapters emit usage counts).
