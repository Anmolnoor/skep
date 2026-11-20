# skep

**Unattended coding agents you don't have to trust. skep sandboxes the agent, re-runs your tests itself, and holds the patch for your approval.**

[![PyPI](https://img.shields.io/pypi/v/skep)](https://pypi.org/project/skep/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://pypi.org/project/skep/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

```sh
pipx install skep   # or inside a venv: pip install skep
```

![skep — the supervisor for AI coding agents: sandbox, agent runs, verify, you approve, audit trail](docs/assets/skep-cover.png)

skep is a local-first supervisor for AI coding agents — Claude Code, Codex,
Aider, pi, or its own built-in worker on a model you choose. Every agent works
in a disposable worktree behind a contract. Claude Code, Codex, Aider and pi
always run inside an OS sandbox; skep re-runs your project's pinned test
command on a clean checkout whatever the agent claims; and the patch waits for
your approval. Your default branch never moves without you: landing without
an approval is opt-in, takes only a re-verified patch, and writes only a
`skep/*` branch. It is single-operator by design, and has been daily-driven in
the field since mid-July 2026.

![skep supervising a run: sandboxed worker, independent verify, patch approval](docs/assets/skep-demo.gif)

**Contents:** [Why](#why-skep-exists) · [How a run flows](#how-a-run-flows) ·
[Install](#install) · [Quickstart](#60-second-quickstart) ·
[Security model](#the-security-model) · [Workers](#workers) ·
[Local models](#local-models-and-the-brain-dial) ·
[Beyond the patch](#beyond-the-patch) · [Command deck](#the-command-deck) ·
[Messengers](#messenger-channels) · [Status](#status) ·
[Honest limits](#honest-limits) · [Comparison](#how-skep-compares) ·
[Docs](#documentation) · [Contributing](#contributing)

## Why skep exists

Raw coding agents move fast, and wide. They are genuinely productive — and
they can also:

- **Mutate repos directly.** An agent can edit, commit, or push before you
  have read a single line of the actual diff.
- **Run broad commands.** Package installs, arbitrary shell, and outbound
  network calls happen without an explicit boundary.
- **Overstate verification.** "Tests passed" from the same process that wrote
  the code is not an independent check. A model saying it is a sentence, not
  a proof.

skep's answer is not a smarter agent. It is a control plane: the agent stays
exactly as capable as before, but every capability runs behind a contract you
define once and the system enforces every time.

## How a run flows

```
1. WORKER STARTED       agent dispatched into an isolated git worktree
2. FILES CHANGED        edits + shell commands stream as durable events
3. WORKER CLAIM         "tests passing" — recorded, not believed
4. INDEPENDENT VERIFY   skep replays the patch on a clean copy and
                        re-runs the pinned verify command itself
5. PATCH READY          evidence + diff await your approval;
                        approval lands on branch skep/<task_id>, never main
```

Two independent results must agree before a run is confirmed: the worker said
*passed*, and skep's own replay said *passed*. Either one alone is not enough.

## Install

```sh
pipx install skep
```

Or `uvx skep` to try it without installing. On Debian/Ubuntu and other
PEP 668 distros, the system Python refuses bare `pip install` with an
externally-managed-environment error — install skep as an app instead:
`sudo apt install pipx && pipx install skep`, then `pipx ensurepath`.
Inside a virtual environment, `pip install skep` works as ever. Please
don't reach for `--break-system-packages` — least privilege applies to
installers too.

From a checkout, `bash scripts/install.sh` detects the source tree and runs
`uv sync`. A `Dockerfile` and `docker-compose.yml` also ship in the repo
(`make image`) for containerized runs.

**Requirements:** Python 3.12+, macOS or Linux. Linux sandboxing needs
`bubblewrap` (`sudo dnf install bubblewrap` / `sudo apt install bubblewrap`);
macOS uses Seatbelt out of the box. All state lives locally under `~/.skep`:
no skep account, no skep service, no telemetry. With a hosted model, prompts
(including code) go to that provider.

## 60-second quickstart

```sh
pipx install skep
skep onboard   # model, work directory, sandbox, policy baseline, role
```

`skep onboard` is the guided first run: five resumable steps that end with a
locked policy baseline, then it starts the daemon and opens the chat
([`docs/onboarding.md`](docs/onboarding.md)). A fresh home asks about
everything until a baseline is approved, and a run with no model configured
fails, so start here. Afterwards:

```sh
skep serve   # daemon + web UI at http://127.0.0.1:8765
skep chat    # the same conversation in your terminal
```

Tell the chat what repo to work on and what you want done — approvals,
cards, and live run events arrive inline. Or skip the conversation:

```sh
skep run /path/to/your-repo "fix the failing test" --execution-mode sandbox
skep review <task_id>             # inspect the patch + evidence
skep review <task_id> --approve   # lands on branch skep/<task_id>, never main
```

To hand the task to Claude Code or Codex instead of the built-in worker, the
project needs the engine and a pinned verify command, and your shell needs the
engine's auth environment variable: [`docs/guide-unattended-run.md`](docs/guide-unattended-run.md). The
complete first-run path, worker setup, and sandbox notes are in
[`docs/quickstart.md`](docs/quickstart.md).

## The security model

The threat model is simple: **the agent is competent and untrusted.** It
works in an isolated git worktree and produces a patch plus evidence. Landing
the patch is the commit, and by default it takes your approval. Your default
branch never moves without you.

- **Sandbox.** macOS Seatbelt or Linux bubblewrap (with Landlock) confines
  every sandbox-mode run, and a run is refused when no backend applies.
  Claude Code, Codex, Aider and pi are always forced into it. The built-in
  worker can also run in `workspace` mode, which has no OS wall.
  ([`docs/sandboxing.md`](docs/sandboxing.md))
- **Verify.** skep re-runs the project's pinned verify command on a clean
  copy before you review. The worker's own command is used only when the
  project pins nothing, and the record says which it was.
  ([`docs/verification.md`](docs/verification.md))
- **Approve.** A patch, not a promise. Workers cannot push, pull, fetch, or
  switch branches; no permission grant overrides that. Auto-landing is
  opt-in, takes only a re-verified patch, and writes only `skep/*` branches.
  ([`docs/approvals.md`](docs/approvals.md))
- **Audit.** Runs, events, patches, re-verifications and approvals are
  durable local records, and every policy decision goes to a decision log.
  For an external engine (Claude Code, Codex, ...) skep keeps the evidence for
  every run (the command it launched, the patch, the sandbox profile, the
  re-verify), not each tool call the agent made inside the sandbox
  ([ADR 0047](docs/adr/0047-cli-agent-engines.md)).

The non-negotiable rules behind all of this are written down in
[`docs/invariants.md`](docs/invariants.md) — that file is the part of the
project I most want challenged.

When a worker needs more permission than policy grants, skep stops at an
approval gate: approve once, deny, or approve-and-remember so similar future
runs move with less friction while the ledger still records everything.
Templates capture only the permissions that actually worked; the default
policy never widens on its own.

## Workers

| Engine | What it drives | Notes |
|---|---|---|
| `claude_code` | Anthropic's CLI agent | adapter ships in the box |
| `codex` | OpenAI's CLI agent | adapter ships in the box; an API key, or an opt-in ChatGPT plan login |
| `aider` | Aider | adapter ships in the box |
| `pi` | the pi coding agent, headless | adapter ships in the box |
| `builtin` | skep's own worker | no external CLI required; uses the model provider you configure, a local Ollama model included |

All engines run under the same contract: the same worktree, the same
re-verification, the same approval gate. The four external engines always run
in the sandbox and are refused until the project pins a verify command;
Claude Code and Codex are also refused until their auth environment variable
is set. The adapter surface for bringing your own worker is in
[`docs/workers.md`](docs/workers.md).

## Local models and the brain dial

The chat "Queen" that plans and dispatches work is deliberately tool-driven
rather than cleverness-driven, so it ships tuned for a small local model via
Ollama — and the model is a dial, not an identity. Five provider protocols
are supported — native Ollama, OpenAI-compatible, Anthropic, the OpenAI
Responses API, and AWS Bedrock — with a preset catalog (`skep provider
presets`) covering ~30 named providers ([`docs/brain.md`](docs/brain.md)).
The core loop — plan, dispatch, sandbox, verify, approve, audit — needs no
skep account and no skep service. Whether anything leaves your machine depends
on the model: a model served by a local Ollama daemon stays local, while the
default `glm-5.2` preset is served through ollama.com, a hosted provider.

## Beyond the patch

Coding runs are the spine, but skep is a full assistant living behind the
same gates:

- **Memory and notes.** Tell skep to remember a decision or a gotcha and it
  becomes durable, curated memory that grounds future work; lighter
  observations land in searchable notes. A vault bridge syncs notes into
  Obsidian as linked markdown — your knowledge graph, not a chat silo.
- **Skills.** Reusable procedures born from confirmed work and curated by
  the operator. skep never fetches skills from the internet or auto-imports
  community packs — skills are local, curated, and trusted. You decide what
  it knows.
- **Governed research.** Web research from allow-listed sources, with
  evidence per source and unreachable sources named explicitly.
- **Maintenance audits.** Bump unsafe dependency pins against an advisory
  set and re-run the suite — deterministic, LLM-free, offline.
- **MCP tools.** Register any MCP server and its tools run through the same
  policy layer and approval gates as skep's own. First-party mail and
  calendar MCP servers ship in the package
  ([`docs/assistant-tools.md`](docs/assistant-tools.md)).
- **Schedules and digests.** "Every morning at 9, review my repos for
  dependency drift" becomes a ticker with last-run/next-run state in the
  dashboard.

skep introduced these itself, in its own words, in a post published through
its own approval gate:
[*I'm Live — Come Build with Me*](https://anmolnoor.com/blog/skep-is-live).

## The command deck

The chat composer doubles as a deterministic command line: any message starting
with `/` is parsed by the UI and executed against the same HTTP API the buttons
use — the assistant's model never sees it. `/help` lists the deck; reads
(`/policy`, `/state`, `/runs`, `/approvals`, `/repos`) render immediately, and
mutations (`/setup`, `/workon`, `/phase`, `/land`, `/approve`, `/deny`,
`/schedule`) show the same confirmation card a model proposal would, audited
under actor `operator-command`. `/trust` clears a chat's taint (the trifecta
guard's "this chat read private data / took in outside content" bits) — an
operator command only, never offered to the model. `/workon <path>` makes any local directory a
first-class workspace: a confirmed `git init` + baseline commit if needed, then
the same trusted-project setup a registered repo gets. The same deck works in
the terminal: `skep chat` (see [`docs/cli-reference.md`](docs/cli-reference.md)).

## Messenger channels

Operate skep from Telegram, Slack, or Discord: enable a channel in Settings,
allow-list your chat id, paste the bot token, and an allow-listed message runs
the same Queen turn — with the same approval gates — as the web composer. When
a channel is confirm-enabled, low-risk actions resolve inline; shell commands,
policy changes, and landings are never confirmable from a messenger. See
[`docs/how-it-works.md`](docs/how-it-works.md#messenger-channels).

## Status

As of 1.5.0:

| Works today | Being wired up | Opinions pending code |
|---|---|---|
| Sandbox-mode runs (Seatbelt, bubblewrap with Landlock, opt-in podman), re-verification with the pinned verify command, patch approval, local audit records | Nothing is tracked in public issues today; open work is in the [pull requests](https://github.com/Anmolnoor/skep/pulls) | Multi-operator / team use |
| One policy engine (`skep policy why / explain / decisions`), the locked baseline, `skep onboard`, roles and delegation | | Hosted service |
| Claude Code, Codex, Aider and pi as engines; the built-in worker on your model provider | | Ambient/self-directed learning |
| The Claude Code policy hook and the ACP governor, for agents you run elsewhere | | |
| Chat Queen (web, terminal, Telegram/Slack/Discord) with carded mutations | | |
| Schedules, digests, MCP tools, Agent Skills shelves, memory + notes | | |

CI runs on Linux and macOS. The macOS leg runs lint, types, unit tests and the
smoke test; coverage, the sandbox smoke and the hygiene scan run on Linux only.

## Honest limits

skep makes agents safer. It is not a silver bullet, and it is worth stating
exactly where the boundary stops:

- **Reads are not fully confined.** Writes are confined to the run's
  worktree. Reads outside it are mostly open; a list of credential paths (the
  secret floor) is hidden, and workers get environment variables by an
  allowlist of names.
- **Workspace mode has no sandbox.** The built-in worker may run there; the
  external engines never do.
- **Verification is only as strong as the verify command.** If a project's
  checks are weak, skep can only confirm that weak checks exit zero.
- **Sandboxes share the host kernel.** Seatbelt and bubblewrap confine a
  competent-but-untrusted agent; a kernel-exploit-wielding adversary is out
  of scope. If you need VM isolation, run skep inside one — or contribute a
  micro-VM backend.
- **Your machine is still your machine.** Malware, a compromised OS, or
  secrets already committed to the repo are out of scope.

The full model is at
[skep.anmolnoor.com/security.html](https://skep.anmolnoor.com/security.html).
Holes poked in it are the most valuable issues you can file.

## How skep compares

Tools in this space tend to lead with one thing: cloud agents run the task on
someone else's infrastructure and hand back a pull request; container and
micro-VM harnesses confine execution; agent orchestrators parallelize
worktrees. skep's bet is a combination, on your own machine: an OS sandbox
for the agent, **and** skep's own re-run of your pinned test command on a
clean copy, **and** a patch that waits on a `skep/*` branch for your approval,
with the records on your own disk. It is not the strongest isolation (a VM
is), and a cloud agent's pull request already runs your CI. The current
comparison, with each claim's limit, is at
[skep.anmolnoor.com/why-skep.html](https://skep.anmolnoor.com/why-skep.html);
[`docs/competitive-analysis.md`](docs/competitive-analysis.md) is the
pre-launch writeup, kept as history.

If you know a project that already does all three locally, open an issue —
genuinely, I want to compare notes.

## Project design source of truth

Internal design decisions, plans, operating rules, and recovered history are maintained in the private [SKEP design archive](https://github.com/Anmolnoor/skep-design). Contributors and coding agents should consult that archive before proposing decisions, making plans, or changing project behavior. The public ADRs and user documentation remain here; the private archive is canonical for internal planning and design context.

## Documentation

The curated index lives at [`docs/README.md`](docs/README.md) — start with
[`docs/quickstart.md`](docs/quickstart.md). For agents: [`SKILL.md`](SKILL.md) teaches an agent
to install and operate skep, and [`docs/agent-guide.md`](docs/agent-guide.md)
how to explain it to a human. Worker adapters and the custom
worker contract are in [`docs/workers.md`](docs/workers.md); verification
semantics in [`docs/verification.md`](docs/verification.md); sandbox
backends in [`docs/sandboxing.md`](docs/sandboxing.md); configuration in
[`docs/configuration.md`](docs/configuration.md); running on a fresh machine
in [`docs/how-to-use-on-new-machine.md`](docs/how-to-use-on-new-machine.md).
Decisions are recorded in [`docs/adr/`](docs/adr/); the post-launch roadmap
in [`docs/post-launch.md`](docs/post-launch.md).

Website: [skep.anmolnoor.com](https://skep.anmolnoor.com)

## Contributing

skep is single-operator by design, but it is no longer a single-person
project by intention. It was built solo in five months across three
attempts, and the surface area — sandbox backends, worker adapters,
verification semantics, chat surfaces — is now bigger than one engineer.

Especially welcome: skeptical review of the security model and
[`docs/invariants.md`](docs/invariants.md), bubblewrap/Seatbelt hardening,
Docker and micro-VM sandbox backends, new worker adapters, and
portability reports from machines unlike the ones it grew up on.
Development setup and the PR guide are in
[`CONTRIBUTING.md`](CONTRIBUTING.md); vulnerability reports via
[`SECURITY.md`](SECURITY.md) — please don't open public issues for those.

skep was built by supervised agents. It would like more supervisors.

## License

MIT.
