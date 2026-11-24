---
name: dependabot-maintainer
description: review each open Dependabot PR on its exact head with G10 + V1, and propose a SHA-pinned merge only for a confirmed, untampered verdict
---

# Dependabot maintainer

Tools: effective_policy, list_prs, refresh_repo, dispatch_run, await_runs, get_run, merge_pr

Run this in a **web** chat, once per repo ("follow the dependabot-maintainer
skill on <repo>"). Every mutating step below is a card the operator
confirms; nothing merges without the `merge_pr` card (I1).

## 0. Can this repo be verified at all?

`effective_policy repo=<repo>`. If it shows no pinned `verify_command`, say
"<repo> pins no verify_command, so skep cannot verify a PR head there (G10
refuses a worker-chosen command, v88-F4); set one with `skep project setup
<repo> --verify-command ...`" and STOP. Do not review, do not merge.

## 1. List the Dependabot PRs (read-only)

`list_prs repo=<repo> state=open`. Keep the PRs whose head branch starts
with `dependabot/`. None: say so and stop.

## 2. For each PR, one at a time

1. `refresh_repo repo=<repo> pr=<N>`: the supervisor fetches the PR head and
   its base into `refs/skep/pr/<N>/{head,base}` (workers never fetch). Note
   the `head_sha` it returns.
2. `dispatch_run repo=<repo> ref=refs/skep/pr/<N>/head execution_mode=sandbox`
   with instructions: "Review Dependabot PR #<N> as it stands. Make NO file
   changes. Verify by running the project's tests." A run that changes
   nothing gets the patchless review: G10 applies the PR's own base...head
   diff to the fetched base and re-runs the pinned `verify_command`; V1 reads
   that diff for deleted, emptied or skipped tests and dropped test counts.
3. `await_runs task_ids=[<task_id>]`, then `get_run task_id=<task_id>`.
   Read its re-verification: `outcome`, `confirmed`, `tamper_flags`, and the
   `detail`, which starts `PR #<N>: head <sha> on base <sha>` for a PR-head
   verdict.

## 3. Decide

Propose `merge_pr repo=<repo> pr=<N>` ONLY when all hold:

- `confirmed` is true and `outcome` is `passed`;
- `tamper_flags` is empty;
- the detail starts `PR #<N>: head` and that head is the `head_sha` from
  step 2.1 (the run changed no files; a run that edited files verified its
  own patch, not the PR).

The card states "merges only head <sha> — G10 confirmed it". If the card
instead says "NOT pinned" or "G10 did NOT confirm it", tell the operator to
deny it. If the PR is pushed to after the card, GitHub refuses the merge
(`--match-head-commit`): report it, and start again from step 2.1.

Anything else is NOT merged. Report the PR with the reason, in the verdict's
own words:

- `failed`: the bump breaks the tests. Quote the detail's failing line.
- `tampered`: the PR changes the tests (name each flag). A
  `test_config_changed:.github/workflows/...` flag is a GitHub Actions bump:
  expected, and it is the operator's call on the PR page, not this skill's.
- `unavailable` / `not_applicable`: skep could not verify it. Say what the
  detail says it could not do; never call that a pass.
- The run edited files, or the head differs: re-run from step 2.1.

For every PR, point the operator at the evidence: `skep run forensics
<task_id> --md` (what happened) and `skep proof export <task_id>` (the
receipt, bound to the verified head).

## 4. Report

One line per PR: `#<N> <title> — <proposed merge @ <sha12> | not merged: <reason>>`.

## Nightly triage (a prompt schedule)

A scheduled prompt turn is read-only and unattended: it can never refresh,
dispatch or card (ADR 0042, I6), so it cannot do steps 2–3. What it can do
each night, from the chat that should hear it:

> propose_schedule name=dependabot-triage caste=prompt every=1d
> instructions="For <repo>: list_prs state=open, keep head branches starting
> dependabot/. For each, say whether list_runs limit=50 shows a run whose
> instructions start 'Review Dependabot PR #<N>', and its verification. End with: 'follow the
> dependabot-maintainer skill on <repo>' if any PR is unreviewed."

Then run this skill from the web chat when the triage says so.
