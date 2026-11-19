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
patch artifact. Your default branch never moves without you: landing a patch
needs your approval by default, and the opt-in auto-landing lanes (section 6)
write only `skep/*` branches.

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

Five engines fit behind that boundary: `builtin` (skep's own worker) and the
CLI adapters `claude_code`, `codex`, `aider` and `pi`. Ollama is a model
provider for the built-in worker, not an engine. The adapter can be thin
because Skep owns supervision, not code generation.

An external engine's own commands do not pass skep's capability layer, so the
sandbox is its only wall. That is why an external engine always runs in
sandbox mode (a `workspace` request is turned into `sandbox`) and is refused
until the project pins a `verify_command`. Claude Code and Codex are also
refused when their auth environment variable is missing (`NO AUTH ENV`). See
[`guide-unattended-run.md`](guide-unattended-run.md).

## 3. Sandbox Boundary

In sandbox mode, Skep asks the host OS to constrain the worker process:

- macOS uses Seatbelt when the host allows `sandbox-exec`.
- Linux uses bubblewrap when `bwrap` is installed and its probe succeeds, with
  Landlock on top where the kernel supports it. podman is an opt-in backend.
- If sandbox mode is requested but no backend can be applied, the worker does
  not start unsandboxed.

The sandbox is defense in depth. It blocks out-of-policy writes and, depending
on platform and policy, network access. Reads outside the workspace stay
mostly open; a list of credential paths (the secret floor) is hidden. It does
not replace review, re-verification, or the audit trail.

Workspace mode has no OS sandbox: the built-in worker runs on the host, held
only by the capability layer and policy.

See [`sandboxing.md`](sandboxing.md) for backend details and current Linux
domain-allowlist limits.

## 4. Approvals

If a worker needs a gated permission, Skep stops the run at an approval point:

```text
approval needed: shell.run
  reason:       shell.run requires approval for command: python -m pytest
  [a] approve once  [b] approve + remember  [d] deny  [s] skip
```

Approving once resumes only the current run. Approving with remember saves the
shell command as a grant for the project (or the repo) straight away and prints
`remembered: <command> → <tier>`; a learned template for similar future tasks
is saved or updated only if the resumed run then completes. `[b]` is offered
only when the request can be remembered.

See [`approvals.md`](approvals.md) for the first-run, auto-match, and drift
flows.

## 5. Re-Verification

After a worker reports a completed patch, Skep checks the evidence independently:

1. Create a fresh worktree at the original baseline.
2. Apply the worker's patch.
3. Re-run the project's pinned `verify_command`. The command the worker
   recorded is the fallback, used only for a project with no pin and for a run
   outside any project; the record says which was used.
4. Store whether the worker's claim was confirmed.

This is the core safety property: Skep does not trust an agent saying "tests
passed" if the patch fails when replayed on a clean copy.

If re-verification disagrees with the worker, Skep surfaces that in `status` and
`review`, and auto-approval does not apply.

See [`verification.md`](verification.md) for the four outcomes and the
re-run's environment, and
[`guide-pin-verify-command.md`](guide-pin-verify-command.md) to set the pin.

## 6. Patch Approval

Review shows the patch and evidence:

```sh
skep review <task_id>
```

Approval applies the patch to a new branch:

```sh
skep review <task_id> --approve
```

The branch is named `skep/<task_id>` unless you pass `--branch NAME`; the
repository's default branch is always refused. The landing is a local commit.
Skep pushes that branch only when you ask it to: `skep review <task_id>
--approve --pr` and `skep pr open` push it and open a pull request with `gh`.
A worker never pushes, pulls, fetches or switches branches. You keep the
normal Git decision: inspect, merge, edit, or delete the branch.

Landing without your approval is opt-in, and has two lanes. A project in the
**maintain** phase (or with `auto_apply_verified_patch` set, or set up with
`--unlocked`) auto-applies a patch that the pinned verify command confirmed;
in the maintain phase those land on ONE integration branch (`skep/maintain`)
instead of a fresh branch per run. `--auto-approve` lands a confirmed patch
that touches only dependency manifests and lockfiles. Both lanes land only a
re-verified patch, only on a `skep/*` branch, and record the approval as
`auto:<rule>`. `main` never advances automatically; you merge when you choose.
See [`guide-auto-landing.md`](guide-auto-landing.md).

## What Skep Guarantees

- The worker runs in a disposable worktree, not your active checkout.
- Your default branch never moves without you. Landing a patch needs your
  approval unless you switched an auto-landing lane on, and those lanes write
  only `skep/*` branches.
- A completed run that produced a patch is re-checked on a clean worktree with
  the project's pinned verify command, or with the worker's own command when
  nothing is pinned. A run that changed no files has nothing to re-check.
- Approval decisions, events, artifacts, and re-verification results are stored
  as durable run evidence. The records are kept locally and are not
  tamper-evident.
- Sandbox mode fails closed when the requested host backend cannot be applied.
- The built-in worker cannot push, pull, fetch or switch branches; no grant
  lifts that. An external engine is kept from pushing by the sandbox instead
  (hidden credentials, git remote protocols switched off for its worktree); a
  branch switch inside its own disposable worktree reaches nothing of yours.

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
four low-risk action classes (`dispatch_run`, `scheduled_result_ack`,
`read_url`, `start_research`) are ever confirmable from a channel — shell commands, policy changes, and patch
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

- Re-verification is only as strong as the command that is re-run. On a
  project with no pinned `verify_command` that is the command the worker chose.
- Workspace mode has no OS sandbox. External engines never run in it; the
  built-in worker can.
- In sandbox mode writes are confined; reads outside the workspace are mostly
  open, apart from the secret floor.
- Sandbox strength depends on the host OS backend and host policy. There is no
  Windows backend.
- Linux bubblewrap enforces deny-all and concrete per-domain allowlists
  (`--unshare-net` plus a filtering-proxy socket bridged in by `skep.netshim`).
  An allow-all (`*`) run shares the host network namespace, so its loopback is
  the host's.
- An external engine's individual tool calls are not policy decisions and are
  not logged one by one. Its run, patch, re-verification and approval are.
- Skep supervises local worker processes for one operator. There is no hosted
  or multi-user mode.

For command examples, see [`quickstart.md`](quickstart.md) and
[`cli-reference.md`](cli-reference.md).
