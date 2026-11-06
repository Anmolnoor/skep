# ACP wire table (B0 of ADR 0054)

What crosses the ACP stdio wire, per agent, as recorded. B1's mapper and
its golden tests are written against these files. Regenerate the raw
field inventory with:

```sh
python -I scripts/acp_fixture_scan.py scan tests/fixtures/acp
```

Citations below are `file:line` (1-based line in the `.ndjson`), paths
relative to this directory.

## (a) Envelope format, scrub rules, and which fixture feeds which test

One frame per line: `{"dir": "c2a" | "a2c", "raw": "<frame>"}`.
`c2a` = client -> agent, `a2c` = agent -> client. The scratch tap log
also carries `"t"` (epoch seconds); `convert` drops it.

- **Live taps** (`claude-agent-acp/`, `codex-acp/`): `raw` is the JSON-RPC
  line byte-for-byte as it crossed the pipe, then scrubbed. Real
  `jsonrpc`/`id`, real request/response pairing. **These are the only
  fixtures for the B1-1 transparency replay and the B1-3 golden-ledger
  replay**, because both need real request/response ids.
- **T3 recordings** (`kiro/`, `grok/`, `fx/`, `acp-registry/`): `raw` is
  T3's recorded `frame`, kept exactly as recorded (only re-serialized
  compactly and scrubbed). **Not verbatim JSON-RPC:** T3 normalizes every
  frame to `{"kind": "request" | "response" | "notification", "method",
  "params" | "result"}` with no `jsonrpc` and no `id`; responses carry the
  `method` they answer; T3's own prompt text, MCP servers and some
  initialize params are `"<any>"`. `expect_outbound` -> `c2a`,
  `emit_inbound` -> `a2c`. No ids were invented. **These feed the B1-2
  mapper shape tests** (`expected_probes.json`): method + params are all
  the mapper reads.

Scrub (applied by `acp_fixture_scan.py convert`; nothing else removed):

| What | Replaced by |
|---|---|
| the session `cwd` (from `session/new`, plus its realpath and `/private`-less spelling), and T3's `<workspace>` placeholder | `$REPO` |
| a home directory (the `Users` or `home` root plus the login name) | `<home>` |
| the local login name | `<user>` |
| email addresses | `<email>` |
| every `availableCommands` list — `session/update` `available_commands_update` (v1 and v2) and the v2 `session/new` result | `[]` — **availableCommands redacted: the operator's local skill list is personal** |
| `_auth/status_update` frames and `session/update` `usage_update` frames | **dropped** — account plan labels and cost figures are the operator's; the mapper reads neither |
| `content.text` of `agent_message_chunk` / `agent_thought_chunk` updates (every other key kept; `user_message_chunk` untouched — it is our own prompt) | `"…"` — **agent prose chunks are blanked: streamed text re-spells scrubbed paths across chunks, and the mapper never reads prose** |

Session ids and tool call ids are kept. Because frames are dropped, live
line numbers are the vendored file's, not the raw tap's. The T3 files
were converted before the last two rules and were not re-converted (the
brief keeps them as recorded; `fx/tool_call_denied_write.ndjson:17` is a
T3 `usage_update`). Blanking prose replaces text in place, so it moves no
line numbers. Agent prose quoted below comes from the unscrubbed scratch
tap log, not the vendored file.

### Files

