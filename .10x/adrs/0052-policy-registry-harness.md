# ADR 0052 (draft) — the policy registry, the characterization harness, and RSoP

**Status:** draft (`.10x/adrs/`); house number 0052 on acceptance into `docs/adr/`.
**Feature:** `policy-engine-rewrite`, phase P0 (`.10x/decisions/cto/policy-engine-rewrite.md`).
**Related:** ADR 0022 (one schema), 0044 (outbound floor), 0047 (CLI engines), 0048 (policy groups), 0050 (one verb, three faces). Invariants I5, I8, I9, I10.

## Context

Four policy systems compose today's answer to "what may this run do?":
the scope document (`policy_schema.py`), the run-policy stack
(`policy_resolver.run_policy_for_repo`), the auto-approval rules
(`policy.py`), and the operator document. Each is sound; nothing enumerates
them, and the one view that exists (`effective_policy_view`) reports the
run-policy values with a single provenance label and no override chain.
Before an engine can replace them (P1), three things must exist: a list of
every setting, a frozen record of today's answers, and a way to ask the
current engine why.

## Decisions

### D1 — the registry is a Python table, frozen dataclasses

`supervisor/policy_registry.py` holds one `PolicyKey` per setting:
`name, system, kind ("knob" | "rule"), type, default, layers, enforced_at,
invariant, doc`. A Python table over JSON or a pydantic model because the
registry is never parsed from outside (pydantic lives at trust boundaries,
ADR 0022) and because the tests that keep it honest are drift guards
against code constants (`PROJECT_POLICY_KEYS`, `policy_view()`,
`GROUPABLE_POLICY_KEYS`, `SCOPE_ACTIONS`) — a table beside those constants
is the cheapest thing that cannot drift silently.

Layer eligibility and the "enforced" bit live **in the registry** as
metadata. Today they are implied by code (`GROUPABLE_POLICY_KEYS` excludes
the trust ramp; `_PROJECT_RUN_POLICY_FIELDS` says what a project may set).
P0 declares them; the drift tests assert the declaration matches the code.
P1 reads the declaration instead of the key lists. Until P1, the registry is
metadata only: no decision path consults it (I5).

The floor is **listed, not modelled**: entries with `layers=()` name the
worker git hard-denies, the never-grantable shell prefixes and the outbound
content rule so RSoP can render "floor: not configurable". No layer can
set them; P1's schema will not be able to express lifting them.

### D2 — golden files come from a snapshot, tests never read `~/.skep`

`scripts/policy_golden.py snapshot` exports the policy-bearing operator
state (projects, bindings, groups, templates, the two policy documents, the
global policy settings on an allowlist) into `tests/fixtures/policy_golden/
seed.json`. Nothing secret can ride it: the allowlist is the registry's
global keys; provider rows, `llm_*` settings and tokens are never read.
Binding paths are rewritten to `<home>/bound/<basename>` so the seed is
machine-free. `freeze` builds a temp home from the seed and writes one
golden JSON per project and template plus the operator document's
resolution. The test rebuilds the temp home from the seed and compares.
The operator refreshes seed and golden on purpose, by script, and the PR
diff is the review of the policy change (I10).

### D3 — RSoP payload

```
{
  "repo": "...", "project": {...} | null,
  "keys": [{"key", "value", "source_layer", "source_name", "overridden": [{"layer", "name", "value"}]}],
  "scopes": {"<scope>": [{"rule_id", "verdict", "action", "pattern", "source"}]},
  "operator": {"<scope>": [...]},
  "floor": ["worker-git-hard-denies", "never-grantable-prefixes", "outbound-content"]
}
```

`source_layer` ∈ global | template | phase:<phase> | group:<name> | project
| session | dispatch | trusted-roots; `source_name` is the project id or
group name. `overridden` lists every lower layer that set the key and lost,
in application order. A key no layer set has `source_layer: null` and the
value the resolver's hardcoded fallback supplied is labelled as exactly
that — unresolved renders as unresolved (I8). The chain is recorded by the
existing `run_policy_for_repo` beside its v109-F9 label (`_provenance_chain`),
popped by `resolve_run_policy` like the label; the resolution result does
not change.

### D4 — the "why" query

`would <scope>.<action> <value> in <repo> be allowed?` runs the same
`resolve_run_policy` a dispatch would (no side effects, no record) and
`decide()`s on the compiled run document; it also answers for the Queen
side via `resolve_operator_policy(store).decision()`. The answer is
`{run: {verdict, decided_by, rule_id, considered}, operator: {...}}`. An
unknown scope or action refuses naming the known set (I9). This is the
read-only ancestor of P3's `POST /api/policy/decide`: same inputs, same
`decided_by` vocabulary, no new engine (I5).

### D5 — three faces

Chat `policy_registry` / `policy_rsop` / `policy_why`; REST
`GET /api/policy/registry`, `GET /api/repos/{repo}/rsop`,
`GET /api/policy/why`; CLI `skep policy registry | rsop | why`. Reads are
exempt from the ADR 0050 rule; they are built on all three faces anyway
because the editor (P2) and the hook (P3) will read them.

## Consequences

- No behavior change in P0; the effective-policy view stays byte-identical
  and every existing test passes untouched.
- The golden suite becomes the parity gate P1 must pass per scope.
- Two small costs: the registry must be edited when a key is added (the
  drift tests make forgetting impossible), and the seed/golden files are
  regenerated when the operator changes policy on purpose.
