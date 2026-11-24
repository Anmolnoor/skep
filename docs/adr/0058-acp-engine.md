# ADR 0058 — the ACP engine: skep dispatches Claude Code and Codex through its own governor

**Status:** accepted (Phase 04, Work Item 4.1, 2026-11-23). Promoted from the
2026-11-06 draft in the design archive and amended: the governor runs
**outside** the sandbox (the draft put it inside, with the store mounted
read-only — rejected, see D2/D6). C0–C2 are built; C3's patch-card ask
counts and the `skep approval answer` CLI face are not. Amended by the Phase
04 review round 1 (4C, 2026-11-23): engine identity by module, strict reads
required, run-bound stacks, port-aware wall, card expiry at the claim, the
worker's claim kept beside the supervisor's verdict. Amended by round 2 (4E,
2026-11-23): the checked read wall carried to launch, race-free cards, one
terminal verdict.
**Depends on:** ADR 0054 (the governor: `acp/proxy.py`, `acp/mapper.py`,
`acp/govern.py`), ADR 0047 (CLI-agent engines), ADR 0059 (`decide()`),
ADR 0061 (unlocked mode), ADR 0068 (strict reads).
**Invariants:** I1, I2, I4, I5, I8, I9, I12, I13.

## Context

skep ran Claude Code and Codex two unrelated ways. Its own dispatch (ADR
0047: `coding_engine = claude_code | codex`) runs them headless in a
sandboxed worktree with no command-level decisions at all: the sandbox is the
wall. The T3 path (ADR 0054: `skep acp govern`) puts skep's governor on the
ACP permission hop, and every ask is decided, forwarded or refused and
recorded. The question was why skep's own dispatch got less governance than a
third-party client. The ACP adapters (`@agentclientprotocol/claude-agent-acp`,
`codex-acp`) offer the hop; the governor already sits on it.

## The decision

Two engines, `acp:claude-agent-acp` and `acp:codex-acp`. The adapter runs
inside the run's sandbox; skep's governor and the ACP client run outside it,
in the supervisor, on the sandboxed process's stdin/stdout. Landing is
unchanged: patch → G10 with the pinned verify → human approval.

## The wall, stated (I12)

**The OS sandbox is the physical wall.** Strict reads (ADR 0068),
worktree-only writes and the run's network proxy confine the adapter and
everything it runs. G10 (and V1) decide whether the patch is any good. Both
hold whether the adapter asks or not — including an adapter put in a mode
that never asks (`approval=never`, `bypassPermissions`).

**ACP adds two things on top:** mediation of the asks the adapter makes, and
a per-tool record. It is not a second wall and nothing here weakens the
first. The agent's own commands still never pass `workers/capabilities.py`:
the engines are `external`, need a real pinned `verify_command` (v88-F4) and
are forced into sandbox execution (v94-F4), like `claude_code` and `codex`.

## Decisions

### D1 — two registry entries, the CLI twins' declarations

`engines.CODING_ENGINES` gains `acp:claude-agent-acp` and `acp:codex-acp`:
`replace()` of the `claude_code` / `codex` entries with the ACP worker as
`argv` (`python -m skep.workers.acp <adapter>`) and the adapter as `binary`.
Auth env, identity env, toolchain homes, API host and (codex) the plan-login
host are the wrapped CLI's — the adapters run the same binaries. So the
auth-env refusal at dispatch and `skep doctor`'s probe read the same names,
and so can the scheduled external-engine preflight (Phase 04 4.2).
`skep project setup --engine acp:codex-acp` and `skep run --engine …` accept
them.

*R1 (A1):* a worker command is recognised by the module it runs, never by
its last argv token (an ACP entry's is the adapter's name, so `my-tool
codex-acp` matched and `python -m skep.workers.acp npx …` did not).
`engines.external_agent_for_command` answers `acp:<adapter>` when the token
after `skep.workers.acp` is a registered adapter, else `acp` — an
unregistered adapter, still an ACP engine. That one answer drives the
scheduler's external-engine gates, dispatch's sandbox coercion, pin, pipes
and governor, and the spawner's key masks (no `llm-secret` for an ACP
worker). Its rows name `acp:<agent>` (the mapper's name for the adapter
argv). *R2 (R2-3):* the run record names `acp` for such a command (it kept
`builtin` before), so the scheduler and forensics see what ran; a resume
handing `acp` back to dispatch runs the worker command again.

