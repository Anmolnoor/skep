# Worker Adapters

Skep supervises workers through the local worker contract. The default worker is
the in-repo coding worker, but you can point a run or server at a different
worker with `--worker-cmd` or `SKEP_WORKER_CMD`.

## Current Adapter Status

| Worker | Status | Notes |
| --- | --- | --- |
| In-repo coding worker | Supported | Deterministic local worker for source-checkout runs. |
| Claude Code | Supported | Thin adapter around the local `claude` CLI. |
| Generic shell worker | Supported | Prompt wrapper for local CLI agents configured with `SKEP_SHELL_WORKER_CMD`. |
| Custom contract worker | Supported | Any command that reads Skep task JSON and writes Skep result JSON. |
| Codex | Supported | Thin `AdapterSpec` on the shared CLI adapter (v33): `python -m skep.workers.codex`. |
| Aider | Supported | Thin `AdapterSpec` on the shared CLI adapter (v33): `python -m skep.workers.aider`. |
| Ollama coding worker | Supported | First-party planner over a local Ollama model (v33): `python -m skep.workers.ollama`. |

## Claude Code

The Claude Code adapter runs the local `claude` CLI in the task worktree, then
captures the resulting patch as normal Skep evidence.

```sh
uv run skep run \
  --execution-mode workspace \
  --worker-cmd "python -m skep.workers.claude_code" \
  /path/to/repo \
  "Fix the failing test"
```

The adapter expects `claude` to be on `PATH`. If your executable is elsewhere,
set `SKEP_CLAUDE_CODE_CMD` and explicitly allow that variable through Skep's
worker environment boundary:

```sh
SKEP_CLAUDE_CODE_CMD="/path/to/claude" \
uv run skep run \
  --execution-mode workspace \
  --worker-cmd "python -m skep.workers.claude_code" \
  --env-allow SKEP_CLAUDE_CODE_CMD \
  /path/to/repo \
  "Add input validation"
```

What the adapter does:

- Calls `claude --print "<instructions>"` with the task worktree as `cwd`.
- Captures `git diff --binary` as the patch artifact.
- Runs `git diff --check` as the baseline verification gate.
- Writes the standard Skep result JSON and event log.

What it does not do yet:

- Parse task-specific verification commands.
- Intercept Claude Code tool calls directly.

The adapter is intentionally thin: Claude Code edits the disposable worktree,
while Skep owns sandboxing, evidence capture, re-verification, patch approval,
approval memory, and learned templates.

## Generic Shell Worker

The generic shell worker is a prompt wrapper for local CLI agents that do not
need a bespoke Skep adapter yet. Set `SKEP_SHELL_WORKER_CMD` to the command that
should receive the task instructions as its final argv item:

```sh
SKEP_SHELL_WORKER_CMD="my-agent --prompt" \
uv run skep run \
  --execution-mode workspace \
  --worker-cmd "python -m skep.workers.shell_worker" \
  --env-allow SKEP_SHELL_WORKER_CMD \
  /path/to/repo \
  "Fix the failing test"
```

The configured command runs with the task worktree as `cwd`, edits files there,
and exits. Skep captures the resulting `git diff --binary`, runs
`git diff --check`, writes the event log/result JSON, and keeps approvals and
audit evidence in the supervisor layer.

Use a custom contract worker instead when the underlying agent needs richer
state, structured progress events, or task-specific verification beyond this
single prompt-call shape.

## The caste roster (v101, ADR 0049)

`src/skep/supervisor/castes.py` is the one place a caste is registered. The
contract owns the names (`KNOWN_WORKER_KINDS`); this registry owns routing and
description, and every operator surface reads it rather than keeping a copy.

| caste | lands a patch | calls the LLM | fetches | what it does |
|---|---|---|---|---|
| `coding` | yes | yes | no | Writes code against a repo and produces a patch for approval. |
| `audit` | yes | no | no | Bumps unsafe dependency pins and re-runs the suite. Deterministic. |
| `curator` | no | no | no | Turns an inbox of notes into memory *proposals* for review. |
| `document` | no | yes | no | Drafts and summaries. No web, no code, nothing lands. |
| `researcher` | no | no | yes | Answers from allow-listed sources only, with evidence per source. |
| `script` | no | no | no | One inline script, sandboxed, deny-all egress. Output, never a patch. |
| `verifier` | no | no | no | Runs the project's PINNED `verify_command` and reports the verdict. |
| `reviewer` | no | yes | no | Reads the diff against the startup baseline and reports findings + a verdict. |

