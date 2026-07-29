# ADR 0036 — Per-project policy copy (v55-F4)

Date: 2026-07-18 · Status: accepted

## Context

Per-project policy has existed since the project layer landed: each
project carries its own overlay (`project_policies` table) over the
global settings — network, shell allowlist, budgets, execution mode,
auto-apply/auto-dispatch, trusted roots (`PROJECT_POLICY_KEYS`). But the
only write path for custom knobs was `setup_project`'s
`policy_overrides`, which re-derives the base from pack/phase defaults
and rebuilds bindings. Field-test words: "we need multiple policies to
each project so that I can manage the access and commands, and where I
can just copy or add the same policy from one project to another one to
save some time." Governing a second project like the first meant
re-answering every policy question by hand.

## Decision

1. **`copy_project_policy(src, dst)` — a carded operator verb.** Reads
   src, filters its stored policy to `PROJECT_POLICY_KEYS`, and writes
   that overlay as dst's policy via `store.add_project_policy`
   (re-validated on write). dst keeps its own name, strategy, phase,
   pack, and — critically — its repo bindings; `add_project_policy`
   never touches `project_bindings`.

2. **Copy replaces dst's overlay, not its resolved policy.** Phase and
   strategy defaults still apply at resolve time underneath the overlay
   (`run_policy_for_repo` merge order is unchanged: run args → project
   overlay → phase defaults → global). Copying an empty overlay is
   legal and means "back to phase defaults."

3. **Not a new data model.** One overlay per project, copyable, covers
   the ask. Named reusable policy documents can grow out of
   `policy_templates/` later if copying proves insufficient — deferred
   until a field test demands it.

## Consequences

- "Set this project up like that one" is one confirmation card; the
  card lists exactly which keys copy.
- A copy is a snapshot, not a link — later edits to src do not follow.
- Chat-tool only (the card machinery is the audit trail); no HTTP route
  until something needs it.