### D2 — amended: the governor is outside the wall

*Draft:* the worker spawns `skep acp govern` inside the sandbox, which reads
the store mounted read-only. *Accepted:* no skep code that reads policy runs
in the sandbox, and `supervisor.sqlite3` is never mounted there.

- **Inside** (`skep.workers.acp.main`): the contract worker. It runs the
  adapter with its own stdin/stdout passed straight through
  (`cli_adapter.AdapterSpec.inherit_stdio`), then takes the patch exactly
  as the CLI engines do (`git diff`, `git diff --check`, the result
  envelope). It refuses to start outside skep's sandbox (`SKEP_SANDBOX`,
  ES-F220). Git never runs host-side on the agent's worktree.
- **Outside** (`skep.workers.acp.AcpEngine`, started by dispatch on the
  pipes `spawn_worker(stdio_pipes=True)` gives it): the ACP client plus
  `Governor` in engine mode (`EngineBinding`). Every frame passes the same
  `Proxy` and handlers the T3 face uses (I5: one implementation of
  "answer the hop").

*R1 (A2):* the wall is not optional. An ACP dispatch is refused before any
run record unless the project's `strict_read_confinement` is on (the I9
message names `skep project setup <repo> --strict-reads`) and the backend a
strict run launches is usable with the sandbox on. Strict reads stay a
project choice for every other engine; for this one they are the engine's.
*R2 (E1):* the project's read wall is read once — the A2 check and the
launch use that one value, so a policy edit in between cannot start the
adapter denylist-only.

*R1 (A7, A15):* `AcpEngine.finish()` joins the session and every forwarded
ask; dispatch calls it before the store closes (in its `finally` too). A
session still running at the timeout is a stated failure, never "none".
*R2 (E5):* the reader is joined before the asks (only it starts one; its EOF
wakes them), and a drive, reader or ask thread alive at the deadline is that
stated failure.

### D3 — a forwarded ask is an approval card; a timeout denies

What the governor cannot decide it forwards, as in T3. Here the client is
skep, so the forward becomes an `acp_permission` row in `chat_actions`,
`source='gate'` — a run waits on it, like a worker gate — in the chat that
dispatched the run, else in a per-run chat (`source='acp'`). It resolves on
the existing faces (ADR 0050): the chat card and
`POST /api/chats/{id}/commands/{action_id}/confirm|deny`. The engine answers
`allow_once` on confirm and `reject_once` on deny — never `*_always` (0054
D6) — and the governor's `forwarded` row names `human@skep`.

**Timeout (I6).** The ticker's sweep skips gate cards, so the engine keeps
its own clock: `card_timeout_seconds` (ADR 0032's setting) capped by the
run's wall clock. On expiry it claims and denies the card itself (a human's
confirm already running wins the claim), answers `reject_once` — the ask, not
the run (open question 2) — and the `forwarded` row's actor is
`auto:card-timeout`, never a human.

*R1 (A5, A6, A10, A13, A14):*

- The card carries its deadline (`expires_at`). A confirm past it never
  claims the card: the commands face denies it (auto) and answers 409. A
  card stuck `executing` (a confirm that died mid-claim) is denied once the
  deadline or the session's end passes and one more poll finds it so.