| File | Package | Client `protocolVersion` | Overrides | Frames |
|---|---|---|---|---|
| `claude-agent-acp/live-v1.ndjson` | `@agentclientprotocol/claude-agent-acp@0.101.0` | 1 | none (default mode `auto`) | 33 |
| `claude-agent-acp/live-v1-ask.ndjson` | same | 1 | `--mode default` (`session/set_mode`) | 38 |
| `claude-agent-acp/live-v2.ndjson` | same | 2 | none (`auto`) | 33 |
| `claude-agent-acp/live-v2-ask.ndjson` | same | 2 | `--mode default` (`session/set_config_option`) | 45 |
| `codex-acp/live-v1.ndjson` | `@agentclientprotocol/codex-acp@2.5.0` | 1 | `--model 'gpt-6-luna[low]'` (`session/set_model`); default mode `agent` | 34 |
| `codex-acp/live-v1-ask.ndjson` | same | 1 | `--model 'gpt-6-luna[low]' --mode read-only` (`set_model`, `set_mode`) | 55 |
| `codex-acp/live-v2.ndjson` | same | 2 | `--model gpt-6-luna` (`session/set_config_option`); default mode `agent` | 57 |
| `codex-acp/live-v2-ask.ndjson` | same | 2 | `--model gpt-6-luna --mode read-only` (both `set_config_option`) | 181 |
| `kiro/kiro_permission_modes-supervised.ndjson` | T3 `kiro_permission_modes/kiro_supervised_transcript.ndjson` (Kiro 2.32.0) | — | — | 43 |
| `kiro/kiro_permission_modes-full_access.ndjson` | T3 `kiro_permission_modes/kiro_full_access_transcript.ndjson` | — | — | 43 |
| `kiro/turn_interrupt_mid_tool.ndjson` | T3 `turn_interrupt_mid_tool/kiro_transcript.ndjson` | — | — | 40 |
| `grok/grok_auto_blocked_command.ndjson` | T3 `grok_auto_blocked_command/grok_transcript.ndjson` | — | — | 134 |
| `fx/tool_call_denied_write.ndjson` | T3 `tool_call_denied_write/fx_transcript.ndjson` (fx 0.0.13) | — | — | 20 |
| `acp-registry/tool_call_read_only_on_request.ndjson` | T3 `tool_call_read_only_on_request/registry_transcript.ndjson` (`grok-replay` shape probe) | — | — | 11 |
| `acp-registry/acp_elicitation.ndjson` | T3 `acp_elicitation/registry_transcript.ndjson` (hand-written) | — | — | 9 |

Live prompt (all runs): ``run `git status`, then create a file named
probe.txt containing the word hello``; `--cwd` a fresh empty git repo per
run; the client advertises no `fs`/`terminal` capability and answers every
`session/request_permission` with the first `allow_once` option.

T3 fixtures are MIT, `LICENSE-t3code.txt`; see the root `NOTICE`. Not
vendored: T3's `muse_*` (`muse.msp-jsonl`), `claude_*`
(`claude-agent-sdk.query`), `codex` (`codex.app-server`) and `opencode*`
recordings — none is ACP. T3 has no ACP recording of Claude or Codex.

## (b) Per agent

### Handshake and modes

| Agent | Client sent → agent answered | Name in `initialize` result | `authMethods` | Mode ids advertised (`session/new`) — default **bold** | Evidence |
|---|---|---|---|---|---|
| claude-agent-acp 0.101.0 | 1 → 1 | `agentInfo.name = "@agentclientprotocol/claude-agent-acp"` (title `Claude Agent`) | `[]` | `modes.availableModes[].id`: `default` (Manual, "Always ask before making changes"), `acceptEdits`, `plan`, **`auto`** ("Claude handles permission decisions"), `bypassPermissions` | `claude-agent-acp/live-v1-ask.ndjson:2,4` |
| claude-agent-acp 0.101.0 | 2 → **2** (real v2: `info`, `capabilities`) | `info.name = "@agentclientprotocol/claude-agent-acp"` | `[]` | same ids as `configOptions[configId=mode].options[].value`; no `modes` object | `claude-agent-acp/live-v2-ask.ndjson:2,4` |
| codex-acp 2.5.0 | 1 → 1 | `agentInfo.name = "@agentclientprotocol/codex-acp"` (title `Codex`) | `api-key`, `chat-gpt` | `modes.availableModes[].id`: `read-only` ("Requires approval to edit files and access the internet"), `workspace-write`, **`agent`** ("Auto review: only ask for actions detected as potentially unsafe"), `agent-full-access` | `codex-acp/live-v1-ask.ndjson:2,4` |
| codex-acp 2.5.0 | 2 → **2** (`info`, `capabilities`, `authMethods[].methodId`) | `info.name = "@agentclientprotocol/codex-acp"` | `api-key`, `chat-gpt` | same ids in `configOptions[configId=mode]` | `codex-acp/live-v2-ask.ndjson:2,4` |
| kiro 2.32.0 (via T3) | 2 → 2 | none | `aws-builder-id` | `vibe` | `kiro/kiro_permission_modes-supervised.ndjson:1-2,5` |
| grok (via T3) | 2 → 1 | none | `cached_token` | — | `grok/grok_auto_blocked_command.ndjson:1-2` |
| fx 0.0.13 (via T3) | 2 → 1 | `agentInfo.name = "fx"` | `[]` | `ask` | `fx/tool_call_denied_write.ndjson:1-2,4` |
| acp-registry probe (via T3) | 2 → 1 | `grok-replay` / `generic-acp-replay` | `replay` | — | `acp-registry/tool_call_read_only_on_request.ndjson:1-2` |

