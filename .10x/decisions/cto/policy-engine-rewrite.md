# CTO decision — policy-engine-rewrite

**Date:** 2026-10-22
**Status:** Accepted by operator (Anmol), sequence agreed
**Decision class:** one-way door (authorization boundary) — run as a strangler so it stays two-way until the old resolvers are deleted

## Business context

skep is a personal daily driver for the next three months, not a product. The
operator's stated jobs for it: schedules and calendar blocks, a
dependabot-PR test-under-policy-then-merge loop, Obsidian as second brain,
and governing every agent on the workstation under one policy layer modelled
on Windows Local Group Policy / AD GPO. Public traction is explicitly not the
goal this quarter (1 star, 0 forks as of 2026-10-22; launch 2026-07-31 got no
response). A later carve-out of a small "govern any agent" package for other
people is deferred until the policy surface is an enumerable list.

## The problem, as discovered in the code

Four policy systems coexist, each sound alone, with no single model over them:

1. Scope document (ADR 0022, `policy_schema.py`): scope x action x pattern ->
   allow / require_approval / deny; templates; learned rules; `decided_by`.
2. Run policy (`policy_resolver.run_policy_for_repo`): a different vocabulary
   (`default_network`, `allowed_shell_commands`, `coding_engine`, trust ramp)
   composed supervisor defaults -> phase -> policy groups (ADR 0048) ->
   project overlay -> dispatch args.
3. Auto-approval rules (`policy.py`) for landing, own matcher.
4. Operator policy document; plus the immutable worker git floor beneath all.

Editing: `PUT /api/policy` raw JSON, `skep project setup`, policy-group verbs,
chat cards. UI: a single pill + popover. `effective-policy` covers only (2)
and reports values without the layer that set them.

Operator's complaint (all four confirmed): two vocabularies; no inheritance
model; cannot see or edit it; wrong enforcement point (external CLI agents
bypass the capability layer — ADR 0047).

## Decision

**Rewrite into one engine, delivered as four sequenced projects, strangler
pattern, full quarter.** Operator chose the rewrite over the lighter
registry+RSoP-only path (offered and declined).

| Phase | Slug | Scope | Est. |
|---|---|---|---|
| P0 | `policy-registry-harness` | Registry of every policy key (type, default, layer eligibility, invariant served, doc). Characterization suite: today's effective policy for every real project/template/group frozen as golden files. Read-only RSoP endpoint (value + source_layer + source_name + override chain, both systems). No behavior change. | ~2 wks |
| P1 | `policy-engine-v2` | One schema, two setting kinds — *rules* (scope·action·pattern -> verdict) and *knobs* (typed values) — in one layer stack: floor -> supervisor defaults -> template -> phase -> groups -> project -> session -> dispatch. Tri-state per setting (not configured / set / explicitly cleared). "Enforced" at a layer blocks lower overrides; the trust ramp becomes an enforced-at-project property instead of a special-case key list. Lands behind existing `resolve()` / `decide()` / `run_policy_for_repo()` signatures, one scope at a time, per-scope kill switch, golden files must match before each cutover. | ~3–4 wks |
| P2 | `policy-editor` | Registry-driven web page: layer tree, tri-state controls, RSoP column, writes through existing PUT routes and cards. Built once, after P1. | ~1–2 wks |
| P3 | `policy-enforcement-hook` | `decide()` reachable from outside: Claude Code PreToolUse hook -> `POST /api/policy/decide`; Codex/Aider via their hooks where present, sandbox as floor where absent. Thin face on the new engine. One-day throwaway spike against the current engine allowed now to learn hook payload shape. | ~1 wk |

## Non-negotiables carried into every phase

- The floor stays outside the engine: worker git hard-denies (I4) and
  never-grantable outbound content (ADR 0044) are not rules in the new
  model; the engine cannot express lifting them.
- I5 holds: one authorization boundary. The registry is metadata, not a
  second decision path. P3 is a face on `decide()`, not a parallel engine.
- I8 holds: RSoP and the editor render "unresolved" as unresolved, never as
  defaults.
- Old resolvers are deleted only after the 30-day dogfood ledger shows no
  per-scope kill-switch flips. Until then the door is two-way.

## Alternatives considered

| Alternative | Why not |
|---|---|
| A. Reskin existing surfaces (UI only) | Puts four shapes on one screen; the mess becomes visible, not fixed. |
| B. Registry + RSoP, editor on top, resolvers untouched (CTO's original recommendation) | Lower risk, two-way; declined by operator — all four complaints are in scope and the inheritance/tri-state model cannot be faked by metadata. Survives as P0. |
| C. Big-bang rewrite | Breaks the daily driver during the exact quarter it must be daily-driven. Rejected in favour of strangler. |
| D. Defer; do P0 + P3 only this quarter | Offered; declined. Recorded as the fallback if P1 slips past week 8. |

## Trade-offs accepted

- The rewrite consumes the whole three-month window. Daily jobs (dependabot
  loop, calendar blocks, Obsidian) get no new engineering until P1 cuts over;
  they run on what already works, continuously, on `main`.
- Four engines keep existing underneath until the final deletion.
- Positioning/site redesign work is stopped for the quarter.

## Risks

- **Breaking the daily driver mid-rewrite.** Mitigation: per-scope kill
  switch, golden-file parity gate, cutover one scope at a time starting with
  `shell` (highest card volume).
- **Scope creep into P2/P3 before P1 is green.** Mitigation: P2 cannot start
  until all seven scopes pass golden parity; P3 spike is explicitly throwaway.
- **Operator attention** (solo, AI-delegated coding, two-session collisions
  recorded v70/v77/2026-08-12). Mitigation: one branch per phase, no parallel
  sessions on the policy tree.
- **Fallback trigger:** if P1 is not cut over for `shell` by end of week 6,
  drop to alternative D for the quarter.

## Success criteria (reviewed at end of 30-day ledger, then at quarter end)

- Every unexpected approval card explained by RSoP in under a minute.
- Zero policy edits made via raw JSON `PUT`.
- "Would action X in project Y be allowed?" answers for all seven scopes.
- Claude Code runs on the workstation produce `decide()` verdicts in the
  audit trail (P3).
- Golden parity held through every cutover; old resolvers deleted.

## Housekeeping decided alongside (not part of this feature)

- Tag and publish 1.1.0 from current `main` first; the three-machine fleet is
  on August's 1.0.2.
- Start the 30-day usage ledger from the existing audit trail now; it is the
  review instrument for this decision.
