# How Skep Works

Skep is a local supervisor around a coding agent. The agent still does the code
editing, but Skep controls where it runs, what it can access, what evidence it
must produce, and how the final patch lands.

## Run Lifecycle

```text
skep run
  -> create isolated worktree
  -> write task contract
  -> start worker in workspace or sandbox mode
  -> collect events, logs, patch, and verification evidence
  -> re-verify the patch on a clean worktree
  -> wait for review and approval
  -> apply the patch on skep/<task_id>
```

The important boundary is that the worker never edits your current checkout and
never lands changes on `main`. It works in a disposable worktree and returns a
patch artifact.

## 1. Isolated Worktree

When you run:

```sh
skep run /path/to/repo "fix the failing test" --execution-mode workspace
```

Skep records the source repo and baseline ref, then creates a separate worktree
for the worker. The worker can change files inside that worktree without
touching your active checkout.

At the end of the run, Skep captures a binary-safe git patch from the worktree.
That patch is the unit you review, approve, merge, or discard.

## 2. Worker Contract

Skep does not need to be the coding agent. It launches a worker process that
speaks the local task/result contract:

- task JSON in: repo, worktree, instructions, permissions, budget, and resume
  state
- event stream out: progress, commands, approvals, verification, and artifacts
- result JSON out: final state, summary, patch path, and evidence paths

The in-repo worker, Claude Code adapter, and future adapters all fit behind the
same boundary. The adapter can be thin because Skep owns supervision, not code
generation.

## 3. Sandbox Boundary

In sandbox mode, Skep asks the host OS to constrain the worker process:

- macOS uses Seatbelt when the host allows `sandbox-exec`.
- Linux uses bubblewrap when `bwrap` is installed and its probe succeeds.
- If sandbox mode is requested but no backend can be applied, the worker does
  not start unsandboxed.

The sandbox is defense in depth. It blocks out-of-policy writes and, depending
on platform and policy, network access. It does not replace review,
re-verification, or the audit trail.

See [`sandboxing.md`](sandboxing.md) for backend details and current Linux
domain-allowlist limits.

## 4. Approvals

If a worker needs a gated permission, Skep stops the run at an approval point:

```text
approval needed: shell.run
  reason:       shell.run requires approval for command: python -m pytest
  [a] approve once  [b] approve + remember  [d] deny  [s] skip
```

Approving once resumes only the current run. Approving with remember records the
permission in the approval ledger, and a successful resumed run can save or
update a learned template for similar future tasks.

See [`approvals.md`](approvals.md) for the first-run, auto-match, and drift
flows.

## 5. Re-Verification

After a worker reports a completed patch, Skep checks the evidence independently:

1. Create a fresh worktree at the original baseline.
2. Apply the worker's patch.
3. Re-run the recorded verification command.
4. Store whether the worker's claim was confirmed.

This is the core safety property: Skep does not trust an agent saying "tests
passed" if the patch fails when replayed on a clean copy.

If re-verification disagrees with the worker, Skep surfaces that in `status` and
`review`, and auto-approval does not apply.

See [`verification.md`](verification.md) for the exact outcomes and unavailable
cases.

## 6. Patch Approval

Review shows the patch and evidence:

```sh
skep review <task_id>
```

Approval applies the patch to a new branch:

```sh
skep review <task_id> --approve
```

The branch is named `skep/<task_id>`. Skep does not push it. You keep the normal
Git decision: inspect, merge, edit, or delete the branch.

A project in the **maintain** phase auto-applies verified patches, and those
land on ONE integration branch (`skep/maintain`) instead of a fresh branch per
run — one place to review the accumulated maintenance work. `main` never
advances automatically; you merge the integration branch when you choose. A
patch that cannot cleanly append escalates to a normal human approval.

## What Skep Guarantees

- The worker runs in a disposable worktree, not your active checkout.
- A patch is reviewed before it lands.
- Completed worker claims are re-checked on a clean worktree when verification
  evidence exists.