`coding` is the default and the only caste with an empty argv: it defers to
`config.command_for`, which is what `SKEP_WORKER_CMD`, `--worker-cmd` and the
coding-engine adapters override.

The registry covers the contract exactly: a caste added to `KNOWN_WORKER_KINDS`
without a registry entry fails the gates, and an unknown caste is refused by
name rather than silently running a coding worker.

`verifier` (v101-F2) runs the command the SUPERVISOR pinned, handed down in the
task envelope (contract 0.3.4) — never one it chose or read out of its
instructions. A worker deciding what "verified" means is what v88-F4 removed
from G10, and a caste called *verifier* is the last place to put it back. With
no pin it refuses and names the verb that sets one.

`reviewer` (v101-F3) diffs the worktree against the same startup baseline
`git.diff` uses, asks the provider for findings, and writes
`.artifacts/review.md`. It produces no patch, so a review never lands — the
operator reads it and decides. An empty diff completes without calling the
provider, and a finding naming a file the diff does not touch fails the run
rather than shipping.

## Worker Contract Stability

The launch build ships worker contract 0.3.5. Check the exact package and
contract versions with:

```sh
skep --version
```

Minor bumps are additive: new optional fields, event types, or capability
metadata may appear without breaking existing workers. Major bumps are breaking:
removing fields, renaming fields, changing required semantics, or changing the
task/result/event JSON shape requires adapter updates.

Contract 0.2.1 (v19) added optional, additive fields only — `commands` on the
`approval.requested` event payload and on `ApprovalVerdict` (batch shell
approvals), plus the supervisor-assigned `superseded` task state that workers
never emit — so the claude adapter's `>=0.1,<0.3` range still holds.

Contract 0.2.2 (v13) added one optional, additive field — `memory` on the task
envelope, a list of curated memory items the supervisor injects as *context, not
authority*. Workers that ignore it behave exactly as before.

Contract 0.3.5 (v101-F3) registers the `reviewer` caste — read-only diff
review; nothing lands. An older worker rejects the name with the existing
doctor-style error.

Contract 0.3.4 (v101-F2) adds one optional, additive field — `verify_command`
on the task envelope: the project's PINNED verification command, the one G10
re-runs (v88-F4). The `verifier` caste runs exactly this and never nominates its
own. Workers that ignore it behave exactly as before.

Contract 0.3.3 (v69, ADR 0040) adds one optional, additive field —
`planning_protocol` on the task envelope (`"plan"` default, `"react"` for the
bounded act–observe loop). Workers that ignore it plan exactly as before.

Contract 0.3.2 (v51) registers the `script` caste — sandboxed inline code
runs dispatched by the Queen's `run_code`. Contract 0.3.1 (v40) adds the
optional `decided_by` audit field; 0.3.0 (v17)
is an additive minor bump: it registers the `researcher`,
`verifier`, and `document` worker castes. v0.2 tasks still parse unchanged; a
worker that does not implement a caste rejects it with a doctor-style error. All
first-party workers and the claude adapter widened their supported range to
`>=0.1,<0.4` in the same change.

## Custom Contract Workers

`--worker-cmd` is not a raw prompt wrapper. The command must speak Skep's worker
contract: read the task JSON path passed by the supervisor, write a result JSON
file, and emit artifacts/events that Skep can audit.

Use this shape for an internal adapter:

```sh
uv run skep run \
  --execution-mode workspace \
  --worker-cmd "/path/to/your-skep-worker" \
  /path/to/repo \
  "Fix the failing test"
```

The adapter can call any underlying CLI agent, but Skep only trusts the contract
files it receives back from the adapter.

## Codex, Aider, and Ollama (v33)

Codex and Aider ship as thin `AdapterSpec` declarations over the shared CLI
adapter body (`src/skep/workers/cli_adapter.py`) — same contract, same
sandbox, same gates as Claude Code. The Ollama coding worker is a
first-party planner that reuses the saved assistant LLM credentials by
default. Select any of them with `--worker-cmd` or `SKEP_WORKER_CMD`.