Setting mode/model: v1 uses `session/set_mode {sessionId, modeId}` /
`session/set_model {sessionId, modelId}` (`claude-agent-acp/live-v1-ask.ndjson:5`,
`codex-acp/live-v1-ask.ndjson:5,7`; claude v1 advertises no `models`
object, only a `configOptions` model entry). v2 uses
`session/set_config_option {sessionId, configId, type: "id", value}`
(`claude-agent-acp/live-v2-ask.ndjson:5`, `codex-acp/live-v2-ask.ndjson:5,7`)
— without `type` codex rejects it `-32602` (`{"type":{"_errors":["Invalid
input: expected \"id\"", …]}}`, run log in (c)).

T3 advertises `clientCapabilities.fs = {readTextFile:false, writeTextFile:false}`
and `terminal:false` (`kiro/kiro_permission_modes-supervised.ndjson:1`,
`fx/tool_call_denied_write.ndjson:1`). The tap advertises nothing.

### `session/update` kinds seen

| Agent | Kinds |
|---|---|
| claude-agent-acp v1 | `available_commands_update`, `config_option_update`, `session_info_update`, `agent_message_chunk`, `tool_call`, `tool_call_update` (+ `usage_update`, dropped) |
| claude-agent-acp v2 | `user_message`, `state_update` (`running` / `requires_action` / `idle` + `stopReason`), `available_commands_update`, `config_option_update`, `session_info_update`, `agent_message_chunk`, **`terminal_update`**, `tool_call_update` — **no `tool_call`** |
| codex-acp v1 | `available_commands_update`, `session_info_update` (`_meta.codex.threadStatus`), `agent_message_chunk`, `tool_call`, `tool_call_update` |
| codex-acp v2 | `user_message_chunk`, `state_update`, `session_info_update`, `agent_thought_chunk`, `agent_message_chunk`, **`terminal_update`**, `terminal_output_chunk`, `tool_call_update` — **no `tool_call`** |
| kiro | `tool_call`, `tool_call_update`, `session_info_update` (heavy `_meta.kiro`), `config_option_update`, `available_commands_update`, `agent_message_chunk` |
| grok | `tool_call`, `tool_call_update`, `agent_thought_chunk`, `agent_message_chunk`, `config_option_update`, `available_commands_update`, `session_info_update`; plus extension notifications `_x.ai/session_notification` |
| fx | `tool_call`, `tool_call_update`, `agent_message_chunk`, `session_info_update`, `usage_update` |

No file contains a `terminal/*` or `fs/*` **method** (the only `terminal/`
text is inside URIs in `grok/grok_auto_blocked_command.ndjson:80,104`).
`terminal_update` / `terminal_output_chunk` are v2 notifications that
stream the agent's own terminal to the client — observation, not a hop
the proxy can refuse.

### Shell command: where the command lives

