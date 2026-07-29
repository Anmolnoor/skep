# 0022 — Policy-first: one schema, two vocabularies, govern-via-MCP

- Status: accepted (v40, executing the v36 plan). N1's email reservation
  resolved in v41-F3: `email` is a live scope with `read`/`send` verbs,
  enforced through an email-bound MCP server (`MCPServerConfig.scope`).
- Date: 2026-07-12

## Context

Skep already IS a policy engine — but policy is expressed four scattered
ways: global settings keys (`serve/settings.py`), per-project policy dicts
(`supervisor/projects.py`), auto-approval rules (`supervisor/policy.py`),
and the worker-side capability ladders (`workers/capabilities.py`,
`workers/runtime_plugins.py`, `workers/ops.py`). Nothing ties a decision
back to the rule that produced it, and day one requires hand-authoring
rules. The v19 field test measured the cost: 74 messages, 12 runs, and 6
approvals for one README task.

The reframe: what the codebase calls sandbox, proxy, and gates are all the
same thing wearing different clothes —

| Internal mechanism | Policy reading |
|---|---|
| Sandbox writable roots (Seatbelt/bwrap) | `filesystem` scope policy, physically enforced |
| Filtering proxy + domain allowlist | `network` scope policy, physically enforced |
| Approval gates / confirm cards | escalation policy (`require_approval` verdicts) |
| Shell allowlist + remember flow | `shell` scope policy with learned rules |
| MCP risk classes + grants (v17) | `mcp` scope policy |
| Audit trail (events, approvals, ledger) | `audit` — always on |

## Decision

1. **One schema.** `supervisor/policy_schema.py` defines `PolicyRule`,
   `ScopePolicy`, and `PolicyDocument` (pydantic at the policy-file trust
   boundary — the `worker_contract/task.py` precedent) plus a pure resolver
   producing frozen `ResolvedScopePolicy` views (the house dataclass
   pattern). Scopes: `coding`, `shell`, `filesystem`, `network`, `mcp`, and
   `email` as a reserved enum value.
2. **Default deny; three verdicts.** `allow`, `require_approval`, `deny`.
   Composition: template base → scope overlays → learned rules;
   most-specific pattern wins; **deny wins ties**; anything unmatched is
   denied. Every decision is auditable as
   `decided_by: <template>/<rule-id>` (extending the `auto:<rule>`
   precedent in `supervisor/policy.py`).
3. **Two vocabularies, two audiences.** Internal names stay (Queen, worker,
   honeycomb — code and dev docs); user-facing language is
   **Policy / Scope / Gate / Template / Audit**.
4. **The immutable floor.** The worker git hard-denies (checkout/switch,
   push/pull/fetch, add/commit — v19-F3/F5, v22-F2 in
   `workers/runtime_plugins.py`) sit ABOVE the schema and are not
   expressible in it. Learned rules may promote `require_approval → allow`;
   nothing may ever promote into denied space — enforced where rules are
   written (`LearnedRuleRejected`), not just where they are read.
5. **Predicates are closed enums per scope in v1.** Shell prefixes reuse the
   `shell_prefixes.py` normalizer, domains reuse `netproxy.domain_allowed`,
   paths use `fnmatch` (the `AutoApprovalRule.diff_scope` precedent), MCP
   patterns match tool names. No user-defined expressions.
6. **Govern-via-MCP (N1).** Skep implements no surfaces — no mail client, no
   file browser. Surfaces arrive as MCP servers; skep intercepts
   (`scope: mcp`), applies policy, records evidence. `email` enforcement
   arrives with the first bound email MCP server.
7. **JSON, not YAML; fixtures, not hypothesis.** Neither YAML nor hypothesis
   is a dependency; templates and policy documents are JSON validated by
   pydantic; correctness rides golden fixture corpora
   (`tests/fixtures/policy_scopes/`, the `policy_regression` pattern).
8. **No template sharing (N3)** until signing + human review ride the v31
   bundle machinery — a community template with one quietly added domain is
   a supply-chain attack.

## Consequences

- `resolve_run_policy` compiles the document once per dispatch; the
  contract's `Permissions` is the compiled artifact of resolved policy, so
  Stages A–C need zero contract bump and zero worker changes. Existing
  behavior is pinned by the 13-fixture policy regression corpus and the
  23-case capability matrix, which must pass unchanged.
- `decided_by` threads through events → ingest → approvals → views
  (contract 0.3.1, additive optional field).
- Four templates ship as data (`locked-down`, `personal-dev`,
  `homelab-ops`, `assistant`), each with a golden resolved fixture so a
  verdict change without a doc change fails the suite.
- The acceptance bar is G5: the operator replaces Hermes for daily personal
  use (the Stage F ritual in `plans/v36/README.md`) — if that fails, the
  release fails, whatever the suites say.
