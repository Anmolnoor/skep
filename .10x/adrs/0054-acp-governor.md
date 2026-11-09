# ADR 0054 (draft) — the ACP governor: skep answers another client's permission hop

**Status:** draft (`.10x/adrs/`); house number 0054 on acceptance into `docs/adr/`.
**Feature:** `acp-governor` — Restart Plan Part B, re-scoped against what P3 shipped.
**Depends on:** ADR 0052 (registry, RSoP, `why`), ADR 0053 (engine v2, P3 `decide()` face).
**Related:** ADR 0044 (floor), 0047 (CLI-agent engines — where the authority boundary is), 0050 (one verb, three faces), 0046 (session approvals). Invariants I4, I5, I6, I7, I8, I9, I12, I13.

## Context

P3 made `decide()` reachable from outside and recorded (`policy_decide`,
`POST /api/policy/decide`, `skep policy decide|hook`). Its one external
caller is the Claude Code `PreToolUse` hook, which maps a tool payload onto
skep's closed `(scope, action, value)` vocabulary and renders the verdict as
`allow | deny | ask`. The mapping half (`policy_hook.py::hook_probe`) is
pure; the transport half is Claude Code's hook JSON on stdin/stdout.

The target this ADR is written for is T3 Code (`pingdotgg/t3code`): a
control surface over Claude Code, Codex, Cursor, Grok, OpenCode and any
agent in the ACP registry. T3's permission model is four generic modes
(Supervised, Auto-accept edits, Auto, Full access; default Full access)
translated per provider into native approval/sandbox knobs. Its own users
have filed the gap skep fills — least-privilege profiles the four modes
cannot express (discussion #6976) and a provider-agnostic "approve for me"
mode (#7340). For ACP agents T3's doc says the agent runs its tools in its
own mode and **T3 answers the agent's approval requests by the permission
mode**. That approval request is `session/request_permission`, and it is
the one hop every ACP agent shares.

The CLI-engine lesson (ADR 0047) applies in reverse here: when skep is not
the one spawning the agent, the sandbox is not skep's wall either. What
skep can govern is exactly what crosses the wire it sits on — and the
record must say so (I8, I12).

## The decision in one line

skep ships as a **transparent ACP proxy** — `skep acp govern -- <agent
command>` — that any ACP client spawns in place of the agent. It forwards
every JSON-RPC message unchanged except the permission and client-mediated
execution hops, which it answers from `policy_decide()`; what it cannot
decide it forwards to the human behind the client; every verdict, including
the human's, lands in the P3 ledger.

## Decisions

### D1 — proxy, not plugin: the client is not modified

Three places skep could sit were considered:

| Where | Needs T3 changes | Covers Codex/Cursor/Grok/registry agents | Hard enforcement point |
|---|---|---|---|
| (a) ACP proxy between client and agent | **no** — point the agent command at skep | **yes**, anything that speaks ACP | `fs/*`, `terminal/*` client methods (D4) |
| (b) a T3 provider shim calling `POST /api/policy/decide` | yes — T3 is not accepting large features | yes | T3's own hop |
| (c) agent-side hooks (today's P3 hook) | no | Claude Code only; Codex has no hook; T3 drives Claude through the Agent SDK, hook delivery unverified | PreToolUse |

(a) is chosen. (c) stays as a second ring for Claude Code where it works.
(b) is offered to T3 as a one-line integration if they want it — it is the
same REST face, nothing new to build. **I5 holds because the proxy carries
no decision logic:** it is `hook_probe` with an ACP front, calling the one
`policy_decide`.

### D2 — the proxy is a child of the operator's store, not of the daemon

`skep acp govern` reads `~/.skep` directly, the way `skep policy hook` does
(the P3 doc: "the daemon does not need to be running"). It never requires
`skep serve`. Reason: T3 spawns agents on session start; a governor that
fails when the daemon is down would either block every session or fall
open. It does neither — it reads the store, and if the store is unreadable
it answers `ask` with the reason (the hook's rule: never a silent allow).

### D3 — session binding: `cwd` from `session/new` is the repo, nothing else

ACP `session/new` carries `cwd` (and `mcpServers`). The proxy binds the
session to that path once and passes it as `repo` on every `decide()`,
exactly as the hook passes Claude Code's `cwd`. The run policy of the
bound project decides `shell` and `filesystem`; an unbound path answers
from the global layer and the record says `unbound` (I8, as P3 already
does).

Rejected: making T3's project/thread a new policy layer. T3's thread is a
UI grouping with no stable identity skep can verify; a layer keyed on it
would be a shadow scope (I5) that an unrelated client could not populate.
`cwd` is what every ACP client sends, so `cwd` is the key. If a session
changes directory mid-flight (`terminal/create` with a different `cwd`),
that call is decided against its own `cwd`, not the session's.

### D4 — two rings, named honestly

The proxy intercepts four agent→client methods and nothing else:

| Wire method | Ring | Maps to | Why this ring |
|---|---|---|---|
| `session/request_permission` | **advisory** | `toolCall.kind` + `rawInput`/`locations` → `(scope, action, value)` (D5) | Only sent when the agent's own mode decides to ask. In T3 **Full access** the agent may never ask, and the proxy sees nothing to decide. |
| `terminal/create` | **hard** | `shell.run <command args…>` | The agent asks the client to run it; the proxy can refuse before any process exists. |
| `fs/write_text_file` | **hard** | `filesystem.write <path>` → `workspace` when inside the session `cwd`, literal path otherwise | Same as above for writes. |
| `fs/read_text_file` | none | — | Reads are a contract permission, not a document rule (the P3 rule, I5). Forwarded untouched. |

The hard ring exists only when the client advertised `fs`/`terminal`
capabilities at `initialize` **and** the agent chose to route through them.
Agents that run their own shell inside their own process bypass it. The
proxy therefore prints, at startup, which rings are live for this
client/agent pair — the voice.py / ADR 0051 pattern: **every registration
prints its egress truth**.

**For T3 specifically (verified in `docs/user/providers-acp.md`, 2026-11-06):**
registry and local-command agents read, edit and run commands themselves
under their own sandbox; only Devin routes commands through T3's terminals.
So on T3 the hard ring is empty and the proxy is advisory-only. The
advisory ring is still worth having, because of how T3 answers: in
**Supervised** it shows the request, in **Auto-accept edits** it approves
edits, and in **Auto** and **Full access** it approves *everything the
agent asks* automatically. The proxy sits between the agent and T3, so it
sees the request before T3 rubber-stamps it — skep can deny in Auto what
T3 would have allowed. The pitch sentence: *T3's Auto says yes to
everything the agent asks; skep says yes by policy, and writes it down.*
What the agent never asks about (its own mode decided silently) skep
only observes.

`session/update` notifications of kind `tool_call` / `tool_call_update`
are not decided but **are written to the ledger** as observations with
`verdict: observed`, so the audit trail shows what ran even when nothing
was asked. This is the "logs everything" half of the governor claim and
the thing T3 does not do.

### D5 — mapping: ACP `ToolKind` → skep's closed vocabulary, joined on `toolCallId`

**The permission request does not carry the operation.** T3's recorded
Kiro transcript shows `session/request_permission.toolCall` as
`{toolCallId, status, title}` — no `kind`, no `rawInput`, no `locations`.
Those arrive earlier, in the `session/update` notification of kind
`tool_call` (and later `tool_call_update`s), keyed by the same
`toolCallId`; agent-specific detail rides `_meta` (Kiro: `fs_write` +
`resource` + `workspaceRoot`). So the proxy is **stateful**: it caches
every `tool_call`/`tool_call_update` per session by `toolCallId`, and on a
permission request joins the cached operation to decide. A request whose
`toolCallId` was never announced is `ask` with that reason.

`hook_probe` becomes one of two front-ends over a shared mapper. The ACP
front reads the joined `ToolCall`:

| ACP `kind` | rawInput / locations read | `(scope, action, value)` |
|---|---|---|
| `execute` | `rawInput.command` (+ `args` joined) | `shell.run <command>` |
| `edit`, `delete`, `move` | `locations[].path`, else `rawInput.path`/`file_path`, else `_meta` per agent preset | `filesystem.write <workspace|path>` — one `decide()` per location; any `deny` denies the call. Containment is symlink-aware and fails closed: realpath the existing ancestors before resolving `..` (T3's `clientPolicy.ts` has the reference algorithm; MIT, port the logic with notice) |
| `fetch` | `rawInput.url` host | `network.fetch <host>` |
| `search` with a URL-shaped input | host | `network.fetch <host>` |
| `read`, `search` (local), `think`, `other`, unknown | — | **no opinion → forward to the human** |

`rawInput` is agent-specific and optional in the spec. **A `kind` the
mapper knows with a `rawInput` it cannot read is `ask`, never `allow`**
(the hook's I8 rule), with the reason naming the missing field so the
agent's adapter can be fixed (I9). Per-agent `rawInput` readers (Claude
Code's ACP adapter, Codex, OpenCode) are data rows in one table, the ADR
0051 shape — presets, not code paths.

### D6 — verdict → ACP outcome; skep never grants "always"

| `decide()` verdict | `session/request_permission` answer | `terminal/create`, `fs/write_text_file` |
|---|---|---|
| `allow` | select the agent's `allow_once` option | forward to the client |
| explicit `deny` | select `reject_once`; if the agent offered none, return the `cancelled` outcome | JSON-RPC error to the agent naming the rule (I9); nothing forwarded |
| `require_approval` / unmatched default-deny | **forward the original request to the client** — the human in T3 decides | forward |
| floor hit (I4, ADR 0044) | `reject_once` before any document is read | error |

Never `allow_always` / `reject_always`: a standing grant is a policy
write, and policy is written through skep's own faces with the tier named
(v114 F2/F3, ADR 0046). An agent that is told "always" would stop asking,
and the advisory ring would go dark. The same reasoning as `--yes` being
one consent (I7).

When the request is forwarded and the human answers, the proxy records
the human's choice too (`actor: "human@<client>"`, `source: "forwarded"`)
so the ledger is a ledger (I13) and the v116 "every refusal teaches" loop
has the data to propose a rule.

### D7 — one new verb, three faces (ADR 0050)

- CLI: `skep acp govern [--repo PATH] [--rings auto|advisory-only] -- <agent cmd…>` — the thing a client spawns. `skep acp doctor -- <agent cmd>` runs `initialize` + a dry `session/new` and prints which rings would be live.
- REST: none new. `POST /api/policy/decide` is already the function; `GET /api/policy/decisions?actor=acp:*` is a filter on the existing ledger view.
- Chat: `/why` already answers the question the proxy asks; the `policy` tool gains an `acp_sessions` read listing live governed sessions (from the proxy's pidfile dir) — read-only, no new policy.

### D8 — packaging is part of the feature, not after it

The P3 hook's install line is `uv run --project /path/to/skep skep policy
hook`. That is a dev checkout. The T3 user gets exactly one command to
type, so B2 below ships: PyPI 1.2.0 actually published (verify 1.1.0
landed — the release workflow is wired but the record last confirms 1.0.2
on PyPI), `pipx install skep` as the documented path (PEP 668, the launch
day lesson), a `skep acp install-hint` that prints the exact T3 settings
to paste, and **engine v2 on by default for `shell` and `filesystem`** in
a fresh home. The switch stays per-scope for existing homes; a new home
has no v1 history to protect.

## What this does not do (stated so the pitch cannot overclaim)

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

## Phases

| Phase | Deliverable | Gate |
|---|---|---|
| **B0 spike (1 day)** | The wire table built from T3's recorded fixtures (`apps/server/src/orchestration-v2/testkit/fixtures/*/**.ndjson`, MIT) plus a 100-line stdio tap run live against `claude-agent-acp` and Codex to catch drift since the recordings — which agents send `request_permission`, what the joined `tool_call` carries, what rides `_meta` | `docs/field-tests/acp-wire-table.md` + the transcripts vendored under `tests/fixtures/acp/<agent>/` with notice |
| **B1 proxy** | `skep acp govern` with D3–D6; mapper shared with `policy_hook.py`; ledger rows typed | Golden: a fixture transcript per agent replayed through the proxy produces byte-equal ledger rows; `test_surface_parity` for the new verb |
| **B2 package** | D8: PyPI release, pipx path, `install-hint`, v2 default-on for fresh homes | Verbatim smoke test on a second machine with no checkout: `pipx install skep && skep acp doctor -- claude-code-acp` prints live rings |
| **B3 deliver** | A 30-second recording: T3 in Supervised, one policy that T3's modes cannot express (allow `pnpm test`, `git status`; ask on `rm`; deny `git push` from the floor), run on Codex, with the `why` output beside it; posted to #6976 and #7340 with the install line | Posted. Then the first 48h is a field test and seeds v117 |

## Open questions — three closed by reading T3's tree (2026-11-06), one left for B0

1. **Closed — install is a setting.** Settings → Providers → Add provider → Show more → **Local ACP command**: an executable plus one literal argument per row, launched directly with no shell expansion. `skep acp govern -- claude-agent-acp` goes there. Registry agents also carry an **Executable override**. No `PATH` shim. Under a managed policy with `maxRuntimeMode`, `binaryPath`/`launchArgs` are admin-pinned — the enterprise install is the admin pinning them to the governor.
2. **Closed — no hard ring on T3.** See D4. Advisory-only, and still decisive in Auto/Full access.
3. **Closed — T3 prefers the ACP v2 preview and negotiates v1.** Its generated schemas (`packages/effect-acp/src/_generated/schema.gen.ts`, `schema-v1.gen.ts`) are the shape reference; `claude-agent-acp` speaks v2 preview by default (`CLAUDE_AGENT_ACP_EXPERIMENTAL_V2=0` forces v1). The proxy must speak both; B0 confirms what each agent emits today.
4. **Open — does T3's Claude provider (Agent SDK) honour `PreToolUse` from `~/.claude/settings.json`?** Decides whether ring (c) exists for T3's bespoke Claude threads. Irrelevant for the Local ACP command path, which is the launch path.

## What T3's own managed policy does and does not do (context for the pitch)

T3 ships `docs/user/managed-policy.md`: a machine-wide JSON that locks
settings, caps `maxRuntimeMode`, allowlists drivers and pins binaries. It
governs *T3's settings*, not the agent's actions, and its own text says
it is not a sandbox and that restricting the agent CLIs is the job of
their own managed configuration. That sentence is the gap this ADR fills,
in their words. Expect the objection that a wrapper is exactly what their
policy exists to block; the answer is that the governor is the wrapper
that records every verdict, including the human's.

## Consequences

- Positive: skep becomes usable as a governor by people who never run `skep serve`, through the one hop all ACP agents share; the T3 threads get a working answer rather than a feature request; the ledger gains cross-agent observations the Queen can learn from (v116).
- Negative: a third front-end over the policy vocabulary to keep in parity (hook, proxy, worker); per-agent `rawInput` tables will rot as agents change — they are data rows with a doctor, not code, for that reason; the advisory ring's coverage depends on a mode skep does not control, which has to be said in every surface that shows a verdict count.
- Risk: T3's team adds a fifth mode or a policy file of their own. The proxy still works — it governs the agent, not T3 — and the REST face is what they would call. Speed matters more than scope: B0–B3 is three weeks, not a quarter.

## Amendments after reading the schemas and the tree (2026-11-06, Architect)

Written before B0 from the ACP JSON schemas (`schema/v1` and `schema/v2` of
`agentclientprotocol/agent-client-protocol`, read 2026-11-06) and from the
skep tree at `main@41dc938`. Each amends a decision above; the decision text
is left as written so the delta stays visible.

- **A1 (D4) — the hard ring is ACP v1 only.** v2's client methods are
  `session/request_permission`, `session/update`, `elicitation/create` and
  `elicitation/complete` — no `fs/*`, no `terminal/*` (`schema/v2/meta.json`).
  `claude-agent-acp` speaks v2 by default. The hard ring exists only when v1
  is negotiated and the client advertised `clientCapabilities.fs.writeTextFile`
  or `clientCapabilities.terminal`; the rings line at startup says which.
- **A2 (D5) — v2 carries `subject`.** v2 `RequestPermissionRequest` is
  `{sessionId, title, description?, subject?, options}`, with `subject` one of
  `{type: "tool_call", toolCall: ToolCallUpdate}`, `{type: "command", …}` or an
  extension string; v2 `session/update` has no `tool_call` kind — the first
  `tool_call_update` carries the full fields. The mapper reads `subject`
  first (`command` → `shell.run` directly; `tool_call` → inline fields merged
  over the cache) and falls back to the v1 `toolCall` join. v1 is as written.
- **A3 (D7) — the ledger read is new, not a filter.** Nothing reads
  `decisions.jsonl` today; only `_append_policy_ledger` writes it. B1-4
  builds one function, `policy_decisions`, with three faces: `skep policy
  decisions`, `GET /api/policy/decisions`, and the chat read tool
  `policy_decisions`.
- **A4 (D7) — no `acp_sessions`, no pidfiles.** Chat policy reads are
  separate tools and each costs tool-index bytes against three ratchet pins;
  the ledger read filtered by `actor=acp:*` answers "what did the governed
  agents do today". A live-session listing is YAGNI until someone asks.
- **A5 (D7) — `test_surface_parity` needs nothing.** It checks chat →
  operator only; `skep acp govern` is a process-shaped CLI verb and ADR 0050
  exempts reads. Recorded so no exemption map grows for it.
- **A6 (D8) — the fresh-home seed lives at store creation.** The
  `pipx install skep && skep acp govern` path never runs `skep setup`;
  `RunStore.__init__` is the one point every entry shares. `fresh = not
  db_path.exists()` captured before connect → write
  `policy_engine_v2_scopes = ["filesystem", "shell"]`. No flip-ledger row: a
  seed is not an operator flip, and ADR 0053 D7's 30-day clock is unaffected.
- **A7 (B0) — the wire table is `tests/fixtures/acp/README.md`.** There is no
  `docs/field-tests/`; field-test docs are local plans. The table lives
  beside the fixtures it documents, and fixtures use one envelope format
  (`{"dir": "c2a"|"a2c", "raw": "<frame verbatim>"}` per line). Vendored and
  live transcripts are scrubbed of host paths before commit (the
  release-hygiene scan runs in CI).
- **A8 (D5, B0) — attribution and the containment port.** ADR 0004 names a
  root `NOTICE`; T3's MIT text is vendored as
  `tests/fixtures/acp/LICENSE-t3code.txt` and `NOTICE` lists it.
  `_workspace_value` already has realpath semantics (`Path.resolve` resolves
  the existing ancestors and appends the missing suffix); it is lifted to
  `policy_paths.workspace_value` and pinned with a symlink-escape test. T3's
  `clientPolicy.ts` algorithm is ported only if that test fails.
- **A9 (D4, D6) — `source` discriminates, not `verdict`.** `verdict` means a
  decision everywhere in the ledger; `source` is already the enum
  `floor | run | operator`. Observed rows are `source: "observed"` with no
  verdict; forwarded rows are `source: "forwarded"` with `outcome` = the
  option kind the human chose, or `cancelled`. The read face counts them
  apart from decisions (I8).
- **A10 (D6) — the hard ring has no human hop.** On `terminal/create` and
  `fs/write_text_file` only `allow` passes through; `require_approval`,
  default-deny, an unreadable store and any governor failure answer a
  JSON-RPC error like an explicit deny, because forwarding a method that
  carries no prompt would run it unapproved (D6's "forward" cells for that
  column are amended to "error").
- **A11 (D3/D4, B3 field test in T3) — a session has more than one opener.**
  T3 respawns the agent after a thread goes idle and continues the same
  session with `session/load` (v1) — v2 clients use `session/resume`, and
  either may `session/fork`. All four carry `cwd`, so all four bind it
  (B3-F2; the first build bound `session/new` only and every resumed turn
  decided against repo ""). A continuing opener replays the session's
  history as `session/update` notifications — v1 `session/load` by the
  protocol, and v2 `session/resume` too under T3 with claude-agent-acp
  0.101.0 (B3-F11: the B0 taps saw no replay on a bare resume; T3's resume
  carries its MCP server and the thread's history came back as v2
  `tool_call_update`/`terminal_update` frames). Those are fed to the cache
  for later joins but never written as observed rows (B3-F3; they were
  re-observed once per turn). The replay window is not the opener's
  request/response pair — under T3 the replay was recorded even so — but
  the stretch from any continuing opener to the client's next
  `session/prompt`, since a continued session can run nothing before it
  (B3-F9). Confirmed on T3's real wire 2026-11-09
  (`tests/fixtures/acp/claude-agent-acp/t3-v2-resume-ask.ndjson`): seven
  replayed calls, zero observed rows, then one observed run, one forwarded
  ask answered by the human and one floor deny.

**Operator decisions (2026-11-06):** D3 accepted, plus a `--repo` override on
the command line; D6 accepted; engine v2 on for fresh homes (`shell`,
`filesystem`) accepted; version 1.2.0; observed and forwarded rows share
`decisions.jsonl`. The executor plan is `plans/acp-governor/README.md`
(local).