- Approval decisions, events, artifacts, and re-verification results are stored
  as durable run evidence.
- Sandbox mode fails closed when the requested host backend cannot be applied.

## Local Ops

Skep can run governed local maintenance on registered nodes (disk/service
inspection, log rotation, path cleanup, backups, service restart). Ops has the
highest blast radius, so it is doubly gated: the decision engine scopes each
capability to a node's explicit allow-list and computes strict bounds, and the
executor is the LAST guard — it re-validates every bound at execution time and
refuses a path outside the bounded roots, a non-allow-listed backup dest, or a
`/` target even if the decision permitted it. `skep ops run <check> --node N`
previews a dry-run plan and mutates nothing; `--approve` is the explicit human
gate that runs the real, bounded pass and records evidence. Inspection is
read-only. Nothing mutates unattended.

## Skill Distribution

Approved skills (learned or hand-authored recipes) can be shared as signed
bundles: `skep skill export <name>` writes a bundle signed with your 0600
skill-signing key. Importing is deliberately never automatic — the ClawHub
exfiltration lesson. `skep skill import <file>` always discloses the skill's
FULL grant surface (the shell commands it may run, whether it mutates git, the
hosts it may reach, the env it reads) and classifies the signature: *verified*
(yours), *tampered* (claims your key but was modified — refused), *foreign* (a
different signer — HMAC can't prove authenticity across parties), or
*unsigned*. Nothing enters the registry without an explicit `--approve`, and an
imported skill never runs until you dispatch it. A valid signature never skips
the human gate for a skill that carries capability grants.

## Web Reading

Workers can read the web, but never freely: `network.read` (and its raw
sibling `network.fetch`) is a governed capability, not a free tool. It reaches
only hosts in the run's network allowlist — the sandbox physically pins egress
to Skep's filtering proxy (Seatbelt on macOS, bubblewrap + netshim on Linux
since v28), and the worker re-checks the allowlist before every request. A
host that is not on the allowlist is a supervisor approval, never a silent
fetch; approving one grants exactly that host for the resumed run. `network.read`
returns the page as readable text (HTML tags stripped). A deep-research run's
`source_allowlist` IS its browse grant — nothing broader.

## Messenger Channels

The web UI is one face of the chat engine; `skep chat` is the same Queen,
gates, and audit trail as a terminal REPL (v38). Telegram, Slack, and Discord
are live entrances (v26, Discord since v37): an
allow-listed conversation routes into the exact same Queen chat →
confirmation → audit flow as the web UI. Unknown identities fail closed. Only
low-risk action classes (`dispatch_run`, `scheduled_result_ack`) are ever
confirmable from a channel — shell commands, policy changes, and patch
landings always route to the web UI, whatever the channel config says.
When the channel is confirm-enabled and the identity is allow-listed, Slack
confirms via signed Block Kit buttons, Discord via embed buttons or ✅/❌
reactions, and Telegram via inline keyboards (v41 — a press is admitted only
when both the chat and the pressing user are allow-listed, so a group
bystander can never resolve a card). Telegram long-polls, Slack
receives signed webhooks, and Discord holds a gateway websocket (messages,
reactions, and button interactions all arrive over that one socket — note the
bot needs the privileged MESSAGE_CONTENT intent enabled in the Discord dev
portal). Secrets live as 0600 files beside the serve token and are write-only
through the API.

## Current Limits

- A worker can only be re-verified as well as its recorded verification command.
- Sandbox strength depends on the host OS backend and host policy.
- Linux bubblewrap enforces deny-all, allow-all, AND concrete per-domain
  allowlists (v28: `--unshare-net` plus a bind-mounted filtering-proxy socket
  bridged in by `skep.netshim`) — at parity with macOS Seatbelt.
- Skep supervises local worker processes. Hosted multi-user execution is a
  future architecture, not the current product.

For command examples, see [`quickstart.md`](quickstart.md) and
[`cli-reference.md`](cli-reference.md).
