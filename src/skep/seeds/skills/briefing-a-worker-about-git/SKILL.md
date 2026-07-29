---
name: briefing-a-worker-about-git
description: what a worker can and cannot do with git inside its worktree, and how to write a dispatch brief that never asks for the impossible
---

# Briefing a worker about git

Tools: dispatch_run, repo_state, merge_branch, refresh_repo, get_run, git_diff

Workers have no skills of their own — you write their brief, so this is
the worker-side contract in the form you can act on. Pair it with
`git-and-github`, which covers your own verbs.

## Where a worker actually is

A detached-HEAD worktree, disposable, cloned at the commit the run
baselines from. It is not on a branch and it has no remote it may reach.

The result skep takes from it is **the diff between the working tree and
that starting commit**. Not a commit. Not a branch. That single fact
decides everything else:

- Editing files is the whole job. Edits show up in the patch.
- Anything that changes *which commits are in history* changes what the
  human is asked to approve. So merge, rebase, cherry-pick, revert and
  `reset --hard` are denied — a merged branch's work would land under
  this task's approval, and a rebase onto a newer default branch would
  put every intervening commit into the diff the card shows.
- Remote git is denied: the patch never travels over the network.
- Branch switching is denied: the run picked its ref at dispatch.

Denied means denied. No grant, allowlist entry or `purpose: "verify"`
label changes it, and the deny message names `merge_branch` so the
worker knows whose job it is.

## What a worker MAY do

- Read anything: `git status`, `diff`, `log`, `show`, `ls-tree`.
- `git reset` bare or `--soft` (index only).
- `git add` / `git commit` **only** when the task explicitly asks and the
  run carries the `git.commit` intent. Usually it should not: the landing
  approval is the commit, and a worker-made commit inside a disposable
  worktree buys nothing.

## Writing the brief

**Never ask for a git operation.** "Rebase onto main", "merge the latest",
"push when done", "create a branch for this" — all of these fail, and
they fail after the dispatch, so they cost a whole run.

**Pass `ref=` instead of asking the worker to switch.** If the work must
build on branch `skep/<x>`, `dispatch_run ref=skep/<x>`. That is the only
way a run sees another branch's work.

**Catch the branch up yourself, first.** If `repo_state` says the branch
is behind, `merge_branch` before dispatching. A worker cannot do it and
will either fail or silently work against a stale baseline.

**Say what "done" means.** One step the worker can finish and verify on
its own, with the acceptance stated: "verify by `<command>`". A brief that
ends with an improvised verify is where runs die.

## Reading the result

- `worker_kind` and `coding_engine` on the run say who actually ran it.
- The patch is what landed for review — read the diff, not the worker's
  summary of it.
- G10 re-verification runs **supervisor-side** with the project's pinned
  `verify_command`. Quote that verdict, never the worker's own claim that
  it passed.
- "completed but produced no patch" means the worker changed nothing. That
  is usually a brief that asked for something impossible — very often a
  git operation.

## If a worker reports it needs git

Believe the refusal, not the request. Take the git step yourself with the
operator verbs, then re-dispatch with `ref=` pointing at the result.
