# CLI Reference

Use `skep --help` and `skep <command> --help` for the exact parser output. This
reference summarizes the public command surface and the options most relevant to
the launch workflow.

## Global Options

```sh
skep [--home HOME] [--version] <command> ...
```

- `--home HOME` selects the Skep state directory. Defaults to `~/.skep`.
- `--version` prints the Skep package version and worker contract version.

## Setup and Health

```sh
skep setup --personal [--provider NAME] [--model MODEL] [--endpoint URL] [--api-key-env ENV]
skep doctor
skep status [--personal] [--json]
```

- `setup --personal` creates or updates the personal profile.
- `doctor` reports readiness and exits non-zero when the profile is not ready.
- `doctor` also reports non-blocking runtime checks such as sandbox backend
  availability.
- `status --personal` shows recent supervised runs, pending approvals, and
  re-verification status.
- `status --json` emits machine-readable status.

## Run

```sh
skep run [repo] [instructions] [options]
```

Common examples:

```sh
skep run /path/to/repo "fix the failing test" --execution-mode sandbox
skep run /path/to/repo "add a signup page" --execution-mode workspace --minimal
skep run /path/to/repo "add a signup page" --execution-mode workspace --no-template
skep run --template web-feature /path/to/repo --execution-mode workspace
```

Important options:

- `--worker-cmd COMMAND` sets the worker adapter command for this run.
- `--execution-mode workspace|sandbox` chooses host-visible workspace execution
  or the host sandbox backend.
- `--template NAME` instantiates a saved workflow template.
- `--param KEY=VALUE` fills a template parameter; repeatable.
- `--no-template` skips learned-template auto-match for this run.
- `--minimal` starts with empty network, env, shell, and git grants so the run
  learns through approvals.
- When multiple learned templates match in an interactive terminal, `run` shows
  a one-key picker; choose `no template` to start from the normal policy.
- `--network DOMAIN` allows one domain; repeatable. Omitted means deny all
  outbound unless policy or a matched template grants more.
- `--env-allow ENV` passes one environment variable; repeatable.
- `--caste CASTE` selects the worker kind. The common launch path is `coding`.
- `--auto-approve` enables configured safe auto-approval rules.
- `--quiet` suppresses the live phase tail.

`--minimal` cannot be combined with `--template`, `--network`, or `--env-allow`.

## Review and Approval

```sh
skep review <task_id> [--approve | --deny | --allow-command]
```

- Plain `review` shows the run evidence, patch, approval state, and
  re-verification result.
- `--approve` applies a completed patch to a `skep/<task_id>` branch, or resumes
  a pending approval.
- `--allow-command` approves and remembers a pending shell command, then resumes
  the run.
- `--deny` rejects the pending approval.
- `--actor NAME` records who made the decision.
- `--pr` opens a GitHub pull request after applying a completed patch.
- `--worker-cmd COMMAND` sets the worker adapter command for resuming a pending
  run.

## Templates

```sh
skep template add NAME --instructions "..." [options]
skep template add --from template.toml
skep template list
skep template show NAME
skep template suggest NAME REPO "instructions" [--save]
skep template remove NAME
skep template delete NAME
skep template rename OLD_NAME NEW_NAME
```

Template authoring options:

- `--caste CASTE` sets the worker kind.
- `--repo REPO` pins a default repo.
- `--ref REF` pins a default ref.
- `--param NAME[=DEFAULT]` declares a template parameter; repeatable.
- `--network DOMAIN` grants a network domain; repeatable.
- `--env-allow ENV` grants an environment variable; repeatable.
- `--shell-allow COMMAND` grants a shell argv prefix; repeatable.
- `--allow-git-mutation` lets the worker mutate git state.
- `--budget-wall-clock`, `--budget-max-iterations`,
  `--budget-max-actions`, and `--budget-max-provider-calls` set template budget.

`template suggest` previews a learned template from remembered approvals. Add
`--save` to insert it into the template registry.

`template rename` preserves the template contents and updates schedule bindings
that referenced the old name.

## Scheduling

```sh
skep schedule add NAME REPO "instructions" --every 1d [options]
skep schedule add NAME REPO --template TEMPLATE --every 1d [--param KEY=VALUE]
skep schedule list
skep schedule remove NAME
skep tick [--worker-cmd COMMAND] [--auto-approve]
```

- `schedule add` creates a recurring direct run or template-bound run.
- `--every` accepts intervals such as `30s`, `5m`, `2h`, or `1d`.
- `tick` dispatches all due schedules and is intended for cron or another
  scheduler.

## Branches and Pull Requests

```sh
skep repo refresh REPO

skep branch list   REPO
skep branch create REPO NAME [--from REF]
skep branch merge  REPO --source REF --into BRANCH
skep branch push   REPO NAME
skep branch delete REPO NAME [--remote]

skep pr list  REPO [--state open|closed|merged|all]
skep pr open  REPO --branch NAME [--base main] [--title TITLE]
skep pr merge REPO NUMBER [--method merge|squash|rebase]
skep pr close REPO NUMBER [--delete-branch]
```

These are the operator's half of the git surface (ADR 0050). They run
supervisor-side on your own credentials; workers can never reach a remote or
move a branch, and neither can the Queen's shell.

