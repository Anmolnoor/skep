---
name: dependabot-maintainer
description: review each open Dependabot PR on its exact head with G10 + V1; a confirmed, untampered verdict gets a SHA-pinned merge card and its receipt, anything else is refused with its forensics
---

# Dependabot maintainer

Tools: list_repos, effective_policy, list_prs, refresh_repo, dispatch_run, await_runs, get_run, merge_pr, add_task

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
with `dependabot/`. None: say so and stop.

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
   review: G10 applies the PR's base...head diff to the fetched base and
   re-runs the pinned `verify_command`; V1 reads that diff for deleted,
   emptied or skipped tests.
3. `await_runs task_ids=[<task_id>]`, then `get_run task_id=<task_id>`. It
   is this PR's run only if `ref` is `refs/skep/pr/<N>/head` and
   `reverification.head_commit` is the `head_sha` from 2.1. Read `state`
   and the reverification's `outcome`, `confirmed`, `tamper_flags` and
   `base_commit`.

## 3. Decide

**Green**, only when ALL hold: `state` is `completed`; `confirmed` is true
and `outcome` is `passed`; `tamper_flags` is empty; `head_commit` is the
`head_sha` from 2.1. Propose `merge_pr repo=<repo> pr=<N>`. Its card pins
the head and base G10 confirmed. If the card's head or base differs from
the verdict's, or it says it is not pinned, tell the operator to deny it.
If the PR moves after the card, GitHub refuses the merge: say so and start
again at 2.1.

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

You cannot run skep yourself (your shell never runs it), so each line
names the evidence command for the operator, filled in:

- green: `#<N> <title>: merge card for head <head12> on base <base12>
  (task <task_id>). Receipt: skep proof export <task_id>`
- red: `#<N> <title>: NOT merged, <reason> (task <task_id>). Forensics:
  skep run forensics <task_id> --md`
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
