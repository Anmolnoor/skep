---
name: dependabot-maintainer
description: review each open Dependabot PR on its exact head with G10 + V1; a confirmed, untampered verdict gets a SHA-pinned merge card and its receipt, anything else is refused with its forensics
---

# Dependabot maintainer

Tools: list_repos, effective_policy, list_prs, refresh_repo, dispatch_run, await_runs, get_run, merge_pr, add_task, proof_export, run_forensics

Run this in a **web** chat, on demand ("follow the dependabot-maintainer
skill on <repo>"). Every mutating step below is a card the operator
confirms; nothing merges without the `merge_pr` card (I1). The nightly
schedule (last section) only reports: it never reviews or merges.

## 0. Preflight: all must hold, or STOP

1. `list_repos`: <repo> is listed. If not, say "register <repo> first" and
   stop.
2. `effective_policy repo=<repo>`. Check each; for every one that fails,
   say it and its fix (`skep project setup <repo> ...`), then stop:
   - `project` is set (no binding: run `skep project setup <repo>`);
   - `verify_command` is pinned, not `(worker-nominated fallback)`
     (`--verify-command "..."`): G10 never re-runs a worker's own choice
     (v88-F4);
   - `execution_mode` is `sandbox` (`--execution-mode sandbox`);
   - `policy_provenance.strict_read_confinement.value` is true
     (`--strict-reads`);
   - `policy_provenance.unlocked.value` is true (`--unlocked`), so a
     review run never waits on an ask.

## 1. List the Dependabot PRs (read-only)

`list_prs repo=<repo> state=open`. Keep the PRs whose head branch starts
with `dependabot/` AND whose author is GitHub's Dependabot: `author.is_bot`
is true and `author.login` is `app/dependabot` or `dependabot[bot]`. Anyone
can name a branch `dependabot/...`: a PR with any other author is an
ordinary PR. List it as "not Dependabot's (author <login>)" and do not
review, card or merge it under this skill. None left: say so and stop.

The PRs `scripts/seed-dependabot-fixture.sh` opens are authored by you, not
Dependabot, so this check skips them too. Review one only when the
operator's own message names its PR number as a fixture to review.

## 2. For each PR, one at a time

1. `refresh_repo repo=<repo> pr=<N>`: the supervisor fetches the PR head and
   its base into `refs/skep/pr/<N>/{head,base}` (workers never fetch). Note
   the `head_sha` it returns.
2. `dispatch_run repo=<repo> ref=refs/skep/pr/<N>/head execution_mode=sandbox engine=builtin`
   with instructions: "Review Dependabot PR #<N> as it stands. Make NO file
   changes. Verify by running the project's tests." Always `engine=builtin`:
   an external engine (claude_code, codex, aider) fails a run that leaves no
   patch ("agent produced no workspace patch") before G10 runs, so the PR
   would get no verdict. A run that changes nothing gets the patchless
   review: G10 re-runs the pinned `verify_command` on the exact PR head;
   V1 reads the PR's own diff (merge-base..head) for deleted, emptied,
   skipped or unreadable tests.
3. `await_runs task_ids=[<task_id>]`, then `get_run task_id=<task_id>`. It
   is this PR's run only if `ref` is `refs/skep/pr/<N>/head` and
   `reverification.head_commit` is the `head_sha` from 2.1. Read `state`
   and the reverification's `outcome`, `confirmed`, `tamper_flags` and
   `base_commit`.

## 3. Decide

**Green**, only when ALL hold: `state` is `completed`; `confirmed` is true
and `outcome` is `passed`; `tamper_flags` is empty; `head_commit` is the
`head_sha` from 2.1. Propose `merge_pr repo=<repo> pr=<N>`. Its card pins
the head, base branch and base SHA G10 confirmed; without a clean verdict
skep refuses the proposal itself. If the card's head or base differs from
the verdict's, tell the operator to deny it. If the PR or its base moves
after the card, the merge is refused: say so and start again at 2.1.
A PR with auto-merge set, or a base that uses a merge queue, is refused
too: tell the operator to merge it on GitHub. If skep could not check for a
queue, GitHub refuses the merge itself (HTTP 405) and the result names the
queue: report it the same way. If the result says it merged onto another
base than the one verified, report that line verbatim: the branch needs a
re-verify.

**Workflow bump**: a `test_config_changed:.github/workflows/...` flag.
skep does not verify CI config, so never propose `merge_pr`. Call
`add_task title="Human review: <repo> PR #<N> changes .github/workflows (head <sha12>)"`
so it waits on the operator's list.

**Red**: anything else. Propose NO merge. Say why, in the run's words:

- `state` is `failed` and `reverification` is null: the bump breaks the
  tests. An honest worker exits failed, so G10 never ran. Quote the
  failing test from the run's summary.
- `outcome` is `failed`: G10 re-ran the tests and they failed.
- `outcome` is `tampered`: the PR changes the tests (name each flag).
- `unavailable` / `not_applicable`: skep could not verify it. Say what the
  detail says; never call that a pass.
- `head_commit` is not the `head_sha`, or the run edited files: start
  again at 2.1.

## 4. Report: one summary for <repo>

Attach the evidence itself, not a command for it (both tools only read):

- green: `#<N> <title>: merge card for head <head12> on base <base12>
  (task <task_id>)`, then `proof_export task_id=<task_id>` and post its
  Markdown receipt below the line. Its PR row must name #<N>, the
  `head_sha` from 2.1 and the base; if it does not, say so.
- red: `#<N> <title>: NOT merged, <reason> (task <task_id>)`, then
  `run_forensics task_id=<task_id>` and post its Markdown below the line.
- workflow bump: `#<N> <title>: human review, task added`

## Nightly report (a prompt schedule)

A scheduled turn reads the store only (ADR 0042, I6). It cannot call
list_prs (GitHub, as you), refresh, dispatch or card, so the nightly
schedule cannot see new PRs and never reviews or merges. It reports what
the last reviews found. From the web chat:

> propose_schedule name=dependabot-report caste=prompt every=1d
> instructions="list_runs limit=50. Keep the runs on repo <repo> whose ref
> starts refs/skep/pr/. For each PR number N (from refs/skep/pr/N/head), take
> the newest run and write one line: #N, its reverification head_commit
> (12 chars), state, outcome, confirmed, tamper_flags. End with: 'New or
> moved PRs need the dependabot-maintainer skill, run in the web chat.'"

Run the skill from the web chat when the report shows a PR with no
confirmed run, or when Dependabot opens a new one.
