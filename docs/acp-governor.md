# The ACP governor

skep ships as a transparent ACP proxy — `skep acp govern -- <agent command>`
— that any ACP client spawns in place of the agent. It relays every
JSON-RPC message unchanged except the agent's permission ask, which it
answers from the same `decide()` every skep run uses, and what it cannot
decide it forwards to the human behind the client. Every verdict, every
tool call the agent ran unasked, and every answer the human gave lands in
skep's decision ledger (ADR 0054).

## Install

The Air smoke — a new machine, this doc alone:

```sh
pipx install skep && skep onboard --template builder --yes
skep acp install-hint
```

A fresh install asks for everything (`ask`, reason `no_baseline`) until a
baseline is approved; a home from an earlier skep keeps deciding as before,
and `skep doctor` says how to approve one.

On Debian/Ubuntu (PEP 668, an "externally managed" system Python) use
`pipx` or `uv tool install skep` — never `pip install` into the system
Python. Either puts `skep` on your PATH. `skep onboard` connects a model,
registers a work directory, records the sandbox backend, and locks
`builder` as the policy baseline the governor answers from (see
[onboarding.md](onboarding.md); add `--yes` to take the defaults without
prompts).

Then ask the agent what it will do under skep:

```sh
skep acp doctor -- npx -y @agentclientprotocol/claude-agent-acp
```

The doctor sends `initialize` and a dry `session/new` (never a prompt) and
prints:

- `agent:` the name and version the adapter reports;
- `protocol:` the ACP version it answered (`v1` or `v2`);
- `preset:` the mapper row it gets — `generic (unknown agent: …)` means
  skep has no `rawInput` reader for it and every ask it cannot read is
  forwarded to you;
- `modes:` the modes the agent advertises, the default marked;
- `rings:` which rings can be live (see below);
- `WARNING: default mode … never asks` when the default mode is one of
  `auto`, `agent`, `agent-full-access`, `bypassPermissions` — in that mode
  skep only observes.

Do the same with `npx -y @agentclientprotocol/codex-acp`.

## T3 Code

Settings → Providers → Add provider → Show more → **Local ACP command**.
The dialog takes an executable plus one literal argument per row, and T3
launches it directly — no shell, no PATH lookup — so the executable must be
an absolute path. `skep acp install-hint` prints exactly what to paste,
under one line naming the baseline the governor answers from (or, with none
approved, `run skep onboard first — the governor answers from an approved
baseline`):

```
baseline: builder approved 2026-11-09T12:00:00Z sha256 0123456789ab
Local ACP command — claude-agent-acp
  executable: /absolute/path/to/skep
  arguments, one row each:
    acp
    govern
    --
    npx
    -y
    @agentclientprotocol/claude-agent-acp
```

and the same for `@agentclientprotocol/codex-acp`. Click **Add argument**
once per line: a row is one literal argument, so a single row holding
`acp govern -- …` reaches skep as one unrecognised word and the provider
reports "could not create a test session". Add one provider instance per
agent. With an adapter installed globally the last three rows become its
binary name (`claude-agent-acp` / `codex-acp`).

T3 respawns the agent after a thread goes idle and continues the same
session with `session/resume` (v2) or `session/load` (v1); the governor
binds that request's `cwd` as it does `session/new`'s, and everything the
agent reports between that request and your next prompt is replayed
history — cached for joins, never recorded as observed rows. Claude's
adapter replays the thread on a resume too, as `tool_call_update` frames
carrying the original ids.

## The two rings, honestly

- **Advisory — the permission ask.** skep decides `session/request_permission`
  only when the agent's own mode chooses to ask. In Claude `auto` and Codex
  `agent` (their defaults) the agent never asks, so skep only observes.
  Even in the asking modes (Claude `default`, Codex `read-only`) both
  adapters run read-only commands such as `git status` without asking;
  those become `observed` rows.
- **Hard — client-mediated execution.** Only on ACP v1, and only with a
  client that advertised `fs.writeTextFile` or `terminal` at `initialize`:
  then the agent's `terminal/create` and `fs/write_text_file` pass only on
  `allow`; anything else is a JSON-RPC error, because that hop has no human
  to forward to (A10). T3 advertises neither, so on T3 the governor is
  advisory only.

## A demo policy

Bind a test repo to a project and give it a shell allow list:

1. Create a policy group holding the allow list. skep's CLI has no
   standing shell-allowlist write today; the group is written through the
   web UI (Policies page → policy groups → new group) or its REST route
   with the token `skep serve` prints:

   ```sh
   curl -X PUT http://127.0.0.1:8765/api/policy-groups/acp-demo \
     -H "x-skep-token: $TOKEN" -H 'content-type: application/json' \
     -d '{"allowed_shell_commands": [["git","status"],["ls"],["pnpm","test"]]}'
   ```

   (From chat, the Queen's `allow_shell_command` with `repo=` writes the
   same grant into one project's list behind a confirm card.)
2. `skep project setup --strategy trusted_local_dev --group acp-demo <repo>`.
   `fs: workspace` is already granted by the strategy, so writes inside
   the repo are allowed.

What that policy answers:

| The agent asks for | skep's answer |
|---|---|
| `git status`, `ls`, `pnpm test` | allow (`shell:<command>`) |
| a write inside the repo | allow (`fs: workspace`) |
| `rm -rf build` (unlisted) | unmatched (ask) → forwarded: the human in T3 decides |
| `ls -la; rm -rf build` | unmatched (ask) → forwarded: a chained line is decided one command at a time, and `rm` is unlisted |
| `ls $(cat names)`, `sh -c '…'` | unmatched (ask) → forwarded: text a prefix matcher cannot read matches nothing |
| `git push` | floor deny, before the client sees it |

A repo's run policy compiles **allow lists only** (`allowed_shell_commands`,
`fs: workspace`). There is no project-level deny rule for shell: "ask on
`rm`" means leaving `rm` unlisted, and the only explicit shell deny is the
floor (remote git, ADR 0044).

## What you will see

The governor writes one line to the client's log at startup; it starts
with `skep acp:`, e.g.

```
skep acp: governing claude-agent-acp (ACP v2) — rings: advisory — home …
```

The agent's own stderr passes through to the same log. T3's approval card
appears only for asks skep forwarded; allows and denies are answered
before T3 sees them.

The ledger:

```sh
skep policy decisions --actor 'acp:*'
```

returns rows newest first, plus a `summary` that counts decisions by
verdict and `observed`/`forwarded` rows apart (they are not decisions).
Rows of the kinds a live session produces (trimmed):

```json
{"actor": "acp:codex-acp", "scope": "shell", "action": "run", "value": "git status",
 "source": "observed", "agent": "codex-acp"}
{"actor": "acp:claude-agent-acp", "scope": "filesystem", "action": "write", "value": "workspace",
 "source": "run", "verdict": "allow", "decided_by": "dispatch/fs:workspace"}
{"actor": "acp:codex-acp", "scope": "shell", "action": "run", "value": "rm -rf build",
 "source": "run", "verdict": "require_approval", "rule_id": "unmatched",
 "decided_by": "default/unmatched"}
{"actor": "human@<client>", "scope": "shell", "action": "run", "value": "rm -rf build",
 "source": "forwarded", "agent": "codex-acp", "outcome": "allow_once", "note": "unmatched"}
{"actor": "acp:codex-acp", "scope": "shell", "action": "run", "value": "git push",
 "source": "floor", "verdict": "deny", "rule_id": "floor.worker-git-hard-denies"}
```

`human@<client>` is the client's own name from its `initialize`. Ask why
any one of them answered as it did:

```sh
skep policy why <repo> shell run "git push"
```

```json
{
  "floor": "remote git commands never run from chat — skep lands patches after approval; …",
  "run": {
    "considered": [],
    "decided_by": "floor/floor.worker-git-hard-denies",
    "rule_id": "floor.worker-git-hard-denies",
    "verdict": "deny"
  },
  …
}
```

The floor answered before any document was read (`considered` is empty);
no grant, group or project overlay can change it.

## Tracing the wire

Set `SKEP_ACP_TRACE=/path/to/trace.ndjson` in the provider's environment
(T3: the provider's **Environment** section) and the governor appends every
frame it relays, both directions, in the fixture envelope the tests use:

```json
{"dir": "c2a", "raw": "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"session/load\", …}", "t": 1793997000.1}
```

Nothing is written without the variable. The file is the agent's and the
client's exact words — scrub paths and ids before sharing it — and
`tests/fixtures/acp/` loads it unchanged, so a session that misbehaved
inside a client becomes a golden case.

## What this does not do

- It does not sandbox the agent. T3 spawned it; T3's (or the agent's)
  sandbox is the wall. skep governs the wire (I12).
- It does not see tool calls the agent neither asks about nor routes
  through the client. In Full access with an agent that runs its own
  shell, skep observes `tool_call` updates and decides nothing.
- It does not verify results. The G10 re-verify is a worker-landing
  concept (I1/I2) and nothing lands through the proxy. Verification of
  externally-driven sessions is a later ADR, if ever.
- It does not read or write T3 state. No T3 API, no T3 database, no
  pairing token.

skep never answers `allow_always` or `reject_always` (D6): a standing grant
is a policy write, made through skep's own faces with the tier named.

From a dev checkout instead of an install:
`uv run --project /path/to/skep skep acp govern -- …`.

See also [`cli-reference.md`](cli-reference.md#skep-acp) and
[`policy-hook.md`](policy-hook.md).
