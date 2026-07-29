# ADR 0023 — Queen-side file reads as a policy-governed tool surface (v51-F2)

Date: 2026-07-16 · Status: accepted

## Context

The 2026-07-16 REPL field test recorded the gap directly: the Queen can
show a repo's git state but not a file's contents. Hermes closes this with
raw `read_file`/`write_file`/`patch`/`search_files`; skep's laws forbid raw
file access (ADR 0019: the model never holds the trigger; ADR 0022:
capabilities arrive as policy scopes).

## Decision

The Queen gets `read_file` and `search_files` (ripgrep), governed by the
existing `filesystem` scope through a new **operator-policy** resolution
path — the stored global policy document decided per call, distinct from
the per-run policy compile workers get:

1. The path is **resolved first** (symlinks judged by where they land),
   then decided.
2. Explicit `filesystem` scope rules win; a deny is a hard deny — no card,
   because a card could be confirmed and denied space must stay unreachable
   by confirmation (the v40-F10 rule).
3. Unmatched paths fall back to the **operator roots** — the skep home,
   the repos root, every workon-bound project path. Inside a root the read
   executes in the turn; outside, the call pauses into a confirmation card
   naming the exact resolved path.
4. Execution re-checks the decision (last guard), mirroring
   `call_mcp_tool`.

Mechanically both tools live in the mutating tier — that is the tier with
policy routing and cards — even though they mutate nothing. The
`call_mcp_tool` precedent applies: the decision, not the tier name, sets
the behavior.

## Writes are deferred, deliberately

No field test has recorded the Queen needing to write a file, and a Queen
writing into a registered repo's working tree needs a dirty-worktree design
answer (repo_state, doctor, patch application onto a modified tree) before
it can ship. When demand appears: `filesystem.write`, always carded, never
auto-approved.

## Consequences

The Queen answers "what's in pyproject.toml?" without a card for the repos
the operator already works on, every out-of-root read is an explicit
operator decision on the true path, and reads are bounded (line-numbered,
capped) for the small model's context.
