# .10x status

**Project:** skep
**Discovered:** 2026-10-22 at `bc6f4cf` (PR #30, v115-F16). Existing codebase; `.10x/` created by discovery, not by scaffold.
**Current feature:** `policy-engine-rewrite`
**Phase:** Implementation — P0 and P1 built; P2 (editor) next, then P3 (hook). Operator decision 2026-10-22: all phases land on PR #33 (one review), separate commits per phase, built one by one with gates between. Design and Planning done 2026-10-22: ADR drafts `adrs/0052-policy-registry-harness.md`, `adrs/0053-policy-engine-v2.md`; executor plan local at `plans/policy-engine-rewrite/README.md`.

## Discovery summary `[DISCOVERED]`

- Stack: Python 3.12, FastAPI + uvicorn, pydantic v2, sqlite store (JSON-in-settings pattern), no-build static web UI, bwrap/seatbelt sandboxes. ~59K src LOC, ~66K test LOC, 237 test files, 51 house ADRs, CI on ubuntu+macos (pytest -n auto, ruff, ruff format, mypy, scorecard).
- Architecture: Queen (chat daemon) dispatches contract-governed workers into git worktrees; patch-as-approval landing; G10 supervisor re-verify; one capability layer for first-party workers; CLI agents (claude_code/codex/aider) confined by sandbox only (ADR 0047).
- Policy today: four systems (scope document ADR 0022; run policy resolver + policy groups ADR 0048; auto-approval rules; operator policy document) over an immutable worker git floor. See `decisions/cto/policy-engine-rewrite.md`.
- Release state: pyproject 1.0.2, PyPI 1.0.2 (Aug), internal rounds at v115 — ~2.5 months unreleased.
- Public signals (2026-10-22): 1 star, 0 forks, 2 open PRs (dependabot).

## Phases — policy-engine-rewrite

| Phase | Owner | State |
|---|---|---|
| 0 Brainstorm | all | done in chat 2026-10-22 (verdict + four complaints confirmed + sequence agreed) |
| 1 Strategy | CTO + PM | **done** — `decisions/cto/policy-engine-rewrite.md`; PM file folded into CTO file (solo operator is the only user) |
| 2 Design | Architect + Staff | **done** 2026-10-22 — `adrs/0052-*.md` (P0), `adrs/0053-*.md` (P1), drafts until accepted into `docs/adr/` |
| 3 Planning | EM + Senior | **done** 2026-10-22 — executor plan (local, gitignored) `plans/policy-engine-rewrite/README.md`: P0-1 registry, P0-2 golden harness, P0-3 RSoP + why; one branch per phase |
| 4 Implementation | SDE | **P0 done**, **P1 done** 2026-10-22 (P1-1 engine `policy_engine.py`, P1-2 reader `policy_layers.py`, P1-3 kill switch `policy_engine_v2_scopes` + flip ledger + golden parity with all seven scopes flipped, P1-4 validator reads the registry; switch ships OFF — operator flips `shell` first). P0 was on `p0-policy-registry-harness` — P0-1 registry (`policy_registry.py`), P0-2 harness (`policy_golden.py`, `scripts/policy_golden.py`, 7 real projects frozen under `tests/fixtures/policy_golden/`), P0-3 RSoP + why (`serve/actions.py`) with chat/REST/CLI faces; each commit gated green |
| 5 Verification | QA + Security | P0: golden-parity suite green on the committed seed; no behavior change (every pre-existing test untouched). P1's per-scope gate from here on |
| 6 Delivery | DevOps + SRE | pending — per-scope kill switch is the rollback plan |

## Risks flagged

- Rewrite consumes the quarter's daily-driver window (accepted by operator).
- Fallback trigger: `shell` scope not cut over by end of week 6 -> drop to P0 + P3 only.
- Two concurrent sessions on the policy tree have collided before (v70/v77, 2026-08-12) — one branch per phase.