| Agent | first frame for the call | later frames | asked? |
|---|---|---|---|
| claude v1 | `tool_call`, `kind:"execute"`, `name:"Bash"`, **`rawInput: {}`**, `title:"Terminal"`, `_meta.claudeCode.toolName` (`claude-agent-acp/live-v1-ask.ndjson:17`) | `tool_call_update` sets `rawInput.command` + `title` = command (`:18`), adds `rawInput.description` (`:19`) | **no** — `git status` ran unasked even in `default` (Manual) (`:17-22`) |
| claude v2 | `terminal_update {terminalId}` (`claude-agent-acp/live-v2-ask.ndjson:19`), then `tool_call_update` `kind:"execute"`, `rawInput: {}` (`:20`) | `terminal_update.command` (`:21`); `tool_call_update.rawInput.command` (`:22-23`); output base64 in `terminal_update.output.data`, `exitStatus` (`:25-26`) | **no** (`:19-27`) |
| codex v1 | `tool_call`, `kind:"execute"`, `name:"exec_command"`, `title` = command, **`rawInput.{command,cwd}` populated** (`codex-acp/live-v1.ndjson:10,12`) | status + `_meta.terminal_output_delta` / `terminal_exit` (`:11,13`) | no (mode `agent`) |
| codex v2 | `terminal_update {terminalId, command, cwd}` (`codex-acp/live-v2.ndjson:28,34`), then `tool_call_update` `kind:"execute"`, `rawInput.{command,cwd}` (`:29,35`) | `terminal_update` output/exit (`:30,36`), status (`:31,37`) | no (mode `agent`) |
| codex v2, `read-only` | same (`codex-acp/live-v2-ask.ndjson:83-84`) | — | **yes**, for the redirect write (`:86`), see below; `git status` unasked (`:34-44`) |
| kiro | `tool_call`, `kind:"execute"`, `rawInput.command` (`kiro/turn_interrupt_mid_tool.ndjson:22`) | same `rawInput` (`:29`) | yes (`:26`) |
| grok | `tool_call` **without `kind`**, `rawInput.command`, kind in `_meta["x.ai/tool"].kind` (`grok/grok_auto_blocked_command.ndjson:74,95`) | first update adds `kind:"execute"` (`:76,97`) | `echo` no, `rm -rf` yes (`:100`) |

In both claude versions and in codex v2, **`terminal_update.terminalId ==
toolCallId`** and `terminal_update` carries `command` (codex also `cwd`):
a secondary command source (`claude-agent-acp/live-v2-ask.ndjson:19-22`,
`codex-acp/live-v2.ndjson:28-29`).

### File write: where the path lives

| Agent | how it wrote | announce | asked? |
|---|---|---|---|
| claude v1/v2, `default` | **`Write` tool**, `kind:"edit"` | v1 `tool_call` `rawInput: {}`, `title:"Preparing file…"`, `locations: []` (`claude-agent-acp/live-v1-ask.ndjson:20`); update fills `rawInput.file_path` (absolute), `locations[].path`, `title:"Write probe.txt"` (`:23`), then `rawInput.content` (`:24`). v2 identical but first frame is a `tool_call_update` (`live-v2-ask.ndjson:24,28-29`) | **yes** (`live-v1-ask.ndjson:25`, `live-v2-ask.ndjson:32`) |
| claude v1/v2, `auto` | shell redirect inside one Bash call (`git status && printf 'hello\n' > probe.txt && cat probe.txt`) | `kind:"execute"` (`claude-agent-acp/live-v1.ndjson:15`, `live-v2.ndjson:19`); path only post hoc in `_meta.claudeCode.toolResponse.bashEditDiff.files[].filePath` (`live-v1.ndjson:17`) | no |
| codex v1/v2, `agent` | shell `printf 'hello' > probe.txt`, `kind:"execute"` | `codex-acp/live-v1.ndjson:12`, `live-v2.ndjson:35` | no |
| codex v1, `read-only` | **did not write**; no tool call for it, no request. Final message (raw tap; blanked in the fixture): "I couldn't create `probe.txt`: the workspace is read-only, so writing the file was denied." | only `git status` (`codex-acp/live-v1-ask.ndjson:12-20`) | no |
| codex v2, `read-only` | shell `printf 'hello' > probe.txt`, `kind:"execute"` (`codex-acp/live-v2-ask.ndjson:84`), then a `kind:"read"` verify with `locations[].path` (`:94`) | — | **yes** (`:86`) |
| kiro | `kind:"edit"`, `rawInput.{path,text}`, `locations[].path` (absolute) (`kiro/kiro_permission_modes-supervised.ndjson:20`) | diff in later update (`:28`) | yes (`:25`) + end-of-turn review (`:29`) |
| fx | `kind:"edit"`, `rawInput.{path,content}` (**relative**), no `locations` (`fx/tool_call_denied_write.ndjson:11`) | — | yes (`:12`) |

### `session/request_permission` shape

