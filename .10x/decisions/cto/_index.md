# CTO — index

## Standing principles

- skep is a personal daily driver first; traction work is paused until the
  policy surface is an enumerable list (then a small "govern any agent"
  carve-out becomes cheap).
- Invariants in `docs/invariants.md` outrank any decision here. Every
  feature file cites the invariants it touches.
- One-way doors (authorization boundary, storage shape, public API) are
  delivered as stranglers behind existing entry points with a kill switch
  and golden-parity gate; they become one-way only when the old path is
  deleted.
- House ADRs live in `docs/adr/NNNN-*.md` (next: 0055; 0052–0054 are drafts in `.10x/adrs/`). `.10x/adrs/` holds
  drafts only; an accepted draft is moved there with the house number.
- Plans are local-only (`plans/` is gitignored). Decide once whether `.10x/`
  is committed or joins the gitignore; this index assumes committed.

## Active features

| Slug | Description | Status |
|---|---|---|
| `policy-engine-rewrite` | Four policy systems -> one engine; P0 harness, P1 engine, P2 editor, P3 enforcement hook; full quarter | Delivered — PR #33 merged 2026-10-23, v1.1.0; all seven scopes on engine v2 since 2026-11-06 |
| `acp-governor` | skep as a transparent ACP proxy answering `session/request_permission` from `decide()` (Restart Plan Part B, re-scoped for T3 Code); ADR draft 0054; B0–B4 | Design done 2026-11-06; Implementation at B0 on branch `acp-governor` |

## Housekeeping (not features)

- Tag + publish 1.1.0 from `main` (fleet on 1.0.2).
- Start the 30-day usage ledger from the audit trail.
- Site redesign paused for the quarter.
