# skep

**Govern your AI coding agent. Sandbox it, verify its work, approve before it lands.**

skep is a local-first supervisor for AI coding agents — Claude Code, Codex,
Aider, or its own built-in worker. Every agent works behind a contract:
sandboxed execution in a disposable worktree, independent re-verification of
whatever the agent claims, and a patch that reaches your repository only when
you approve it. It is v1, single-operator by design, and has been
daily-driven in the field since 2026-07-17.

## Install

```sh
pip install skep
```

Or `uvx skep` to try it without installing. From a checkout,
`bash scripts/install.sh` detects the source tree and runs `uv sync`.
Linux sandboxing needs `bubblewrap` (`sudo dnf install bubblewrap` /
`sudo apt install bubblewrap`); macOS uses Seatbelt out of the box.

## 60-second quickstart

```sh
skep serve   # daemon + web UI at http://127.0.0.1:8765
skep chat    # the same conversation in your terminal
```

Tell the chat what repo to work on and what you want done — approvals,
cards, and live run events arrive inline. Or skip the conversation:

```sh
skep run /path/to/your-repo "fix the failing test" --execution-mode workspace
skep review <task_id>             # inspect the patch + evidence
skep review <task_id> --approve   # lands on branch skep/<task_id>, never main
```

The complete first-run path, worker setup, and sandbox notes are in
[`docs/quickstart.md`](docs/quickstart.md).

## The security model

The agent never touches your repository directly. It works in an isolated
git worktree, produces a patch plus evidence, and the patch lands only
through your approval — landing IS the commit.

- **Sandbox.** macOS Seatbelt or Linux bubblewrap confines every worker;
  filesystem and network access are policy, not habit.
- **Verify.** skep re-runs the project's pinned verify command on a clean
  copy before you review — the worker's own claim is never the evidence.
- **Approve.** A patch, not a promise. Workers cannot push, pull, fetch, or
  switch branches; no permission grant overrides that.
- **Audit.** Every run, event, shell command, approval, and decision is a
  durable record you can inspect later.

When a worker needs more permission than policy grants, skep stops at an
approval gate: approve once, deny, or approve-and-remember so similar future
runs move with less friction while the ledger still records everything
([`docs/approvals.md`](docs/approvals.md)).

## Status

| Works today | Being wired up | Opinions pending code |
|---|---|---|
| Sandboxed runs, re-verification, patch approval, full audit trail | PyPI release automation (this release) | Multi-operator / team use |
| Chat Queen (web, terminal, Telegram/Slack/Discord) with carded mutations | macOS CI matrix | Hosted service |
| Claude Code / Codex / Aider adapters + built-in and Ollama workers | Closing the remember→improve loop (usage signals, recurrence counting) | Ambient/self-directed learning |
| Schedules, digests, MCP tools, Agent Skills shelves | | |

## The command deck

The chat composer doubles as a deterministic command line: any message starting
with `/` is parsed by the UI and executed against the same HTTP API the buttons
use — the assistant's model never sees it. `/help` lists the deck; reads
(`/policy`, `/state`, `/runs`, `/approvals`, `/repos`) render immediately, and
mutations (`/setup`, `/workon`, `/phase`, `/land`, `/approve`, `/deny`,
`/schedule`) show the same confirmation card a model proposal would, audited
under actor `operator-command`. `/workon <path>` makes any local directory a
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

## Documentation

The curated index lives at [`docs/README.md`](docs/README.md) — start with
[`docs/quickstart.md`](docs/quickstart.md). Worker adapters and the custom
worker contract are in [`docs/workers.md`](docs/workers.md); messenger
channels in [`docs/how-it-works.md`](docs/how-it-works.md#messenger-channels);
sandbox backends in [`docs/sandboxing.md`](docs/sandboxing.md). Decisions are
recorded in [`docs/adr/`](docs/adr/); the post-launch roadmap in
[`docs/post-launch.md`](docs/post-launch.md); development and PR guide in
[`CONTRIBUTING.md`](CONTRIBUTING.md); vulnerability reports via
[`SECURITY.md`](SECURITY.md).

Website: [skep.anmolnoor.com](https://skep.anmolnoor.com)

## License

MIT.
