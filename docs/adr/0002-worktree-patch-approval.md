# ADR 0002 — Worktree + patch artifact; applying the patch is the approval (Q5-A)

Date: 2026-06-11 · Status: accepted

## Context

The worker needs a repo to mutate. Direct mutation of the user's checkout has
a large blast radius and complicates future parallelism. The architecture also
carried its oldest open problem: a *double* approval gate — worker-side
interactive approval for risky actions, plus supervisor-side human review — with
no clean story for which gate owns a commit.

## Decision

Every task runs in a temporary, detached git worktree created by the
supervisor. The worker **never commits and never pushes**; its output is a
patch artifact (`git add -N` + `git diff --binary`, with `.events/` and
`.artifacts/` excluded) plus verification evidence. Review shows the evidence;
**applying the patch is the approval action**: `skep review --approve`
applies it via `git apply --index` on a fresh branch `skep/<task_id>`
(through a temp worktree — never main, never the user's checkout) and records
the verdict + actor in the approval queue.

## The load-bearing note

This dissolves the double approval gate rather than coordinating it. Because
the worker cannot commit, worker-side commit approval has nothing left to ask in
headless mode (approval-requiring actions terminate as `pending_approval`);
the single human approval lives in the supervisor as "apply patch". The oldest open
problem in the architecture retired as a side effect of the mutation model —
not through more approval machinery.

## Consequences

- The result IS a diff + test evidence; "review evidence, not promises" is
  literal.
- v3 parallelism is nearly free: isolation was the design from day one.
- Patches must always be appliable — hence `git diff --binary` (found when a
  pycache-polluted patch refused to apply during the Stage 4 smoke).
- The repo may move between run and approval; `git apply` failure is surfaced
  as a doctor-style error whose remediation is "re-run the task".
