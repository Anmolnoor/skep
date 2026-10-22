# .10x status

**Project:** skep
**Discovered:** 2026-10-22 at `bc6f4cf` (PR #30, v115-F16). Existing codebase; `.10x/` created by discovery, not by scaffold.
**Current feature:** `policy-engine-rewrite`
**Phase:** Strategy complete (CTO). Next: Design (Architect + Staff Engineer) — P0 and P1 ADR drafts.

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
| 2 Design | Architect + Staff | pending — P0 + P1 ADR drafts (house numbers 0052, 0053) |
| 3 Planning | EM + Senior | pending |
| 4 Implementation | SDE | pending — P0 first |
| 5 Verification | QA + Security | pending — golden-parity gate is the QA instrument |
| 6 Delivery | DevOps + SRE | pending — per-scope kill switch is the rollback plan |

## Risks flagged

- Rewrite consumes the quarter's daily-driver window (accepted by operator).
- Fallback trigger: `shell` scope not cut over by end of week 6 -> drop to P0 + P3 only.
- Two concurrent sessions on the policy tree have collided before (v70/v77, 2026-08-12) — one branch per phase.