| Agent | Envelope | Operation in the request? | Evidence |
|---|---|---|---|
| claude v1 | `{sessionId, toolCall, options}` | **full**: `toolCall.{toolCallId, name:"Write", status, rawInput.{file_path,content}, title, kind:"edit", content[{type:"diff",path,oldText,newText}], locations[].path}` | `claude-agent-acp/live-v1-ask.ndjson:25` |
| claude v2 | `{sessionId, title, subject, options}` | **`subject: {type:"tool_call", toolCall:{toolCallId, name, rawInput.{file_path,content}, title, kind:"edit", locations[].path, content[{type:"diff", changes[{operation:"add",path,fileType}], patch{format:"git_patch",text}}]}}`** | `claude-agent-acp/live-v2-ask.ndjson:32` |
| codex v2 | `{sessionId, title:"Run command?", description, subject, options}` | `subject: {type:"tool_call", toolCall:{toolCallId, title:"Run command", rawInput.{command, cwd}, kind:"execute", status}}` — **`rawInput.command` is shell-quoted** (`"\"printf 'hello' > probe.txt\""`), unlike the `tool_call_update` (`printf 'hello' > probe.txt`, `:84`); `description` is model prose | `codex-acp/live-v2-ask.ndjson:86` |
| codex v1 | none recorded (read-only declined without asking; `agent` never asked) | — | `codex-acp/live-v1-ask.ndjson`, `live-v1.ndjson` |
| kiro | v1 `toolCall` | **only** `{toolCallId, status, title}`; detail in `params._meta.kiro.{toolId, command, consent.{capability,resource,workspaceRoot}}` | `kiro/kiro_permission_modes-supervised.ndjson:25`, `kiro/turn_interrupt_mid_tool.ndjson:26` |
| grok | v1 `toolCall` | full: `kind`, `rawInput.command`, `_meta["x.ai/tool"]` | `grok/grok_auto_blocked_command.ndjson:100` |
| fx | v1 `toolCall` | full: `kind`, `rawInput.path` | `fx/tool_call_denied_write.ndjson:12` |
| acp-registry probe | v1 `toolCall` | `kind`, no `rawInput` | `acp-registry/tool_call_read_only_on_request.ndjson:7` |

In v2 the agent also sends `state_update {state:"requires_action"}` just
before the request and `running` after the answer
(`claude-agent-acp/live-v2-ask.ndjson:31,34`, `codex-acp/live-v2-ask.ndjson:85,88`).
The request id was `0` in every live run (`:25`/`:32`/`:86`).

### Options offered (verbatim, in order)

| Agent | `(optionId, name, kind)` | Evidence |
|---|---|---|
| claude v1 and v2 (Write) | `allow-once` "Yes" `allow_once`; `allow-with-updates` "Yes, allow all edits during this session" `allow_always`; `reject` "No" `reject_once` | `claude-agent-acp/live-v1-ask.ndjson:25`, `live-v2-ask.ndjson:32` |
| codex v2 (command) | `allow_once` "Yes, proceed" `allow_once`; `accept_execpolicy_amendment` "Yes, and don't ask again for commands that start with `printf 'hello' > probe.txt`" `allow_always`; `cancel` "No, and tell Codex what to do differently" `reject_once` | `codex-acp/live-v2-ask.ndjson:86` |
| kiro (per tool) | `accept` Allow `allow_once`; `always-accept` Always allow `allow_always`; `reject` Deny `reject_once`; `always-reject` Always deny `reject_always` | `kiro/kiro_permission_modes-supervised.ndjson:25` |
| kiro (turn review) | `accept` Accept changes `allow_once`; `reject` Reject changes `reject_once` | `…-supervised.ndjson:29` |
| grok | `always-allow` `allow_always` **(first)**; `allow-once` `allow_once`; `reject-once` `reject_once`; `reject-always` `reject_always` | `grok/grok_auto_blocked_command.ndjson:100` |
| fx | `allow_once`; `allow_always`; `reject_once` | `fx/tool_call_denied_write.ndjson:12` |
| acp-registry probe | `allow-once`; `allow-always`; `reject-once` | `acp-registry/tool_call_read_only_on_request.ndjson:7` |

No live agent offered `reject_always`. `optionId`s differ per agent
(`allow-once` / `allow_once` / `accept`): B1 selects by `kind`, never by id
or position.

## (c) Drift and run log

### Live runs, 2026-11-06

