# Handoff

## Current handoff — Architect -> SDE, start at B0

**Feature:** `acp-governor` — read `adrs/0054-acp-governor.md` in full first,
amendments A1–A9 included; then `plans/acp-governor/README.md` (local) for the
day-by-day steps, anchors and gates.

**What to produce, one branch (`acp-governor`), one PR:**

1. **B0 — the wire table.** `scripts/acp_fixture_scan.py`, `scripts/acp_tap.py`
   (stdio logger + minimal ACP client), T3's MIT ACP transcripts and the live
   taps of `claude-agent-acp` (v1 and v2) and `codex-acp` vendored under
   `tests/fixtures/acp/<agent>/` in the envelope format, with
   `LICENSE-t3code.txt` and a root `NOTICE`; `tests/fixtures/acp/README.md`
   is the table. Gate: both live agents' `tool_call` shapes for a shell
   command and a file write, the permission options offered, and whether the
   permission request itself carries kind/rawInput/locations.
2. **B1 — the proxy.** `acp/proxy.py` (pure; transparency test byte-identical
   on every fixture), `policy_paths.workspace_value` + `acp/mapper.py`
   (`expected_probes.json` per agent), `acp/govern.py` + `acp_cmds.py`
   (`skep acp govern|doctor`; `policy_note`; golden `expected_ledger.jsonl`
   per agent; floor `git push` → `reject_once`; never `*_always`),
   `policy_decisions` on three faces.
3. **B2 — package.** Engine v2 seed at store creation for fresh homes,
   `skep acp install-hint`, `docs/acp-governor.md`, hook doc in installed
   form, 1.2.0 everywhere, version-history entries. PR to `main`.

**Constraints that are not negotiable:** I5 one boundary (the proxy carries no
decision logic — it is `hook_probe` with an ACP front over `policy_decide`);
fail-to-ask (store unreadable, unknown agent, unreadable `rawInput`, any
exception → forward to the human; never a silent allow); D6 no standing
grants; pass-through is the original bytes; no new runtime dependency; the
five gates green on every commit; fixtures scrubbed of host paths.

**Known seams to read before coding:** `supervisor/policy_hook.py`,
`cli_cmds.py::cmd_policy_hook`, `serve/actions.py::policy_decide` /
`_floor_verdict` / `_append_policy_ledger`, `netshim.py` (REMAINDER argv,
signal forwarding), `channel_cmds.py` (command-group template),
`tests/supervisor/test_policy_hook.py`, `tests/supervisor/conftest.py`.

**Not in scope:** T3 code changes, a sandbox, verification of proxied
sessions, the UI (shelved), pidfiles / live-session listing.

## Handoff history

- 2026-11-06 — Architect -> SDE: ADR 0054 draft written in `adrs/` with nine amendments from the ACP v1/v2 schemas and the tree; operator accepted D3, D6, engine v2 for fresh homes, 1.2.0, one ledger file; feature opened on branch `acp-governor` (app worktree); Implementation starts at B0.

- 2026-10-22 — CTO -> Architect + Staff: ADR drafts 0052 (P0) and 0053 (P1) written in `adrs/`; Planning folded into the local executor plan; Implementation of P0 started on `p0-policy-registry-harness`.

- 2026-10-22 — Discovery -> CTO: existing codebase analysed; four policy systems identified; operator confirmed goal (personal daily driver, three months) and chose engine rewrite over registry-only path; sequence P0->P1->P2->P3 agreed.

## 2026-10-22 — SDE handoff: P0–P3 built, awaiting operator review (PR #33)

Built one by one, separate commits per phase, every commit gated green:

- **P0** registry (`policy_registry.py`), golden harness (`policy_golden.py`,
  `tests/fixtures/policy_golden/`), RSoP + why on three faces.
- **P1** engine v2 (`policy_engine.py`: one stack, set/extend/clear,
  enforced, floor outside), reader (`policy_layers.py`, nothing rewritten),
  kill switch `policy_engine_v2_scopes` + flip ledger, golden parity with
  all seven scopes flipped, validator reads the registry. ADR 0053 draft
  carries the P1 refinements (union is a statement mode — the golden proved
  a project list replaces today).
- **P2** the "Resolve & edit" tier on the Policies page (registry-driven,
  RSoP per knob, inherit/set, writes via the project upsert + PUT /api/policy,
  why box, engine switch). Explicit "cleared" has no stored form yet.
- **P3** `policy_decide` + `POST /api/policy/decide` + `skep policy
  decide|hook` (Claude Code PreToolUse), ledger `<audit>/policy/decisions.jsonl`,
  `docs/policy-hook.md`.

Operator's next moves: review and merge PR #33; flip `shell` on the live
daemon (Policies → Resolve & edit → engine switch, or `PUT /api/policy`);
install the hook in `~/.claude/settings.json`; start the 30-day ledger.
Open after that: a stored form for "cleared" and "enforced" (D3/D4), the
remaining `resolve(document)` preview in `policy_templates`, and deleting
the v1 resolvers only after the ledger shows no flips. ADR drafts 0052/0053
move to `docs/adr/` on acceptance.
