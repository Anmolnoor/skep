# Handoff

## Current handoff — CTO -> Architect + Staff Engineer

**Feature:** `policy-engine-rewrite` — read `decisions/cto/policy-engine-rewrite.md` in full first.

**What to produce:** two ADR drafts in `.10x/adrs/` (house numbers 0052, 0053 when accepted into `docs/adr/`):

1. **P0 — policy registry + characterization harness + RSoP.**
   Decide: registry shape (Python table vs JSON vs pydantic model), where
   layer eligibility and "enforced" metadata live, how golden files are
   generated from the operator's real `~/.skep` (snapshot into a temp home —
   never read operator state in tests, per CLAUDE.md), RSoP payload shape
   (`{key, value, source_layer, source_name, overridden_by: [...]}` for both
   the run-policy stack and scope-document rules), and the "why" query API
   (`would <scope>.<action> <pattern> in <repo> be allowed?` -> verdict +
   decided_by, no dispatch). All three faces (chat tool, CLI, REST) per ADR
   0050; `test_surface_parity.py` will enforce it.
2. **P1 — engine v2.**
   Decide: the unified schema (rules + knobs in one layer stack), tri-state
   semantics, "enforced" semantics vs today's `GROUPABLE_POLICY_KEYS` list
   and trust ramp, how the floor (I4, ADR 0044) stays inexpressible, the
   per-scope kill switch mechanism, cutover order (`shell` first), and the
   migration of stored documents (`policy_document`, `operator_policy_document`,
   `policy_groups`, project overlays) — forward-only, with the old reader
   kept until deletion.

**Constraints from the CTO file that are not negotiable:** same entry-point
signatures; floor outside the engine; I5 one boundary; I8 unresolved renders
unresolved; old resolvers deleted only after the 30-day ledger is clean.

**Known seams to read before designing:** `supervisor/policy_schema.py`
(resolve/decide, learned-rule vetting), `supervisor/policy_resolver.py`
(`run_policy_for_repo`, groups merge), `supervisor/policy.py`
(auto-approval), `serve/actions.py::effective_policy_view`,
`serve/settings.py::policy_view`, `workers/capabilities.py` (the floor),
ADRs 0022, 0044, 0047, 0048, 0050, `docs/invariants.md` Part I.

**Not in scope for Design:** P2 editor UI and P3 hook details beyond
confirming `decide()` will have a stable external face.

## Handoff history

- 2026-10-22 — Discovery -> CTO: existing codebase analysed; four policy systems identified; operator confirmed goal (personal daily driver, three months) and chose engine rewrite over registry-only path; sequence P0->P1->P2->P3 agreed.
