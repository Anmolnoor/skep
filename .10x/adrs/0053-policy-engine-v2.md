# ADR 0053 (draft) — policy engine v2: rules and knobs in one layer stack

**Status:** draft (`.10x/adrs/`); house number 0053 on acceptance into `docs/adr/`.
**Feature:** `policy-engine-rewrite`, phase P1. Depends on ADR 0052 (P0).
**Related:** ADR 0022, 0044, 0047, 0048, 0050. Invariants I4, I5, I8, I9, I12.

## Context

ADR 0022 unified the *rule* vocabulary (scope · action · pattern → verdict)
but left the *knobs* (budgets, modes, engine, allowlists, the trust ramp)
in a second system with its own composition (`run_policy_for_repo`,
ADR 0048 groups). The operator's four complaints — two vocabularies, no
inheritance model, nothing to see or edit, wrong enforcement point — all
trace to that split. P0 enumerated the keys and froze today's answers.

## Decisions

### D1 — one schema, two setting kinds

Every setting is either a **rule** (`scope`, `action`, `pattern`,
`verdict`) or a **knob** (a typed value from the registry). Both live in
the same document shape, in the same layers, resolved by one engine.

### D2 — one layer stack, in this order

floor → supervisor defaults → template → phase → groups (attach order) →
project → session → dispatch. Higher wins, except where a lower layer has
**enforced** the setting (D4). This is today's `run_policy_for_repo` order
with `template` and `session` made explicit layers (today a template's
knobs ride dispatch args and session grants live in a separate settings
key). ADR 0048's merge rules survive as the engine's list-knob semantics:
list knobs union across layers; scalar knobs take the highest layer.

### D3 — tri-state per setting

Each layer may leave a setting **not configured** (inherit), **set** it,
or **explicitly clear** it (stop inheritance; the engine's hardcoded
default applies and RSoP says "cleared at <layer>"). Clearing is how an
operator turns a group's host list off for one project without editing
the group. Today's documents have no "cleared"; the migration reads every
absent key as not configured.

### D4 — "enforced" replaces the special-case key lists

A setting set with `enforced: true` at a layer cannot be overridden by any
lower layer; attempts are refused at write time naming the enforcing layer
(I9). The trust ramp (`auto_apply_verified_patch`, `auto_apply_branch`,
`allow_git_mutation`, `auto_dispatch_allowed`, `trusted_workspace_roots`)
becomes "enforceable only at project" via the registry's `enforced_at`,
which is what `GROUPABLE_POLICY_KEYS` encoded by omission. Groups cannot
carry an enforced-at-project setting; the write-time validator reads the
registry instead of the list.

### D5 — the floor stays outside the engine

The worker git hard-denies (I4), `dangerous_prefix_reason` and the
outbound-content rule (ADR 0044) are not rules in v2. The schema has no
construct that can grant into them: `vet_learned_rule`'s shape — refuse
any rule whose pattern intersects a floor pattern — is applied to every
layer's rules at write time and again at resolve time. The registry lists
the floor so RSoP can show it; the engine cannot express lifting it.

### D6 — strangler behind the same signatures, per-scope kill switch

`resolve()`, `decide()` and `run_policy_for_repo()` keep their signatures.
A setting `policy_engine_v2_scopes` (a list of scopes, default empty)
routes a scope through v2; everything else keeps the old path. Cutover
order: `shell` first (highest card volume), then `network`, `filesystem`,
`coding`, `mcp`, `email`, `browse`. Before each flip the P0 golden suite
must pass with the scope on v2 (the harness runs both paths and compares).
A flip back is one settings edit; flips are recorded in the audit trail.

### D7 — migration is forward-only with the old reader kept

`policy_document`, `operator_policy_document`, `policy_groups` and the
project overlays are read by both readers until deletion. The v2 reader
derives tri-state from presence; it never rewrites stored documents
during P1. Deletion of the old resolvers happens only after the 30-day
dogfood ledger shows no kill-switch flips (the CTO file's non-negotiable).

## Consequences

- Positive: one vocabulary, visible inheritance, an editor can be
  generated from the registry (P2), and `decide()` gains a stable external
  face (P3).
- Negative: two engines coexist for at least 30 days; every policy test
  runs twice during that window; the migration's "absent = not configured"
  reading must be reviewed against the golden files before each flip.
- Fallback (CTO file): if `shell` is not cut over by end of week 6, P1
  stops and the quarter ships P0 + P3 only.
