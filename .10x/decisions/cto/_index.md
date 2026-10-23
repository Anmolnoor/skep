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
- House ADRs live in `docs/adr/NNNN-*.md` (next: 0052). `.10x/adrs/` holds
  drafts only; an accepted draft is moved there with the house number.
- Plans are local-only (`plans/` is gitignored). Decide once whether `.10x/`
  is committed or joins the gitignore; this index assumes committed.

## Active features

| Slug | Description | Status |
|---|---|---|
| `policy-engine-rewrite` | Four policy systems -> one engine; P0 harness, P1 engine, P2 editor, P3 enforcement hook; full quarter | Strategy complete, handed to Architect for P0/P1 ADRs |

## Housekeeping (not features)

- Tag + publish 1.1.0 from `main` (fleet on 1.0.2).
- Start the 30-day usage ledger from the audit trail.
- Site redesign paused for the quarter.