| # | Package | Client | Overrides | Result |
|---|---|---|---|---|
| 1 | `@zed-industries/claude-agent-acp@0.23.1` | v1, v2 | none | answered 1 to both; prompt `-32603 … "Claude Code 2.1.83 does not support this model; version 2.1.251 or newer is required." … "claude_code_version_too_old"` (superseded, not vendored) |
| 2 | `@zed-industries/codex-acp@0.16.0` | v1, v2 | none | answered 1 to both; prompt `-32603` `"The 'gpt-5.6-sol' model is not supported when using Codex with a ChatGPT account."` (superseded) |
| 3 | claude-agent-acp@0.101.0 | v1 | none | works → `live-v1.ndjson` |
| 4 | same | v2 | none | attempt 1: tap stopped at the v2 prompt response (tap bug, fixed); attempt 2 works → `live-v2.ndjson` |
| 5 | codex-acp@2.5.0 | v1 | none, then `--model 'gpt-6-luna[low]'` | attempt 1: turn ended with the agent message "The 'gpt-5.6-sol' model is not supported when using Codex with a ChatGPT account."; attempt 2 works → `live-v1.ndjson` |
| 6 | same | v2 | `--model gpt-6-luna` | attempt 1: tap read `configOptions[].id` (v2 uses `configId`; tap bug). Attempt 2: `set_config_option` without `type` → `-32602 Invalid params` (tap bug). Round three, with both fixed: works → `live-v2.ndjson` |
| 7 | claude-agent-acp@0.101.0 | v1 | `--mode default` | works, asked for Write → `live-v1-ask.ndjson` |
| 8 | same | v2 | `--mode default` | works, asked for Write → `live-v2-ask.ndjson` |
| 9 | codex-acp@2.5.0 | v1 | `--model 'gpt-6-luna[low]' --mode read-only` | works (`end_turn`); **never asked and did not write** → `live-v1-ask.ndjson` |
| 10 | same | v2 | `--model gpt-6-luna --mode read-only` | works, asked for the redirect command → `live-v2-ask.ndjson` |

### Drift found

1. **claude-agent-acp 0.101.0 and codex-acp 2.5.0 speak v2 for real**
   (0.23.1 / 0.16.0 did not). Each answers the version the client offers
   (`claude-agent-acp/live-v1.ndjson:2` vs `live-v2.ndjson:2`). The ADR's
   "v2 by default" means "v2 when the client offers it".
2. **v2 `session/prompt` returns immediately** with `{messageId}`
   (`claude-agent-acp/live-v2.ndjson:7`); the turn ends on
   `state_update {state:"idle", stopReason}` (`:33`).
3. **Amendment A2 confirmed**: in v2 neither agent sends `tool_call`; the
   first frame for a call is a `terminal_update` and/or a
   `tool_call_update` carrying `kind` (`claude-agent-acp/live-v2-ask.ndjson:19-20,24`,
   `codex-acp/live-v2.ndjson:28-29`), and the request carries a `subject`
   (`claude-agent-acp/live-v2-ask.ndjson:32`, `codex-acp/live-v2-ask.ndjson:86`).
4. **claude's first announce has an empty `rawInput`** for both Bash and
   Write (`claude-agent-acp/live-v1-ask.ndjson:17,20`); codex's carries the
   command (`codex-acp/live-v1.ndjson:10`).
5. **The default modes never ask.** claude `auto` and codex `agent` ran
   `git status` and wrote the file with no request (`claude-agent-acp/live-v1.ndjson`,
   `live-v2.ndjson`, `codex-acp/live-v1.ndjson`, `live-v2.ndjson`).
6. **Same mode, different behaviour by protocol version**: codex
   `read-only` under v1 declined the write without asking
   (`codex-acp/live-v1-ask.ndjson`), under v2 it asked for an escalation
   (`codex-acp/live-v2-ask.ndjson:86`).
7. **codex's request rewrites the command**: `rawInput.command` in the v2
   request is shell-quoted (`codex-acp/live-v2-ask.ndjson:86`), the
   preceding `tool_call_update` is not (`:84`).
8. Kiro negotiates v2 but sends v1 shapes (`kiro/kiro_permission_modes-supervised.ndjson:20,25`)
   and asks even under T3 Full access, which T3 auto-answers
   (`kiro/kiro_permission_modes-full_access.ndjson:29-30`). Grok's own mode
   runs `echo` silently and asks only for `rm -rf …`
   (`grok/grok_auto_blocked_command.ndjson:74-80` vs `:95-101`).

