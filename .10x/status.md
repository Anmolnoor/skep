# .10x status

**Project:** skep
**Discovered:** 2026-10-22 at `bc6f4cf` (PR #30, v115-F16). Existing codebase; `.10x/` created by discovery, not by scaffold.
**Current feature:** `acp-governor` (Restart Plan Part B, re-scoped) — ADR draft `adrs/0054-acp-governor.md`; executor plan local at `plans/acp-governor/README.md`; branch `acp-governor` (app worktree), one PR at the end.
**Phase:** B1 — the proxy: B1-1..B1-3 done and live-smoked 2026-11-06; B1-4 (read faces) next. B0 done 2026-11-06. Opened 2026-11-06 after the operator accepted D3, D6, engine v2 for fresh homes, version 1.2.0, and one ledger file for observed/forwarded rows.
**Previous feature:** `policy-engine-rewrite` — Delivered — PR #33 merged, v1.1.0 tagged, `shell` on engine v2 since 2026-10-23, all seven scopes since 2026-11-06 (review 2026-12-05). Next feature slug: D3/D4 (a stored form for `cleared`/`enforced`). The v1 resolvers are deleted only after the 30-day ledger shows no flips. Operator decision 2026-10-22: all phases land on PR #33 (one review), separate commits per phase, built one by one with gates between. Design and Planning done 2026-10-22: ADR drafts `adrs/0052-policy-registry-harness.md`, `adrs/0053-policy-engine-v2.md`; executor plan local at `plans/policy-engine-rewrite/README.md`.

## Discovery summary `[DISCOVERED]`

- Stack: Python 3.12, FastAPI + uvicorn, pydantic v2, sqlite store (JSON-in-settings pattern), no-build static web UI, bwrap/seatbelt sandboxes. ~59K src LOC, ~66K test LOC, 237 test files, 51 house ADRs, CI on ubuntu+macos (pytest -n auto, ruff, ruff format, mypy, scorecard).
- Architecture: Queen (chat daemon) dispatches contract-governed workers into git worktrees; patch-as-approval landing; G10 supervisor re-verify; one capability layer for first-party workers; CLI agents (claude_code/codex/aider) confined by sandbox only (ADR 0047).
- Policy today: four systems (scope document ADR 0022; run policy resolver + policy groups ADR 0048; auto-approval rules; operator policy document) over an immutable worker git floor. See `decisions/cto/policy-engine-rewrite.md`.
- Release state: pyproject 1.0.2, PyPI 1.0.2 (Aug), internal rounds at v115 — ~2.5 months unreleased.
- Public signals (2026-10-22): 1 star, 0 forks, 2 open PRs (dependabot).

## Phases — acp-governor

| Phase | Owner | State |
|---|---|---|
| 0 Open | Architect | **done** 2026-11-06 — ADR 0054 draft + nine amendments from the v1/v2 schemas and the tree (hard ring v1-only; v2 `subject`; ledger read is new; no `acp_sessions`; seed at store creation; `source` discriminates observed/forwarded); operator decisions recorded; handoff to SDE |
| B0 Wire table | SDE | **done** 2026-11-06 (`B0-1` 1bbb067, `B0-2` 790f82a, tag `ckpt/acp-day-1`) — `scripts/acp_fixture_scan.py`, `scripts/acp_tap.py`; T3's MIT ACP transcripts (kiro, grok, fx, registry probe) + eight live taps of `@agentclientprotocol/claude-agent-acp` 0.101.0 and `codex-acp` 2.5.0 (v1/v2, default/asking modes) under `tests/fixtures/acp/` in the envelope format; `tests/fixtures/acp/README.md` is the wire table. Findings: both adapters speak v2 when offered; default modes (claude `auto`, codex `agent`) never ask; claude's first announce has empty `rawInput`; codex's v2 request shell-quotes the command; a shell redirect is a write the `filesystem` scope never sees; options chosen by `kind`, never position. Four rounds (two tap bugs on v2, three scrub rules added: skill list, account frames, agent prose) |
| B1 Proxy | SDE | **B1-1 done** bd5eb73 (`acp/proxy.py`, byte-transparent, 31 tests; review folded in: cancel direction, dup ids, null ids, raising callbacks), **B1-2 done** b14da4d + 3a65093 (`policy_paths.workspace_value` — symlink escape pinned on stdlib, no port; `acp/mapper.py` Session/probe/presets; `expected_probes.json` × 6), **B1-3 done** 820c407 (`acp/govern.py`, `acp_cmds.py` `skep acp govern|doctor`, `policy_note`, A10 hard ring refuses non-allow; review folded in: shutdown abort on agent-exit-first, `shlex.join` floor bypass, observed = ran unasked; goldens × 9 + synthetic). **Live smoke 2026-11-06 (Day 5, `plans/acp-governor/day5-smoke.md`)**: temp home — Claude v1 `default`: observed `git status`, floor deny `git push`, allow `fs:workspace` Write; Codex v2 `read-only`: observed `git status`, `rm -rf build` forwarded → human allow_once, floor deny `git push`; real home: one floor row, nothing else touched; doctor works on both adapters. **B1-4 pending** — `policy_decisions` on three faces |
| B2 Package | SDE | pending — engine v2 seed for fresh homes, `skep acp install-hint`, `docs/acp-governor.md`, 1.2.0; PR opened; operator tags and smokes on the Air |
| B3 Field test | Operator | pending — T3 desktop, Supervised + Auto on both agents, GIF, F-list; fixes on `b3-field-fixes` |
| B4 Deliver | Operator | pending — comments on t3code #6976 and #7340; 48-hour watch seeds v117 |

## Delivered — policy-engine-rewrite (record)

| Phase | Owner | State |
|---|---|---|
| 0 Brainstorm | all | done in chat 2026-10-22 (verdict + four complaints confirmed + sequence agreed) |
| 1 Strategy | CTO + PM | **done** — `decisions/cto/policy-engine-rewrite.md`; PM file folded into CTO file (solo operator is the only user) |
| 2 Design | Architect + Staff | **done** 2026-10-22 — `adrs/0052-*.md` (P0), `adrs/0053-*.md` (P1), drafts until accepted into `docs/adr/` |
| 3 Planning | EM + Senior | **done** 2026-10-22 — executor plan (local, gitignored) `plans/policy-engine-rewrite/README.md`: P0-1 registry, P0-2 golden harness, P0-3 RSoP + why; one branch per phase |
| 4 Implementation | SDE | **P0 done**, **P1 done** 2026-10-22 (P1-1 engine `policy_engine.py`, P1-2 reader `policy_layers.py`, P1-3 kill switch `policy_engine_v2_scopes` + flip ledger + golden parity with all seven scopes flipped, P1-4 validator reads the registry; switch ships OFF — operator flips `shell` first). **P2 done** 2026-10-22: registry-driven "Resolve & edit" tier on the Policies page (RSoP per knob, inherit/set at the edited layer, writes via the project upsert and PUT /api/policy, why box, engine switch), browser-verified on a temp home. **P3 done** 2026-10-22: `policy_decide` (recorded why-query), POST /api/policy/decide, `skep policy decide|hook` (Claude Code PreToolUse), `docs/policy-hook.md`; smoked on the real home. P0 was on `p0-policy-registry-harness` — P0-1 registry (`policy_registry.py`), P0-2 harness (`policy_golden.py`, `scripts/policy_golden.py`, 7 real projects frozen under `tests/fixtures/policy_golden/`), P0-3 RSoP + why (`serve/actions.py`) with chat/REST/CLI faces; each commit gated green |
| 5 Verification | QA + Security | P0: golden-parity suite green on the committed seed; no behavior change. P1: parity proven two ways — `test_policy_layers` (values + labels for all seven real projects) and `test_policy_golden` with all seven scopes flipped (byte-equal). P2: browser-verified set/inherit/why on a temp home. P3: hook smoked on the real home (allow by rule, unlisted → ask, read silent). Every commit gated green (pytest -n auto, ruff, format, mypy, scorecard) |
| 6 Delivery | DevOps + SRE | **PR #33 merged 2026-10-23 (merge commit 2919983) after review finding F1 (floor consulted before decide) landed; tagged `v1.1.0` at 4d11978 (release workflow publishes PyPI/GHCR/GitHub release); live daemon restarted on main; `shell` flipped to engine v2 at 2026-10-23T02:53Z — the 30-day ledger has started (`<audit>/policy/engine-flips.jsonl`).** Rollback = one `PUT /api/policy` flip back |

## Risks flagged

- The skep PreToolUse hook floor-denies branch/commit/push for Claude Code in the main checkout (as designed); this feature runs in an app worktree the hook does not cover — the judge loop (gates every commit, every new module read in full) is the control there.
- T3's recorded ndjson may be T3's own orchestration events rather than raw ACP frames; the live taps are then the primary fixtures.
- Live agents may refuse the tap client (auth, version negotiation) — fall back to the operator running the tap by hand.
- The fresh-home engine seed makes every test home v2 for two scopes; pins asserting v1-only shapes are fixed, never opted out (`test_policy_engine_switch.py:65-66` is the known one).
- Two concurrent sessions on one tree collided before (v70/v77) — one Opus writer at a time.
