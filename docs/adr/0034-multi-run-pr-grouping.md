# ADR 0034 — Multi-run PR grouping (v54-F4)

Date: 2026-07-17 · Status: accepted

## Context

Every run landed on its own `skep/<task_id>` branch and `open_pr`
opened one PR per run — five related fixes meant five PRs and five
merge decisions. The field-test words: "it keep creating new PR for
every change where it should know better." The `skep/maintain`
integration branch (v30) already proved the pattern — many patches as
commits on one branch, one human merge — but only for auto-applied
maintain-phase patches, and the Queen had no guidance on when to group.
The append-to-existing-branch mechanism itself has existed since
v24-F1.

## Decision

1. **`open_pr` accepts `task_ids` (and `title`).** The runs must share
   a repo; each lands in the given order (earliest first) as a commit
   on ONE shared branch — `skep/<slug(title)>`, staying in the `skep/`
   namespace like every supervisor branch, falling back to
   `skep/<first_task_id>` — then ONE PR opens. The single-`task_id`
   path is unchanged.

2. **Presentation, not governance.** Each run is still independently
   dispatched, approved (patch-as-approval, ADR 0002 — the shared
   branch is persisted on each run's own approval), re-verified (G10),
   and audited; its evidence line travels in the grouped PR body. The
   grouping only decides how many PRs the human reviews.

3. **Conflicts stay honest.** A run already landed on a different
   branch is rejected (it cannot join); one already on the shared
   branch is skipped, never re-applied. A patch that no longer applies
   mid-sequence fails that run cleanly — earlier commits stay on the
   branch, the failing run stays un-landed, the human decides.

4. **The judgment is taught, not hard-coded.** A system-prompt
   paragraph tells the Queen to group same-topic same-repo runs
   (earliest first, titled by topic), keep unrelated work separate,
   and ask when unsure. Learning from corrections is v53-F1's
   conversation-skill observer — already built.

## Consequences

- Related fixes arrive as one branch, one PR, one diff, one merge.
- `main` still never moves on its own; merging stays the separate
  operator-confirmed `merge_pr` step.