- The chat is the one `run_task(chat_id=…)` names (a schedule's — the
  scheduler passing `schedule.chat_id` is Phase 04 4.2's half), else the
  chat whose action dispatched the run, else ONE fallback chat per run,
  reused by every later ask.
- A forwarded ask the engine itself failed answers `cancelled` with actor
  `auto:engine-error`, never `human@skep`.
- A card whose run has ended (the serve that held its engine restarted) is
  superseded by the ticker with the run's state — nobody can answer it.

*R2 (E2, E4):*

- The engine and a confirm compete through conditional updates. When the
  engine stops waiting on a card a confirm still holds (`executing`), it
  first settles the card `superseded` ("the engine already answered no"),
  from `executing` only, and then answers `reject_once`. The confirm's own
  resolve then loses with a 409 naming that note. If the confirm resolved
  first, the engine re-reads the card and answers what it says. The card
  and the `forwarded` row always agree.
- A chat dispatch (`dispatch_run`, `quick_edit`, `batch_dispatch`,
  `forge_tool`) hands its chat through `submit_run` → `Dispatcher.submit` →
  `run_task(chat_id=)`, so the run's first ask cards there even before the
  dispatch receipt names the run.

*Not built:* `skep approval answer <id>`. REST + chat already satisfy ADR
0050; a CLI face for `chat_actions` is still overdue.

### D4 — the mode is pinned to one that asks

Both adapters' defaults never send `session/request_permission`. After
`session/new` the engine pins Claude `default` and Codex `read-only` (v1
`session/set_mode`; v2 `session/set_config_option` `mode`), skipping the call
when it is already current. An adapter that does not offer the mode fails
the run with the reason (`… offers no asking mode … ADR 0058 D4, I9`) —
recorded as the run's failure detail, never a silent observe-only run.
`acp_agent_mode` as a project key (open question 3) is not built.

*Measured (R1, A11, 2026-11-23, macOS, `@agentclientprotocol/codex-acp`
2.10.0 / codex 0.171.0, inside skep's strict Seatbelt profile, fake
`CODEX_HOME`, a dummy key, deny-all network):* the adapter starts,
authenticates from the env, offers its modes and accepts `read-only`.
Codex's own sandbox cannot start there — `codex sandbox -- ls` exits 71
with `sandbox_apply: Operation not permitted` (nested Seatbelt; ES-F220 saw
the same for bwrap inside Landlock), while `sandbox_mode=
"danger-full-access"` runs. No codex-acp mode both asks and runs without
codex's sandbox: the four built-in modes send their own sandbox policy on
every turn (config.toml cannot override it), `agent-full-access` never
asks, a `[permissions.<id>]` profile still sandboxes (and may not extend
`:danger-full-access`), and `approval_policy = "untrusted"` is no longer
accepted. So `read-only` stays the pin: each command codex runs fails in
its own sandbox first and codex's on-request approval then asks to run it
without one — an ask skep decides, inside skep's wall. *Owed:* that
escalation with a real model (Test 4.1 on `acp:codex-acp`); if codex gives
up instead of asking, the honest answer is to refuse `acp:codex-acp` and
point at `codex` (headless, its sandbox off, skep's wall).

### D5 — every decision is bound to the run, never to the adapter

`actions.acp_engine_decide` decides with the dispatched run's repo (its
project's run policy), its delegation's role and chain, and records each row
with Phase 02's blame — `task_id`, `engine: acp:<adapter>`,
`caste: coding` — plus `tool_call_id` when the frame names one, and
`actor: acp-engine:<adapter>`. Observed and forwarded rows carry the same
blame and the run's repo. `skep run forensics <task_id>` shows them.

*R1 (A3):* the stack is the dispatched run's own binding
(`project_context`) first — a template-bound run is judged by its project,
never by whatever its repo path resolves to — and the policy cwd (the
floor's for shell paths) is the run's worktree, not the operator's
checkout.

**Paths and hosts are the supervisor's.** The session's cwd is bound to the
worktree, whatever the adapter sends. An item the running wall would refuse
anyway is recorded as the wall's deny (`layer: wall`) with an I9 reason and
never allowed or carded:

- a write whose path resolves outside the worktree (an adapter naming the
  operator's own checkout, a symlink out);
- a command whose claimed cwd is outside the worktree;
- a fetch of a host the run's proxy was not started with — even one a rule
  or a human would allow ("add the host to the project's network policy and
  dispatch again"). Approving it would not open the proxy, and nothing
  widens a running sandbox. *R1 (A4):* the fetch's URL rides the decision
  (its scheme and port), and the check is the proxy's own admission
  (`netproxy.admit_target`: port pins, loopback ports, serve's port) — its
  DNS check stays the proxy's at connect time. *R2 (E8):* an explicit `:0`
  is port 0, not the scheme's default;
- *R1 (A12):* a read outside the worktree and outside the run's recorded
  read allowlist (the `dispatched` line's `read_allowlist`), worded as a
  read. *R2 (E6):* judged where the path resolves (`..`, a symlink out of
  an allowed root), never as spelled; an unresolvable path is outside;
- any `terminal/create` or `fs/write_text_file`: the engine's client
  advertises neither capability, and running one would run on the host.
  Each is still put to `decide()` (a floor deny stays the floor's) and then
  refused.

### D6 — amended: `sandboxed=True` only for the engine

ADR 0061's zero-ask step needs `decide(…, sandboxed=True)`. The engine may
pass it — and is the only caller besides the worker-gate auto-answer — when
dispatch launched the adapter behind a verified wall (execution mode
`sandbox`, the host sandbox on, its backend usable) and the run is its own
unlocked project's (the R3 rule `unlocked_gate_allowed` keeps). So an
unlocked project's normal edit-and-test run cards nothing.

The standalone `skep acp govern` — an IDE's agent on the bare host — has no
`EngineBinding` and answers through `policy_decide`, which has no
`sandboxed` parameter. On the same unlocked project the same command
forwards to the human there.

### D7 — landing is unchanged

Nothing lands through the wire. The worker exits, the patch is the worktree
diff against the startup baseline, G10 re-verifies with the pinned command,
the human approves, landing is the commit (I1, I2). A session failure the
worker inside could not see (no asking mode, an adapter error, a `refusal`
stop) makes the run failed with that reason (I8); it never upgrades
anything. *R1 (A9):* the worker's own result is ingested as it made it (the
store's result fields and the audit's `result.json` agree); the
supervisor's verdict is its own record — a `failed` transition with
`skep acp: <reason>` and `supervisor_verdict.json` (`status`, `reason`,
`worker_status`) beside the worker's result. *R2 (E3):* the verdict file is
written before the claim is ingested, and the ingest's one terminal
transition is `failed` (`ingest_run(supervisor_failure=)`). The run is
never `completed` first. A crash before the ingest leaves the run in
flight; restart recovery reads the verdict file and fails it. A failed
verdict-file write still fails the run in the store. Startup re-verify
fails a `completed` run that has a verdict file (a pre-R2 crash) instead
of running G10 on it.

## Mediated vs observed (I8)

| row | source | means |
|---|---|---|
| a permission ask the governor answered | `run` / `operator` / `floor` / `baseline` with a verdict | **mediated**: decided before the tool ran |
| a wall refusal | `layer: wall`, `verdict: deny` | **mediated**: refused, with why the sandbox would refuse it too |
| a forwarded ask | `forwarded`, `outcome`, actor `human@skep` or `auto:card-timeout` | **mediated** by a human (or by the timeout) |
| a tool call that finished without asking | `observed`, no verdict | **observed only**: it ran unasked inside the sandbox; never claimed as pre-approved |
| one skep has no rule for (a read, a search — R1 A8, engine runs) | `observed`, `scope` null, `value` its title, `kind` | **observed only**, with what the agent called it |

Adapters run read-only commands unasked even in asking modes; those rows are
the honest record of that, and the sandbox is what bounded them.

## What this does not do

- It does not make the adapters pass the capability layer.
- It does not replace `claude_code` / `codex`; headless runs remain.
- It does not govern MCP servers an agent starts, nor make an unasked tool
  call into a decision.
- An ACP session does not survive a serve restart (ADR 0060): the governor
  and client lived in the old serve, so the adapter loses its client and
  stops. Whatever the worker inside then files is re-verified like any other
  run; asks after that point were never made.

## Consequences

- Positive: skep's own dispatch gets the ring it gives T3, with every row
  naming its run; the store never enters a sandbox.
- Negative: a run that asks can wait on a card for up to its timeout; an
  unattended schedule on a locked project will deny on timeout rather than
  progress (4.2's floor and the doctor are where that is warned).
- Risk: the adapters are npm packages skep does not pin; a wire change
  surfaces as `Unreadable` forwards (0054 D5) or a stated mode failure, not
  silent allows.