## (d) The ADR's open questions, with this evidence

1. **Install is a setting** — nothing on the wire contradicts it; both
   live adapters ran from plain argv (`npx -y <pkg>@<ver>`), no shell.
2. **No hard ring on T3** — confirmed: T3 advertises `fs.*: false`,
   `terminal: false` in every recording (`fx/tool_call_denied_write.ndjson:1`,
   `kiro/kiro_permission_modes-supervised.ndjson:1`, `grok/grok_auto_blocked_command.ndjson:1`);
   no `fs/*`/`terminal/*` method appears anywhere; v2 terminals stream to
   the client as `terminal_update` notifications only.
3. **T3 prefers v2, negotiates v1** — T3's side confirmed (line 1 of every
   T3 file sends 2). Agent side: claude-agent-acp 0.101.0, codex-acp 2.5.0
   and kiro answer 2; grok and fx answer 1. The proxy must speak both, and
   v2 (`subject`, `state_update`, no `tool_call`) is what the two adapters
   T3 users most likely run will send.
4. **Does T3's Claude provider honour `PreToolUse`?** — not answerable from
   ACP traffic (T3's Claude recordings are `claude-agent-sdk.query`). Open.

The T3 containment reference (`acpCanonicalPathForContainment`) was not
located: the sparse-clone `git grep` is refused in this environment.
Amendment A8 ports it only if the symlink-escape test fails.

## (e) What this means for the mapper (B1-2 preset rows)

- **Default modes: skep only observes.** claude `auto` and codex `agent`
  asked nothing for a `git status` and a workspace write (drift 5). On a
  T3 mode that maps onto those, the advisory ring has nothing to decide;
  the governor writes `observed` rows from `tool_call*` and that is all.
  Say so wherever a verdict count is shown.
- **claude-agent-acp** (v1 and v2) — the request is self-contained: v1
  `toolCall.{kind, rawInput, locations}`, v2 `subject.toolCall.{…same}`.
  Write: `locations[].path` (absolute), else `rawInput.file_path`. Shell:
  `rawInput.command`. The cache matters only for observations: the first
  announce has `rawInput: {}`, so merge `tool_call_update`s by
  `toolCallId`; in v2 `terminal_update.command` (keyed by `terminalId ==
  toolCallId`) is a secondary command source. Read-only commands
  (`git status`) ran unasked even in Manual.
- **codex-acp** — v2 request: `subject.toolCall.kind:"execute"` +
  `rawInput.{command, cwd}`; **`command` is shell-quoted in the request**
  — prefer the cached `tool_call_update.rawInput.command` (or
  `terminal_update.command`) for the same `toolCallId`, and treat the
  request's string as a fallback that must be unquoted. `rawInput.cwd` is
  the per-call cwd D3 decides against. v1 `read-only` declines instead of
  asking, so v1 sessions yield observations only.
- **A shell redirect is a write the `filesystem` scope never sees.**
  Claude `auto` and both codex runs wrote `probe.txt` with
  `printf … > probe.txt` under `kind:"execute"`; codex's escalation request
  for it is `kind:"execute"` too. Only a `shell.run` rule on the command
  string can govern it.
- **kiro** — the request carries nothing: join on `toolCallId` to the
  cached `tool_call`. Shell: `rawInput.command` (fallback
  `_meta.kiro.command`). Write: `locations[].path`, then `rawInput.path`,
  then `_meta.kiro.consent.resource` against `consent.workspaceRoot`. The
  end-of-turn `turn_approval_*` request (`kiro/kiro_permission_modes-supervised.ndjson:29`)
  has a `toolCallId` that was **never announced** → per D5 the governor
  does not decide it and **forwards it to the human**.
- **grok** — self-contained request: `toolCall.kind` +
  `toolCall.rawInput.command`; the cached `tool_call` may lack `kind`
  (`grok/grok_auto_blocked_command.ndjson:74`). **Its options list
  `allow_always` first** (`:100`): select by `kind == "allow_once"`, never
  `options[0]` — the first option would be the standing grant D6 forbids.
- **fx** — self-contained request: `toolCall.kind:"edit"` +
  `toolCall.rawInput.path`, **relative** to the session `cwd`; join it to
  `cwd` before realpath. No `reject_always` offered.
