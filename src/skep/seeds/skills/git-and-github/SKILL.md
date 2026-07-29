---
name: git-and-github
description: how git and GitHub actually work in skep — which verb to use, who is allowed to run it, and how work gets from a run to a merged PR
---

# Git and GitHub

Tools: repo_state, git_log, git_diff, list_prs, list_worktrees, refresh_repo, create_branch, merge_branch, push_branch, open_pr, merge_pr, close_pr, delete_branch, land_run, dispatch_run

Read this before any branch, merge, push or PR work. It exists because
guessing here produces branches nobody merges: the operator whose field
test prompted it had **13 branches on one repo and 1 merged commit**.

## The model, in four sentences

1. A run works in a **disposable worktree** detached from any branch.
2. Its result is a **patch diffed against the commit the run started from**
   — not a commit, not a branch.
3. A human approving that patch is what creates a commit, on
   `skep/<task_id>` (or the project's `auto_apply_branch`). **Landing IS
   the commit.**
4. `main` never moves except through a pull request a human merges.

Everything below follows from those. If a request seems to need something
that contradicts them, the answer is a different verb, not a workaround.

## Who may do what

| | worker | you (the Queen) | operator verbs you can propose |
|---|---|---|---|
| edit files | yes | no | — |
| `git add` / `commit` | only with `git.commit` intent | **never** | landing does it |
| branch / checkout / switch | **never** | **never** | `create_branch` |
| merge / rebase / cherry-pick / revert / `reset --hard` | **never** | **never** | `merge_branch` |
| fetch / pull / push | **never** | **never** | `refresh_repo`, `push_branch` |
| PRs | **never** | **never** | `open_pr`, `merge_pr`, `close_pr` |

The "never" column is a hard deny. No allowlist entry, no grant, no
`verify` label and no phrasing gets around it — and it binds you exactly
as it binds a worker. **Do not attempt these through `run_shell`.** You
will get a refusal, and the refusal is correct.

What you *can* do is propose the verb in the right-hand column. Each one
is supervisor-side, runs on the operator's own credentials, and shows the
operator a card before anything happens.

## The verbs

Reads — free, no card:

- `repo_state` — branches, HEAD, how far behind origin. **Start here.**
- `git_log`, `git_diff` — history and diffs for any ref
- `list_prs`, `list_worktrees`

Mutations — each one cards:

- `refresh_repo` — fetch origin, fast-forward the default branch. This is
  the only "fetch". Run it before reasoning about how stale anything is.
- `create_branch` — new branch off a base ref. Refuses the default branch
  and names that already exist.
- `merge_branch` — merge one local ref into another local branch. Refuses
  to merge **into** the default branch. Conflicts abort cleanly and name
  the conflicting files; nothing is left half-merged.
- `push_branch` — push a non-default branch to origin. Fast-forward only.
- `open_pr` / `merge_pr` / `close_pr`
- `delete_branch` — refuses the default branch and anything unmerged.
- `land_run` — turn an approved run's patch into a commit on a branch.

## Recipes

**A branch is behind the default branch.** `refresh_repo`, then
`merge_branch source=<default> into=<branch>`. Never dispatch a run to do
this — a worker cannot, and asking it to wastes the run.

**Several task branches should go up as one PR.** This is the common case
after a few runs on the same repo, and doing it one-PR-per-branch is what
produces an unreviewable pile.

```
refresh_repo                                    # know the real state
create_branch     name=skep/<topic>             # off the default branch
merge_branch      source=skep/<task-1> into=skep/<topic>
merge_branch      source=skep/<task-2> into=skep/<topic>
push_branch       name=skep/<topic>
open_pr           branch=skep/<topic>
```

Merge them oldest first: a later branch usually expects the earlier one's
changes, and that order turns most conflicts into fast-forwards.

**Extending work that already landed on a branch.** Pass
`ref=<that branch>` to `dispatch_run`. Without it the run baselines from
the **default branch** and cannot see the earlier work — which is how ten
runs produce ten independent branches that each ignore the last.

**A conflict.** `merge_branch` aborts and tells you which files. Do not
retry it; nothing changed. Either the operator resolves it in a checkout,
or you `dispatch_run` with `ref=<the target branch>` and a brief naming
the conflicting files and the intended outcome.

**Work is finished and should reach `main`.** `push_branch`, `open_pr`,
and stop. The human merges. Never try to move `main` yourself.

## Things that look like solutions and are not

- **Dispatching a run to do git.** Workers are denied every one of these.
  A run asked to merge fails, and the failure costs a full dispatch.
- **`run_shell` with a git command.** Same deny list, applied to you.
- **Re-running a task hoping the branch stacks.** It does not. Each
  dispatch baselines from the default branch unless you pass `ref=`.
- **Force-pushing.** Not available anywhere on purpose. If a push is
  rejected as non-fast-forward, say so — that is information, not an
  obstacle to route around.

## What to tell the operator

Name the branch and what happens next: "merged `skep/<a>` and `skep/<b>`
into `skep/<topic>`, pushed, PR #N open — merging it is yours." When
something is refused, say which rule refused it and which verb does the
job instead. Never report a merge or a PR you did not actually get a
result back for.
