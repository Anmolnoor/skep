# ADR 0026 — Operator-policy resolution: the Queen's standing policy (v52)

Date: 2026-07-17 · Status: accepted

## Context

Until v52, "Queen-side" meant "no policy applies" for network tools:
`search_web` hit its backend ungoverned and `read_url`'s domain was never
consulted. v51-F2 (ADR 0023) gave file reads a policy path, but it read
the STORED GLOBAL document — and that document is not Queen-only:
`resolve_run_policy_for_ops` feeds its network scope into ops-worker
`allowed_hosts` and its filesystem write rules into ops
`allowed_roots`/`allowed_dests`. A Queen-side allowance placed there
would widen worker egress. Queen-only rules need a document workers
never read.

## Decision

1. **A second standing document, same schema.**
   `OPERATOR_POLICY_SETTINGS_KEY` holds a plain `PolicyDocument`; the
   default allows exactly one thing — keyless web search, by the named
   `net:search` rule. No new types (ADR 0022's schema is reused as-is).

2. **Composition over migration.** Queen-side decisions resolve
   `resolve(base=global document, overlays=(operator document,))` — the
   native composer. Global rules (templates, learned MCP allows) keep
   their exact effect on the Queen; deny wins ties across BOTH documents;
   `decided_by` threads through unchanged. v51-F2's semantics survive:
   with an empty operator document the composition IS the global
   document, and the dynamic operator-roots fallback is untouched.

3. **`search` is a network-scope action.** The search backend (ddgs)
   rotates engines, so no domain pattern could honestly govern it; the
   action names the capability instead. Precedent: the email scope's
   actions arrived when email went live (v41-F3). `search_web` runs only
   on an `allow` (default: `operator-default/net:search`, named in the
   result); anything else is a clean tool error — read tools never card
   (the card tier is the mutating tier, ADR 0023).

4. **Option A for carded tools.** `read_url`'s card remains the human
   gate. The operator policy is consulted for AUDIT only: a standing
   allow is credited by its rule id, otherwise `decided_by:
   operator-card`. The policy check never blocks a confirmed card —
   allow rules exist for tools that execute WITHOUT one.

5. **No cache.** The draft plan designed a ConfigHolder-style cached
   resolution. The house pattern for policy consults is a per-call
   settings read (`fileio.py`, `mcp_client.py`); a chat turn is seconds
   and the read is microseconds. Per-call load removes the invalidation
   bug class entirely.

6. **Edits are carded and vetted.** `set_operator_policy` (scopes
   bounded to what the Queen consults: filesystem, network) appends one
   rule behind a confirmation card. An allow whose pattern intersects
   composed deny space is rejected with the deny's rule id — denied
   space stays unreachable by confirmation (v40-F10). A deny that would
   strand an existing learned rule fails a dry-run composition at write
   time, not on every later Queen decision.

## Consequences

- Every Queen-side scoped tool result carries `decided_by`; the chat
  transcript is the audit record (ADR 0019 §3).
- The `decided_by` label for Queen decisions is the global template when
  one is set, else `operator-default`.
- Rule removal, per-project Queen overrides, and a require_approval→card
  path for read tools are deliberately not built (no observed demand;
  recorded in plans/v52).