- `repo refresh` fetches origin and fast-forwards the default branch. Run it
  before reasoning about how stale anything is.
- `branch merge` is for catching a branch up (`--source origin/main`) or
  consolidating several task branches onto one before a single PR. It refuses
  to merge **into** the default branch, and a conflict aborts cleanly naming
  the conflicting files — nothing is left half-merged.
- `branch push` is fast-forward only. A non-fast-forward push failing is
  information, not an obstacle; there is no force flag anywhere.
- `pr merge` is the only way the base branch ever moves.

A typed command is your decision and acts immediately — confirmation cards
exist because a *model* proposed the action, not a human.

## Serve

```sh
skep serve [--host HOST] [--port PORT] [--worker-cmd COMMAND] [--auto-approve]
```

`serve` starts the local HTTP API and browser dashboard. It prints the URL and
access token on startup.

## Chat

```sh
skep chat [--url URL] [--chat ID] [--continue] [--thinking] [--oneshot MESSAGE]
```

`chat` is the terminal face of the same Queen the web UI talks to. It is an
operator surface with the serve token — the same trust class as the browser,
subject to the same confirmation cards, 409s, and audit actors. It needs the
daemon: run `skep serve` first. The REPL never opens the store directly.

- Assistant proposals render as inline cards: `[y] confirm  [n] deny  [s] skip`.
  A skipped card stays pending and shows up in the web UI — the two surfaces
  share one store, so "skip here, click there" is expected, not a leak.
- Confirmed dispatches auto-tail the run's events at the prompt; Ctrl-C stops
  watching, never the run. A run that stops at an approval gate prompts
  `[a] approve once  [b] approve + remember  [d] deny  [s] skip` right there.
- `--continue` resumes the most recent chat; `--chat <id>` resumes a specific
  one and replays its recent transcript.
- `--oneshot "message"` sends one message to a new chat, streams the reply,
  and exits 0 — no prompts; cards are skipped and reported by id (the
  scripting/cron face).

Messages starting with `/` are the command deck — deterministic operator
commands the model never sees, identical to the web deck:

| Command | Purpose |
|---|---|
| `/help` | list the command deck |
| `/policy [repo]` | effective policy for a repo |
| `/repos` | registered repos |
| `/runs [n]` | recent runs |
| `/approvals` | the pending approval queue |
| `/state <repo>` | a repo's git state |
| `/setup <repo>` | bind a repo to a trusted project (confirm card) |
| `/phase <project-id> <phase>` | move a project's trust phase (confirm card) |
| `/land <task-id> [branch]` | land a completed run's patch (confirm card) |
| `/approve <review-id> [branch]` | approve a pending review (confirm card) |
| `/deny <review-id>` | deny a pending review (confirm card) |
| `/workon <path>` | make a local directory a first-class workspace (confirm card) |

## Skills

```sh
skep skill ...
```

The skill lifecycle promotes tested learned recipes into the same template
registry. Use `skep skill --help` for the full subcommand list.

## Projects and Policy

```sh
skep project preview REPO [--pack PACK] [--phase PHASE]
skep project setup   REPO [--engine ENGINE] [--verify-command CMD] [--group NAME]
skep project list
skep project show    PROJECT_ID
skep project set-phase PROJECT_ID PHASE
```

`project setup` binds a repo to a policy pack and infers a `verify_command`
from the repo's own entry point (v91); an explicit `--verify-command` always
wins. `set-phase` moves the trust-ramp defaults (build → maintain).

## Memory

```sh
skep memory list|search|show|forget
skep memory proposals|propose|approve|reject
skep memory approve-batch|reject-batch
```

Durable memory is proposal-gated: nothing becomes standing context without an
approval, and `forget` is a soft delete that keeps the audit trail.

## Channels and Providers

```sh
skep channel status
skep provider list
skep provider health
skep provider add ID --protocol P --base-url URL --model M [--api-key-env ENV]
                     [--cost-class C] [--order N] [--host H]... [--activate]
skep provider use ID
skep provider remove ID
```

`channel status` prints one honest line per messenger channel: config state,
secret presence, last delivery. `provider` reads the registered LLM providers
and their latest health probes. `provider add/use/remove` (v108) are the
registry's write faces — the same verbs as `POST/DELETE /api/providers` and
the carded chat tools. `--api-key-env` names the env var holding the key;
key values never ride the command line, and `use` writes the profile through
to the saved assistant config so the Queen actually speaks it.

## Nodes and Ops

```sh
skep node add|list
skep ops run CHECK --node NODE [--approve] [--arg KEY=VALUE]
skep ops schedule ...
```

Ops checks resolve against a registered node and execute only with
`--approve` — the CLI face of the carded ops lane.

## Imports

```sh
skep hermes import [--hermes-home PATH] [--dry-run]
```

Stages Hermes memory, skills, and sessions behind skep's existing gates —
imported items arrive as proposals and drafts, never as standing state.

## Dashboard

```sh
skep start [--host HOST] [--port PORT]
```

`start` serves the legacy dashboard; `skep serve` is the full supervisor
daemon (web UI, chat, ticker) and is what you normally want.
