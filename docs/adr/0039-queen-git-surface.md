# ADR 0039 — The Queen's git surface: reads free, mutations carded (v57)

Date: 2026-07-18 · Status: accepted

## Context

After v55 gave the supervisor its fetch station and v56 fixed context
and approval delivery, the operator named the remaining gap directly:
"create all the tools regarding the git / pr / merge and all we gonna
need to work with git and worktrees." The audit trail agreed — branches
were born only as landing side effects, PRs were invisible without a
terminal, worktrees were unlisted, repo removal was HTTP-only (and
would rmtree under a live worker), and updating a grouped-PR branch
after another landing meant a manual push.

## Decision

One consistent rulebook for every git-facing verb:

1. **Reads are free tools; mutations are cards.** `git_log`, `git_diff`
   (capped, honest truncation), `list_worktrees` (joined with run
   states), and `list_prs` run without confirmation — they change
   nothing. `create_branch`, `delete_branch`, `push_branch`, and
   `unregister_repo` ride the existing confirmation-card machinery.

2. **Remote git is supervisor-side on operator credentials, only.**
   `list_prs`/`push_branch`/`delete_branch --remote` use the same gh /
   git-push boundary as open_pr and merge_pr. Nothing worker-side
   changed: the v19-F3/F5 and v22-F2 denies stand untouched.

3. **Destructive edges refuse by construction.** delete_branch uses the
   safe form only (`-d`, never `-D`): the default branch and unmerged
   work 409 — force-delete stays a human at a terminal. push_branch
   refuses the default branch (main moves only through merge_pr) and
   never forces. create_branch refuses existing names (appending is
   landing's job, v24-F1). unregister_repo refuses while runs are in
   flight — a guard the HTTP route also gained.

4. **Unknown refs teach, not just refuse.** Every ref-taking read names
   refresh_repo in its error, closing the loop with ADR 0035.

## Consequences

- The register → refresh → branch → dispatch → review (git_diff) →
  land → PR (list_prs/open_pr/push_branch) → merge (merge_pr) → clean
  up (delete_branch/unregister_repo) lifecycle is drivable end-to-end
  from chat, with a card at every mutation.
- The Queen can answer "what is skep doing right now" (list_worktrees)
  and "what PRs are open" (list_prs) without operator terminal output.
- No force flags exist anywhere on this surface, deliberately.
