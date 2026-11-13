# ADR 0059 — policy v3: one decide(), layer-aware ceilings, scoped grants

**Status:** accepted for rounds P2-1..P4-2, with the operator decisions of
2026-11-10 (below). **Implemented so far: P3-1** (`policy_v3.decide()`,
`load_stack`, the label vocabulary, the `SCOPE_ACTIONS` unmatched table and
the rotated decision log, wired to REST / CLI / hook / ACP — ES-F6, ES-F7)
**P3-2** (the Queen's file and shell lanes, decisions #1 and #2 —
ES-F15–ES-F20), **P3-3** (read_url, search_web, MCP and browse —
ES-F21–ES-F24), **P3-4** (scoped grants — ES-F25–ES-F27), **P3-5** (the
write-time check and replay — ES-F28–ES-F31), **P4-1** (the trifecta guard
— ES-F28–ES-F32; the two rounds were cut from the same commit, so their fix
numbers overlap), **P4-2** (fewer, better cards — ES-F33–ES-F38) **and P3-6**
(the tool surface follows policy — ES-F39–ES-F47).
**Related:** ADR 0022 (one schema), ADR 0026 (operator policy), ADR 0050
(one verb, three faces). Ledger numbers are the edge-sweep ledger's; file
anchors are at origin/main ff9f344 unless marked `r1` (origin/es-policy-r1
@ bdc1e44) and may have moved since.

## Question

Edge-sweep lanes F1, F3, M2 and M3 ran on the real model and showed that skep
has no single policy. **Five decision paths each compose a different subset
of the nine layers**, and every one of them has its own private fallback for
"no rule matched":

| Path | Composes | Unmatched means | Evidence |
|---|---|---|---|
| Queen file reads (`fileio.py:67`) | global + overlay + baseline, role via `tighten_autonomy` | operator roots allow, else card (a private fallback) | f1-01: REST/CLI deny the same repo file |
| Queen shell (`tools.py:2691`) | global (allows stripped) + baseline (allows stripped, ES-F3/F7) | card | f1-26: a baseline deny was dropped until ES-F7 |
| read_url / MCP / browse (`tools.py:2570`, `mcp_client.py:485`) | **global doc only, no baseline** | card / a name-substring risk ladder (`mcp_client.py:115`) | f1-20: `budget_transfer` ran with no card; f1-27: a denied host was fetched |
| search_web (`tools.py:3469`) | operator policy, **no role** | — | f1-35 |
| REST / CLI / hook / ACP (`actions.py:1897,1956`) | floor → baseline guard → run view or operator doc → role → chain | **deny** | f1-01, NEW-F1L-04 = B77 |

Results seen live: the faces disagreed on 6 of 19 probe classes (f1-01); about
9 of the ~23 cards in F1's session were noise (cards that could not succeed,
repeat cards after a grant, "remembered" receipts that stored nothing); the
three most dangerous actions of that session (an MCP money transfer, a
private-file → `search_web` chain, a host-wide fetch grant) got **no card**.
When a card failed, the model asked `policy_why` / `policy_rsop`, got answers
that matched neither the card nor each other, and spent 5–6 extra tool calls
per incident (f1-15, f1-17). One decision function fixes UX as well as safety.

### B12, root-caused (it drives V6/V7)

`_within_delegation` (`actions.py:4374`) decides every host in the worker's
compiled `permissions.network` under the chain, and drops any row that the
chain made stricter (`narrowed_by`, `:4411`; the drop at `:4455`). The
provider host was merged into that list by `resolve_run_policy`
(`policy_resolver.py:702`) before narrowing, so it goes through the same check.

1. The worker's own row (`_decision_record`, `actions.py:1956`): when the home
   has **no `baseline_guard` reason for `network`** (`actions.py:1989`), the
   run view decides. It sees the merged host as a compiled `net:<host>` rule
   → `allow` (f1-30: `allow trusted_local_dev/net:localhost`).
2. The delegator `queen@builder` (`_queen_chain`, `tools.py:2560`): the
   builder role covers `network connect *` with `net-gated` ask
   (`role_covers`, `roles.py:236`), so its bound is `require_approval`
   (`roles.py:319`). That is stricter than allow, so the row becomes
   `exceeds_delegator` and the host is dropped.
3. The worker: `llm_plan.py:213` → `:296` "provider host … not in the task
   network allowlist". The registry hosts (pypi etc.) are kept because the
   builder role allows them, so the list isn't empty.
4. Retry with `network=["localhost"]`: an explicit network skips the v23-F5
   registry merge (`policy_resolver.py:724-735`), the provider host is
   dropped again, and the list is **empty** → `llm_plan.py:294` "require a
   task network allowlist".

**When it fires:** (a) the dispatch carries a chain from a role chat, or a
worker `role` (REST and CLI too, not only chat: `narrowed_by` also fires
on the worker's role), **and** (b) the home has no `no_baseline` guard for
`network`. That means an approved baseline, or an upgraded home that isn't
zero-state (`policy_layers.py:152,174`). The provider host doesn't matter:
`ollama.com` falls under `*` exactly like `localhost` does.

**Why M2 (Mac) didn't reproduce it:** M2's home was never onboarded (no
`onboard`/`policy approve` in its notes; created by first use, so
`fresh_home_seeded_at` is set, `store.py:869`). On a zero-state home every
`network` row, the worker's and the delegator's alike, is
`require_approval baseline/no_baseline`. The ranks are equal, so nothing is
"narrowed" and every host is kept. M2's role-less chat (a) passed no chain
at all. F1's homes were onboarded with `--template … --yes`, so they have a
locked baseline. To confirm on the Mac: `SKEP_HOME=~/.cache/skep-sweep/m2/home
skep policy baseline status` should print no baseline. **New row (B147, P1,
I5):** on a zero-state home, delegation narrows nothing at all.

Also false today: the docstring at `actions.py:4386` says a dropped host makes
"the worker then card for it". For network it doesn't. `capabilities.py:809-818`
asks only when the allowlist is *empty*; a host missing from a non-empty list
is a flat deny.

## Decision

### D1 (V1). One `decide()` every face calls

`policy_v3.decide(req, *, stack, role, chain, face, taint) -> Verdict`.
`req = (scope, action, value, attrs)`. `attrs` carries facts the matcher
needs: the resolved path, the URL's parts, the argv segments, an MCP
`readonly` flag. `Verdict = {verdict, layer, rule_id, decided_by, chain,
deferred, tier}`. It is pure over a `Stack` loaded once per request by
`load_stack(store, home, repo=, chat=)`. Every call appends to
`audit/policy/decisions.jsonl` (rotated at 10 MB), which V11's replay needs.

| Face | Today | Routes through decide() as |
|---|---|---|
| read_file / search_files | `fileio.py:67` | `filesystem.read`, face=queen |
| run_shell / start_process / stop_process | `tools.py:2691`, `:5127` | `shell.run` / `run_background` / `run_repo` |
| read_url (grant + card lanes) | `tools.py:2570`, `:3671` | `network.fetch`, attrs=url |
| search_web | `tools.py:3469` | `network.search`, **with role and taint** |
| call_mcp_tool, browser | `mcp_client.py:485` | `mcp.call` / `browse.*`, attrs.readonly |
| dispatch_run, quick_edit, run_code, batch_dispatch | `actions.py:4570` (no chain: B22) | `dispatch.delegate` with chain, then D3 compiles the permissions |
| every other mutating tool | `tools.py:5127` → None → card | the `TOOL_SCOPES` map or `ALWAYS_CARDS` (D7) |
| REST decide, CLI decide/why, hook, ACP | `actions.py:1897`, `app.py:735`, `cli_cmds.py:2695`, `acp/govern.py:114`, `policy_why` `:2181` | the same call, face=advisory/hard |
| run compile (serve, CLI `skep run`, scheduler, resume) | `resolve_run_policy` + per-face provider merges (`scheduler.py:213`, `cli_cmds.py:775,841`) | one `compile_permissions()` that calls decide() per host and prefix |
| worker capabilities | the compiled `task.permissions` | **unchanged shape**: it consumes the compiled artifact (see Disagreements) |
| unattended turns (prompt schedules, continuations, channels without confirm) | the refusal set in chat.py | face=headless: ask → deny |

**Deleted:** the MCP name-substring ladder (`mcp_client.py:115`), the browse
ladder (`:109`), `_fetch_grant_lane`'s private global-only resolve,
`_OPERATOR_LANES` special-casing (`actions.py:1891`), the role-less search
call, and decision #7's "a delegator's unset is deny" (`roles.py:236`;
unmatched is now ask, so the child gets a deferred ask). **Turned into
rules:** operator roots become generated `global` allows
`filesystem read <root>/**`. The Queen guard, git floor and secret floor
become the `floor` layer, evaluated first, as today. The v83-F9 repo pen
becomes the unmatched default of `shell.run_repo`. **Kept outside
decide():** `no_such_path` (a validity check, not authorization), the
sandbox walls (I12), the patch card and G10 (I1, I2).

**"Unmatched" means one thing:** each `(scope, action)` in `SCOPE_ACTIONS`
carries its unmatched verdict. It is `ask` everywhere except
`shell.run_repo`, which is `deny`. REST, CLI, hook and Queen all read the
same table (fixes B77).

### D2 (V2). Deny > ask > allow, layer-aware

Inside a layer, the most specific rule wins (so `pypi.org` allow beats `*`
ask inside the builder template). Across layers, each layer's hit is
classified as follows:

| Layer | Can grant | Binds as a ceiling |
|---|---|---|
| floor | never | always deny |
| global document (`skep setup`) + overlay | yes **only while no baseline is locked**; tighten-only after (B30) | denies, non-`*` asks |
| template = the locked baseline | yes | denies, non-`*` asks |
| role (chat or worker) | never (as today's `tighten`) | denies, non-`*` asks |
| phase / group / project | yes | denies, non-`*` asks |
| grant (once/session/project/global, D4) | yes | never |
| dispatch (a card-approved manifest, D6) | yes, for that task | — |
| delegation chain | never | the delegator's verdict; its ask passes as **deferred** (D3) |

**A `*` ask is a default, not a ceiling.** `shell-learnable *`,
`net-gated *`, `net-fetch-gated *` and `mcp-gated *` say "ask unless
someone granted this". A grant answers them. A deny or a named ask binds.

Order: floor deny → any binding deny → any binding ask → any allow from a
granting layer → defaults and unmatched (ask, or the SCOPE_ACTIONS verdict)
→ delegation cap → taint (D5) → face (headless: ask → deny). `decided_by` is
always `<layer>[:<name>]/<rule_id>`: `floor/`, `global/`, `template:builder/`,
`role:assistant/`, `group:node-dev/`, `project:<id>/`, `grant:session/<id>`,
`dispatch:<task>/`, `delegation:queen@builder/…`, `default/unmatched`,
`taint/…`. That is one vocabulary instead of four (B79). The card's why-line
is rendered from it, which kills the false "no policy rule covers this tool"
at `onboard.py:580`.

This fixes B56 (a learned grant answers the role's `*` ask) and B30 (a stale
setup document can't lift the baseline), and B12's chain half follows from it.

### D3 (V6 + V7). Supervisor resources; ask delegates as a deferred approval

- **V6a, this ADR.** `Permissions` gains `provider_hosts: list[str]` (each
  entry `host:port`). `compile_permissions` fills it from
  `configured_provider_hosts` for `needs_provider` castes. An external
  engine gets **only** its own `network_host` (B140). `_within_delegation`
  never sees it, because skep's own LLM endpoint isn't a grant the chain
  holds. llm_plan/document/reviewer check `provider_hosts ∪ network`. The
  netproxy admits a provider entry by exact `host:port`, so `localhost`
  stops opening every loopback port (f1-30). This closes B12.
- **V6b, deferred:** the worker never holds the key. netproxy is a CONNECT
  tunnel, so it **can't inject a header into TLS**. Masking needs a
  supervisor-side relay that the worker reaches over plain HTTP and that
  forwards over TLS with the key. That needs its own design (sandbox
  loopback, B05). Until then `llm-secret` stays readable to LLM castes only
  (B125).
- **V7.** A narrowed host or prefix is no longer *dropped*. It moves to
  `Permissions.network_ask` / `shell_ask`. `capabilities.py:809` returns
  `require_approval` (the same gate path as `network_allowlist_missing`).
  The card names the chain ("worker@builder asks network.connect X —
  delegated by queen@builder, which would itself ask"). The approval is
  bound to `(task_id, capability, value)` and dies with the run. Approving
  the dispatch manifest (D6) pre-answers the listed asks. On a headless
  face the deferred ask is a deny. B147 is fixed because decide()'s
  delegation cap compares verdicts, not the baseline-guard placeholder.

### D4 (V8). Scoped, expiring grants

Grants are `LearnedRule`s with three new fields: `lifetime`, `matcher`, and
`bound` (`repo`/`project_id`/`chat_id`). They stay in the one learned list,
so this adds no new store.

| Lifetime | Lives | Bound to | Offered by default |
|---|---|---|---|
| once | the card itself, no row | — | always |
| session | until serve restarts | **chat** (Queen) / **repo** (worker): B39 | yes (default) |
| project | durable | project_id | worker cards, Queen in a repo context |
| global | durable | — | only untainted, only after V11 passes |

| Matcher | Matches | Fixes |
|---|---|---|
| `exact_argv` | normalized argv; a compound line stores one grant per segment, offered only if every segment is grantable | B04, B61 |
| `host` | an exact host (`network.connect`) | — |
| `subdomain` | `*.x.com`: exactly one label, never crosses a dot | — |
| `path_prefix` | realpath prefix on a directory boundary | read widening (V4) |
| `url_prefix` | scheme + host + path prefix; the query must be empty or identical to the card's | B74 / NEW-F1L-05 |
| `mcp_tool` / `mcp_server_reads` | `server:tool` / readonly tools of one server (F3 lesson 4) | B09 noise |

Never offered: `*`, a bare scope, a guarded class (remote git, outbound
content, dangerous prefixes). Fixes:
- **B59:** a keep on a subject that is already granted *upgrades* its
  lifetime in place and returns it.
- **B21:** `start_process` stores `run_background`.
- **B18:** worker "Allow & remember" writes a `project` grant, not the
  enforced `allowed_shell_commands` knob (`actions.py:3506`).
- **B101:** on `no_baseline`, grants apply (each is a human's explicit
  approval) and everything else asks. Tampered: nothing applies.
- **B23:** see D6. At decide time, a grant that intersects a binding deny
  is ignored and logged, not raised through every lane.
- **B32:** a keep the tool can't honour is a 400 naming the tool's
  ALWAYS_CARDS reason.

### D5 (V9). Trifecta guard: two taint bits plus provenance

`chats.taint` holds two bits. Each bit records the first message id that
set it.

| Bit | Set by |
|---|---|
| `private` | a Queen read resolved outside the operator roots (card-confirmed or grant-allowed), **any** run_shell/start_process output, a search_files hit outside the roots |
| `untrusted` | results of read_url, search_web, browse, MCP calls (except operator-flagged trusted servers); chat rows whose provenance isn't operator or queen (webhook, channel non-operator, script output) |

| State | Effect at decide() |
|---|---|
| private only | egress that carries model-chosen text (`network.search`, `network.fetch`, outbound `mcp.call`) → ask, even if granted |
| untrusted only | consequential actions (every mutating tool, dispatch, non-read MCP) → ask, even if granted |
| both | all of the above **plus** every egress → ask; tier HIGH; keep offers shrink to `once` |

A tainted chat is never offered `project`/`global` egress grants. A dispatch
records the chat's taint on its decision, and the run card shows it.
**Provenance (B26):** `chat_messages.provenance` takes one of `operator`,
`queen`, `tool`, `webhook:<id>`, `channel:<platform>:<user>`, `schedule:<id>`
or `worker:<task>`. Webhook text is stored as `role=user`, never
`assistant` (`webhooks.py:150`). Replay wraps every non-operator, non-queen
row in `<untrusted source="…">` inside a user turn, and its presence sets
`untrusted`. The observer and curator read only operator and queen rows
(the B113 class).

### D6 (V10 + V11). Fewer, better cards; analysis on every write

- **Manifest card at dispatch:** the dispatch card lists the compiled hosts,
  `network_ask`, shell prefixes, write roots, engine, mode, verify pin,
  chain and taint. One approval covers them, and anything outside them gets
  a per-call gate card.
- **Folding:** pending cards with the same `(tool, scope, matcher subject)`
  in one chat fold into one card with an item list. Confirm or deny
  resolves them all. HIGH cards never fold.
- **Tiers:**
  - LOW runs without a card (granted, inside the roots).
  - MEDIUM gets the ordinary card.
  - HIGH looks different, offers no keep beyond `once`, never folds and is
    never channel-confirmable. HIGH covers: script schedules (create,
    replace-by-name, `chain`), any doubly tainted action, non-read MCP on
    a server without trust, policy writes, engine change, `network ["*"]`,
    `register_mcp_server`, forge install, and pack promotion.
  - The script-schedule card (F3 lesson 1) shows the command, interval,
    scrubbed env list, the next 3 fire times and chain targets, and states
    "runs as you, on this host, unattended, until disabled".
- **Success measure:** re-run F1's t01–t24 and M3's card script. Noise
  cards are those whose confirm returns a refusal, cards whose subject a
  live grant covered at proposal time, and keep receipts that stored
  nothing. Target: noise = 0 (from ~9), total cards ≤ 12 (from ~23), and
  the three dangerous actions each get a HIGH card. A test computes the
  counts from `chat_actions` + `decisions.jsonl`.
- **V11 on every policy write:** `learn_policy_rule` (`actions.py:4672`),
  `set_operator_policy_rule` (`:3926`), `PUT /api/policy`, group
  set/attach, `project setup`, `skep setup --template`, and baseline
  approve/reapprove. Each one builds the would-be stack and runs §5.4
  checks 1–6. A failure rejects with the deny's name (B23 write half).
  Each write also replays the last 200 `decisions.jsonl` rows against the
  new stack and shows what flips on the card ("now allows 3 past asks: …").
  `skep setup --template` keeps learned grants and shows them in its diff
  (B38).

### D7 (V12). Tool surface follows policy; roles everywhere

- **Hide tools:** a tool is hidden from a chat when decide() is a binding
  deny for its scope/action for every value (assistant: `dispatch_run`,
  `quick_edit`; B22).
- **Map every mutating tool:** each one maps to `TOOL_SCOPES` (tool →
  scope, action, value extractor) or sits in `ALWAYS_CARDS` with a
  one-line reason. A test fails on any unmapped mutating tool (B32).
- **Truthful descriptions:** each description gets a generated "In this
  chat:" line rendered from decide() (e.g. "reads inside ~/repos run;
  elsewhere asks; credential files never"). A test pins the rendering, so
  the line can't drift from the policy (B33 class).
- **B20:** `set_operator_policy` accepts `shell`, with exact-argv allows and
  any denies, written to a `queen.shell` overlay. That is also the only
  sanctioned home for a Queen host-shell deny (NEW-F1L-01's gap).
- **Roles (B55):** `create_chat` requires a role. Channel chats
  (`channels/runtime.py:171`) get `assistant` (F3 lesson 5). Analysis
  chats (`chat.py:2222`) get `operator`. Hermes-import (`hermes_cmds.py:233`)
  and the policy-baseline chat (`actions.py:2058`) get the default role.
  `role_for_chat` maps a legacy NULL role to the default role; with no
  default recorded it uses `assistant`.
- **B78:** search_web takes the role and taint (D1).

## Rejected

- **Keep five lanes and patch each** (what PRs #56–#61 did): every patch
  opened a sibling hole (ES-F3 stripped denies → B72). Faces that disagree
  are themselves the bug.
- **A literal deny > ask > allow across all layers:** a template's `*` ask
  would out-rank every project and learned allow. That makes B18 and B56
  permanent and the card loop pointless. Hence "`*` ask is a default".
- **Workers call decide() per command over IPC:** it needs a worker → serve
  channel, which B05/B51 just closed. The compiled artifact is the
  contract.
- **Credential injection at netproxy:** impossible through a TLS CONNECT
  tunnel (V6b).
- **A new grants table:** the learned list with three fields carries the
  same data, and one store keeps I5 plain.

## Disagreements with the plan's V-list

1. B12's ledger text: the provider host is irrelevant. The switch is the
   baseline state (zero-state homes mask it), REST/CLI with a worker role
   hit it too, and the retry fails because the host is dropped *again* into
   an empty list.
2. V7 is mostly built: `within_delegator` already passes an ask. The defect
   is `_within_delegation` turning "narrowed" into "dropped", and the worker
   denying a host missing from a non-empty list.
3. V1 "worker caps call decide()": no (see Rejected).
4. V2 as written breaks the card loop (see Rejected).
5. V6's credential masking needs a relay, so it splits into V6a/V6b.
6. V9 with only the "both bits" rule misses f1-07 (private read + search,
   no untrusted content), so the table has single-bit rows.

## Consequences

- One verdict per action on every face. `skep policy why` names the layer,
  rule and chain the Queen used.
- Behaviour changes that operators will see (each one is listed by the
  migration dry-run):
  - grants now work in role chats;
  - a stale setup document stops granting once a baseline is locked;
  - REST and the hook answer ask where they said deny;
  - search_web asks after a shell or outside-roots read;
  - assistant chats lose `dispatch_run`;
  - channel chats become `assistant`.
- `decisions.jsonl` grows with every Queen call (it rotates).
- V6b and the per-server MCP "trust reads" UI are follow-ups.

## Migration

| Home state | What v3 does |
|---|---|
| Locked baseline with the legacy `fs-read *` (f1-06) | `template_layer()` drops an exact legacy `filesystem read *` allow from a baseline that names a shipped template, after `verify` (narrowing only). doctor says so. `--reapprove --template` adopts the shipped file. |
| No `policy_engine_v2_scopes` (B31) | v3 has no v1/v2 split, so every scope routes through decide(). The setting is ignored and then removed. |
| Learned rules | Shell rules → `exact_argv`, `global`. A compound pattern splits per segment, and one that can't be granted is dropped and logged. Host-wide fetch grants → `host` matcher, labelled `legacy-host` and listed by doctor; taint still gates them. |
| Worker session tier | Cleared at restart anyway; new grants are repo-bound. |
| Project knob `allowed_shell_commands` | Still read; new remembers go to `project` grants. |
| Groups (ADR 0048) | Unchanged; they are a granting layer, and V11 runs on their writes. |
| NULL-role chats | Default role (D7). |

`skep policy migrate --dry-run` replays the home's `decisions.jsonl`
(V11's replay) under v3 and prints every flip. `onboard --check` and doctor
print the same summary. `skep policy approve --reapprove` gains `--diff`,
which shows the V11 flips before it re-locks.

## Test plan

- **§5.4 property test, extended:** template × role(none + 3) × face × taint
  × chain depth(0, 1). Checks 1–6 are unreachable. Check 5 covers "a child
  never holds an allow its delegator lacks"; the child may hold a deferred
  ask.
- **Cross-face equality:** f1-01's 19 probes × 5 homes give one verdict and
  one `decided_by` across Queen, REST, CLI, hook and ACP.
- **Regressions, one per ledger row:** B12 (role chat + locked baseline,
  `localhost` and `ollama.com`), B147, B56, B59, B21, B61, B74, B39, B18,
  B101, B23, B30, B31, B77, B78, B79, B20, B22, B32, B55, B26, B75/f1-07.
- **Card-noise metric test** (D6) over a scripted F1 transcript.
- **Every gate** in CLAUDE.md, plus `pytest -m smoke`.

## Implementation rounds (each ≤ ~600 lines, shippable alone)

| # | Round | Closes |
|---|---|---|
| P2-1 | V6a: `provider_hosts`, `host:port` matching, external engines get only their own host | B12, B147-partial, B140 (merge) |
| P2-2 | V7: `network_ask`/`shell_ask`, chain-naming gate cards, headless deny; `dispatch_run_decision` consults the chain | B12 residue, B22 (card half), B147 |
| P3-1 | `policy_v3.decide()` + `load_stack` + labels + SCOPE_ACTIONS unmatched table, wired to REST/CLI/hook/ACP only; decision log | B77, B79, B34 rest |
| P3-2 | Queen file + shell lanes → decide(); operator roots as rules; `*`-ask-is-default; global doc tighten-only under a baseline | B30, B56 (shell), B31, B01-upgraded |
| P3-3 | read_url, search_web, MCP, browse → decide(); ladders deleted | B29, B09 residue, B78, B56 (network) |
| P3-4 | V8 grants: fields, matchers, lifetimes, the remember paths (Queen + worker + session tier) | B04 rest, B18, B21, B39, B59, B61, B74, B101, B32 |
| P3-5 | V11: write-time checks on every writer, replay diff on cards, `migrate --dry-run`, `--reapprove --diff` | B23, B38, B31 signal |
| P3-6 | V12: hidden tools, TOOL_SCOPES/ALWAYS_CARDS, generated descriptions, `set_operator_policy shell`, roles at chat creation | B20, B22, B32, B33 class, B55, NEW-F1L-01 |
| P4-1 | V9: taint bits, provenance column, replay wrapping, observer filter | B26, B75, f1-07 |
| P4-2 | V10: manifest card, folding, HIGH tier, script-schedule card, noise metric | F1 noise, F3 lesson 1, B111 (card half) |

P2-1/P2-2 don't need decide() and ship first. P3-1 must land before P3-2–6.
P4 needs P3-3 (taint is a decide() input).

## Invariants touched

I5 (one boundary, no private fallbacks), I6 (headless ask → deny, HIGH never
channel-confirmable), I8 (one label vocabulary, honest receipts), I9
(generated descriptions, a hidden tool beats one that can't succeed), I12
(provider hosts as `host:port`, V6b), I13 (every face's decision recorded).
I1, I2 and I4 are untouched: the floor and the patch card stay above
decide().

## Operator decisions (2026-11-10)

1. **A catch-all `ask` is a default, not a ceiling.** Approved grants (session/project/always) answer a role's or baseline's `ask *`; explicit denies and the floor still win. Closes B56, B18.
2. **Under a locked baseline, the `skep setup` document can only tighten.** Closes B30. Old allows stop granting on homes that also have a baseline.
3. **Any `run_shell` output sets the chat's "read private" taint**, so later `search_web`/`read_url` in that chat card. Closes the f1-07 leak.
4. **Every role-less chat → `assistant`.** This includes channel chats, analysis chats and legacy chats. It is stricter than the ADR's proposal (analysis → operator, legacy → default role); the operator chose the strictest option. Closes B55.
5. Not asked; the ADR default stands: V6b (proxy-held provider key) is deferred until a relay exists.

## Implementation status

| Round | State | Notes |
|---|---|---|
| P3-1 | **implemented** (ES-F6, ES-F7) | `src/skep/supervisor/policy_v3.py`. REST `policy/decide`, `skep policy decide` / `why`, `GET /api/policy/why`, the `policy_why` read tool, the Claude Code hook, `skep acp govern` and the dispatch's delegation narrowing call `decide()`. Unmatched: ask (deny for `shell.run_repo`), B77. Labels: B79, B34. The decision log redacts inline credentials and rotates at 10 MB. Deliberately not yet: decision #1 (`*` ask is a default) and #2 (global document tighten-only), which change verdicts and belong to P3-2; `taint` and `tier` (P4); a `chat=` stack input (P3-2). Still as before, though D1 lists them as gone: `network.search` / `fetch` answer from the operator document (`policy_v3.OPERATOR_LANES`, moved, not deleted); a delegator whose role has no rule holds `deny` (decision #7, `roles.within_delegator`); a request carries no `attrs`. |
| P2-1 | **implemented** | `Permissions.provider_hosts` (`host:port`, contract 0.3.6), filled by `provider_hosts_for()` and overwritten in `run_task`; the delegation narrowing never sees it. B12, B140 (merge half). Not done: loopback stayed open to every port but serve's (v115-F9) until D6 closed it (ES-F91, row R2a); V6b (the worker never holds the key). |
| P2-2 | **implemented** | `network_ask` / `shell_ask` (contract 0.3.7): a grant the chain turns into an ask is the worker's deferred ask where a human answers, approve-once for that run; a deny, a headless dispatch, a wildcard and a guarded prefix are dropped. `dispatch_run_decision` consults the chain (B22, card half). With P3-1's unmatched = ask, a per-dispatch `network` host no rule covers is a deferred ask on a delegated run, or dropped where no human answers. B147 (ES-F45, after P4-2's manifest): on a home with no approved baseline a delegated worker's grants are deferred asks where a human answers (dropped where none can); the dispatch card lists them and the desk's confirm answers them for that run, so a sandboxed install reaches its registry; a chat dispatch the project would auto-allow cards anyway when its sandboxed run would ask about a host (`manifest_asks`), on every home (workspace mode has no proxy; a shell ask is the worker's own card). quick_edit, batch_dispatch and team cards carry no manifest, so their asks stay asks. Not done: the proxy cards no ask host the card did not list. |
| P3-2 | **implemented** (ES-F15–ES-F20) | `read_file` / `search_files` decide `filesystem.read`, `run_shell` / `start_process` / `stop_process` `shell.run` / `run_background` / `run_repo`, through `decide()` (`policy_v3.queen_decide`, actor `queen` in the decision log): the verdict and `decided_by` REST, the CLI and the hook give with no repo. D2 inside `decide()`: per layer the most specific rule wins; across layers any deny, then any named ask binds; decision #1 — a role's catch-all `*` ask yields to the Queen's learned grant (`grant:session` / `grant:global` on the operator lane, shell and filesystem only) (B56) — not to the worker session tier (the run lane's `grant:session`, a prefix held for every repo, B39, which stays open), and not yet to a fetch or MCP grant (P3-3); decision #2 — under a locked baseline the setup document's allows leave the operator lane (B30). Operator roots are generated `global/operator-root` rules, consulted where no explicit rule matched (the fallback they replace, so they never outrank one). B31: the operator lane composes the baseline in every scope. B23 / §5.4 #6: a learned rule inside a deny is ignored and named (`ignored_grants`), not raised. B01-upgraded: `template_layer` drops a legacy `filesystem read *`; doctor and `onboard --check` say so, and when setup-document allows stopped granting. A frozen table (`tests/fixtures/policy_v3/pre_p3_2_queen_verdicts.json`) holds the lanes to their pre-P3-2 answers. Differs from the D2 table, on purpose: only a grant answers a role's `*` ask — a template, phase, group or project allow does not yet (the round's rule: no loosening but a human's grant); the operator overlay (`set_operator_policy`) still grants under a baseline — decision #2 names only the setup document. Not yet: `face=headless` is no `decide()` input (unattended turns keep chat.py's refusal, unchanged); `baseline_guard` still reads `policy_engine_v2_scopes` for `no_baseline`; the repo pen's background refusal and `no_such_path` stay outside `decide()`; read_url / search_web / MCP / browse (P3-3), the run compile and the ops bounds still read the setup document's allows and raise on B23. |
| P3-3 | **implemented** (ES-F21–ES-F24) | read_url decides `network.fetch <host>`, search_web `network.search` (value `ddgs`, never the query), call_mcp_tool `mcp.call` / `email.read`·`send` / `browse.read`·`act` (value `server:tool`, never the arguments), through `decide()` with the chat's role (`policy_v3.queen_decide`, actor `queen`). read_url's card lane re-decides after the confirm (a deny refuses and nothing is fetched, f1-27) and every redirect hop re-decides (the granted lane follows allowed hops only; the card lane refuses a denied hop, where it followed every hop before). The baseline reaches all four (B29), a tampered or zero-state home asks, a learned grant answers a role's catch-all `net-fetch-gated` / `mcp-gated` ask (B56, network half), search_web follows the role (B78). `read_only` is a `decide()` input (REST `read_only`, CLI `--read-only`): the server's `readOnlyHint` plus a leading read verb, an email read, a browse page-state read. A template `mcp.call` allow grants only a read-only tool (`calendar:*` covers the calendar server's reads, never `delete_event`: B09 residue); an unmatched read-only request answers `default/read-only` allow, the read default the ladder held. Deleted: `_fetch_grant_lane` (the global-document-only resolve), the role-less search call, `_mcp_scope_lane` (the MCP risk ladder's and the browse ladder's decision halves; `classify_mcp_risk` / `classify_browse_action` stay, as inputs), `roles.tighten_autonomy`. A frozen table (`tests/fixtures/policy_v3/pre_p3_3_net_verdicts.json`) holds the lanes to their pre-P3-3 answers. Differs from D1, on purpose: `OPERATOR_LANES` stays — the Claude Code hook's WebFetch / WebSearch from a checkout would otherwise be decided by the worker's run policy; the operator overlay's `network fetch` allows (`set_operator_policy`) do not grant read_url yet (operator decision, `policy_v3.operator_lane`): recorded and named by `policy why` (`overlay_fetch_allow`), while its fetch denies and asks bind. Not yet: grant matchers and lifetimes (a fetch grant still covers its whole host, any path or port — B74 is P3-4); taint (P4-1); HIGH cards for non-read MCP (P4-2); `face=headless` (unattended turns keep chat.py's refusal); the decision log does not record `read_only`; no MCP server trust flag exists to probe. Old helpers that keep callers: `resolve_operator_policy` (`rsop_view`'s operator section, `set_operator_policy_rule`'s deny vetting, `policy_golden`), `queen_global_document` / `queen_template_layer` (`policy_v3.operator_lane`, `resolve_operator_policy`, `policy_layers.operator_layers`). |
| P3-4 | **implemented** (ES-F25–ES-F27) | `LearnedRule` gains `lifetime` (`session` / `project` / `global`; `once` writes no row), `matcher` and `bound` (`repo` / `project_id` / `chat_id`) on the one learned list; a row without them loads as it always matched (`lifetime_of` / `matcher_of`: session by provenance else global; shell `exact_argv`, network `host` labelled `legacy-host`, else glob) and an unbound row applies everywhere it did. Every remember path writes one: a Queen card keep (`remember_action_for_session`) — read_url a `url_prefix` (scheme + host[:port] + the path's directory, the card's query or none; dot segments, userinfo and backslashes never match: B74), run_shell `exact_argv` per segment of a compound line, offered only if every segment is grantable (B61), start_process `run_background` (B21); `session` is bound to the chat (B39), `always` is global and unbound. A worker plain approve is a `session` grant bound to the repo (B39: the global `session_allowed_shell_commands` tier is no longer read or written); "Allow & remember", `--allow-command`, the ledger remember and `allow_shell_command repo=` write a `project` grant bound to the project, or the repo when it has none (B18: not the knob a locked baseline enforces) — both argv prefixes in the compiled allowlist (`worker_shell_grants`, minus any a setup-document or baseline shell deny intersects), unchanged in meaning. A keep on a subject already held upgrades it in place or reports what holds (B59). `decide()` takes `chat=` (via `load_stack`) and `url=` (REST and the CLI pass a URL as the `network` value); in a line with a lone `&` a `run` grant covers nothing (B21); on `no_baseline` a grant-labelled allow on the operator lane applies, still under every deny and named ask (B101); a tampered baseline applies nothing. A keep the card cannot honour is a 400 before it runs, and `card.keep` lists only the keeps the server honours (B32, item 10). Receipts carry `lifetime`, `matcher`, `bound`, `status`; allow-command answers `remembered: project:<id>` / `repo:<path>`. `skep doctor` lists legacy-host fetch grants. A frozen table (`tests/fixtures/policy_v3/pre_p3_4_grant_verdicts.json`) holds every grant kind to its pre-P3-4 answers. Wider, by decision: B101 (81 zero-home cells), B18 (one cell per locked home) — and the kept subject itself where the keep used to store something that never matched (B21: the start_process command; B61: the compound line and its segments). Not done: the `subdomain`, `path_prefix`, `mcp_tool` matchers (MCP grants stay a glob); the `allow-host` worker remember and the ledger's network remember now store a `project` grant on `network.connect <host>` too (ES-F48, B18's network half: bound to the project or the repo, read by the run compile, dropped there under a setup-document or baseline connect deny, receipts name the binding); the Claude Code hook decides WebFetch by host, so a `url_prefix` grant does not cover it (stricter); redirect hops are decided by host only; `project` lifetime is not offered on Queen cards; legacy unbound session rows keep matching until the serve restart drops them. |
| P3-5 | **implemented** (ES-F28–ES-F31) | One write-time check, `policy_write.vet_write`, called by every policy writer before it stores anything: `store_grant` (so `learn_policy_rule` / `allow_fetch_domain` / `allow_mcp_tool`, every card keep, the worker plain approve and "Allow & remember"), `set_operator_policy_rule`, `update_policy` (`PUT /api/policy`, `set_policy`, the knobs `skep setup --template` writes, onboard's sandbox knob), `set_policy_group`, `attach_policy_group`, project setup (`setup_project_record`), `skep setup --template`'s document, and baseline approve / `--reapprove` (REST, CLI, the card, onboard). It composes the WOULD-BE operator stack (the store's settings with the write's pending values over them, a candidate baseline composed as `template_layer` will compose it once locked) and runs §5.4 checks 1, 2, 3 and 6; a finding the current stack does not already hold refuses the write (`PolicyWriteRejected`, a 400 on REST and chat) naming the rule and layer and what would be accepted, and nothing is written. 1: an explicit read allow (not a generated operator root) matching a secret-floor probe. 2: a read allow whose literal directory lies outside every operator root, while `decide()` allows `network` fetch, connect or search to an unnamed host. 3: `decide()` allows a probe install or networked line on the Queen's host shell (run or run_background). 6: a new allow — a grant (vetted unbound) or an overlay rule — intersecting a deny of the setup document, overlay, baseline or the confirming chat's role, or the floor (B23 write half). Deleted: the three private vetting paths (`store_grant`'s and `_denied_grant`'s `resolve()` + `LearnedRuleRejected`, `set_operator_policy_rule`'s `resolve_operator_policy` deny scan and its dry-run that refused a deny over a learned grant). A deny is always accepted: a grant it newly shadows is a warning, decide() ignores it as before. Each write replays the last 200 `decisions.jsonl` rows (`.1` first) under the current and the would-be stack and reports the flips, by count, with rule ids and two short examples each; advisory, never blocking. A policy-write card is checked at PROPOSAL (`tools.policy_write_preview`): a refused one gets no card, the model gets the refusal (B22's lesson); a passed one carries `replay` and `policy_warnings`; a keep-able card carries the replay of an `always` keep, or only `once` with `keep_refused` when the check refuses its grants. B38: `skep setup --template` keeps every learned grant, the Queen's and the workers', and lists them in its diff; one the new document's denies collide with is dropped with a line naming the deny. `skep policy migrate --dry-run` replays the log under today's stack against each row's logged verdict, lists the §5.4 combinations the home already holds and the B31 upgrade (a locked baseline on a home whose `policy_engine_v2_scopes` misses scopes), and writes nothing; doctor and `onboard --check` print its one line when there is something to say. `skep policy approve --diff` shows the replay before the lock. B82: `PUT /api/policy` keeps `card_timeout_seconds` and answers 400 naming an unknown key. Differs from §5.4 / D6, on purpose: check 2 counts `network search` as egress (the f1-07 pair), so with the default operator document's `net:search *` allow, a new read allow outside the operator roots is refused until search is denied; check 3 is probed through `decide()`, so an exact-argv grant of one line passes (a human approved that line). Not here: check 4 is the sandbox's (its probe tests); check 5 is `_within_delegation` at dispatch. The run lane (groups, project overlays, knobs) carries no rule checks 1–3 read, so its writers run the check on an unchanged operator stack. Replay limits, counted as "not replayable" with the reason, never guessed: rows decided in a repo (the run lane is not replayed), delegated rows, values the log redacted, `read_only` requests (the fact is not logged), `network.fetch` (the URL a `url_prefix` grant needs is not logged), rows a chat-bound session grant decided (the chat is not logged), unreadable rows; rotation beyond `.1` is gone. Not done: grant writes stay an unlocked read-modify-write on one setting (a compare-and-set in `store_grant` would need the store to expose a conditional `set_setting`); check 1 probes fixed paths (a rule covering only some repo's `.env` passes; the floor still denies the read); the replay does not record the chat or URL to make those rows replayable. |
| P4-1 | **implemented** (ES-F28–ES-F32) | `chats.taint` holds two bits, `private` and `untrusted`, each the id of the first message that set it (additive column, NULL = no taint; sticky — only the operator's `/trust` clears them (ES-F58: the web and terminal decks, `POST /api/chats/{id}/trust`, an interactive `skep trust`; never a model tool, a chat's shell or schedule, a clarification button, a channel or an unattended turn; recorded in `audit/policy/taint-cleared.jsonl` and a system line, and rows up to `trusted_through` stay wrapped but no longer set a bit), compaction never touches them, a serve restart reloads them). Fail closed: a missing chat or an unreadable value reads as both bits. `decide()` applies them last before the face (`policy_v3.taint_verdict`, from `Stack.taint`, loaded by `load_stack(chat=)`): only an allow moves, to an ask labelled `taint/private-egress` (private: `network.search` / `network.fetch`, a non-read `mcp.call`, `email.send`, `browse.act`), `taint/untrusted-action` (untrusted: the shell lanes, a non-read MCP call, and every Queen mutation allowed outside `decide()` — an auto-dispatch, quick_edit, run_code, batch_dispatch, remember — asked as `dispatch.delegate`) or `taint/trifecta` (both: those plus every egress, the card `tier: high`, keeps `once` only). A deny (the floor too) stays a deny; an ask keeps its own label; the delegation-cap recursion runs untainted. REST, the CLI, the hook and ACP carry no chat: no taint. Set by (`serve/taint.py`): private — a read_file resolved outside the operator roots, search_files hits outside them, any run_shell / start_process / read_process_log output (decision #3), an email read; untrusted — read_url, search_web and every MCP reply (no trust flag exists), and every row whose provenance is not operator / queen / tool (set in the same write); a read of another chat (search_chats, get_chat_messages, get_chat_context) or an analyst's answer carries that chat's bits; analysts start with the delegating chat's bits. Not set by: a read inside the roots, a refused read, a tool that failed. A live turn cards a taint-asked search_web (one taint card per tool per round, the rest wait); an unattended or /btw turn refuses it and says why. A tainted chat is offered no standing fetch grant and no session keep its own taint would ask past (the 400 and `card.keep` come from one function). Provenance (B26): `chat_messages.provenance` (NULL reads as the role's author: nothing is retroactively tainted); webhook rows are `user` / `webhook:<name>`, script output `schedule:<name>`, a continuation's worker summary `worker:<task>` with skep's prompt as `queen` (B37); allow-listed channel authors stay `operator` (strangers never reach a chat, B114). Replay wraps outside rows in `<untrusted source="…">` user turns (inner tags neutralised) plus one system line; the digest keeps them wrapped. The observer and the conversation drafter read only operator rows (B113). A dispatch records the chat's bits (`constraints.chat_taint`), shown on the run card. A frozen table (`tests/fixtures/policy_v3/pre_p4_1_taint_verdicts.json`) is byte-identical with taint none. Since ES-F50–F52: notes, tasks and memory proposals carry the writing chat's bits (webhook and script-schedule notes `untrusted`), approved memory is operator-trusted and carries none; `get_run` / `await_runs` / `list_runs` and reads under the home's `results/` and `worktrees/` set `untrusted`, the store and `audit/` set `private`; `policy_why` / `policy_rsop` answer for the asking chat. Not done: a per-server MCP trust flag; taint into workers; worker-written observation memory (the run harvester, curator observations) carries no taint; a shell egress (`curl -d`) under private only is not gated (D5 names no shell row); HIGH beyond the doubly tainted card and card folding are P4-2. |
| P4-2 | **implemented** (ES-F33–ES-F38) | **HIGH** (`cards.high_tier`, one function the card and every confirm call): a doubly tainted chat; a script schedule, a chained schedule, a schedule replacing a script by name; a non-read MCP call not allowed by exact `server:tool` (no per-server trust flag exists — the operator's `allow_mcp_tool` grant naming the tool is the one trust mark; a glob grant is not); the policy writes (`set_operator_policy`, `set_policy`, `approve_policy_baseline`, the `allow_*` grants, `apply_policy_preset`, `copy_project_policy`, group set/attach/detach, `set_role`; `revoke_policy_rule` narrows and stays ordinary; a card keep=always stays the operator's choice on an ordinary card — a channel confirm never names a keep); an explicit engine; `network '*'` from the call or the compiled manifest; `register_mcp_server` / `setup_browser`; `promote_tool`; `promote_skill_pack`. A HIGH card says so in words, holds once (a wider keep is a 400), never folds, is never auto-allowed (a grant or auto-dispatch allow cards as `tier/high`; `decide()` is untouched) and no messenger can confirm it: `ChatEngine.verdict_events` refuses a `channel:` actor with a 403 naming the web UI and the taint, so Discord (button, reaction, `/skep approve`), Slack and Telegram all reach the refusal; the ticker still expires one; `skep chat --yes` leaves one for a named confirm. **Folding** (`cards.fold_subject`): read_url by origin, a taint-asked search_web by lane, keyed with the rule that asked; nothing else. Folds live for one model round; the card is emitted when the round closes and nothing can confirm before (the turn lock), so no item joins a card already shown; the one claim (PR #61) resolves every item once. **Script-schedule card**: command, interval, the env names the script gets (`scheduler.script_env`), the next three runs (`scheduler.next_fire_times`), the chain walked, what it replaces, "runs as you, on this host, unattended, until disabled". **Manifest** (`actions.dispatch_manifest`, side-effect-free): hosts, provider hosts, deferred asks, shell prefixes, write roots, engine, mode, verify pin, chain, taint — recorded on the card; the operator confirming it at the web UI or CLI answers the asks it LISTED for that run (moved into that run's allowlist, `constraints.manifest_preanswered`); a channel confirm, an auto-allowed and a headless dispatch answer nothing; a dispatch the policy refuses outright is answered at proposal, not carded. `chat_actions.card_json` (nullable, added in place) holds what the server recorded. **Noise metric** (`test_card_noise.py`, F1's t01–t24 on the fixture home): 17 cards, noise 1 — `set_operator_policy scope=shell` cards and is refused on confirm (B20, P3-6). Targets: noise 0 missed by that one card; total ≤ 12 missed (17: each remaining card is a first-time question in its own chat — F1's repeat and dead-end cards are gone); the MCP transfer is HIGH; private file → search is one folded MEDIUM card with the taint line (D5: HIGH needs both bits); the host-wide fetch grant can no longer come from a read_url keep (P3-4 stores a URL prefix) and its one remaining path, `allow_fetch_domain`, is HIGH. `start_research` carries the same manifest (ES-F70, L23: its source hosts the role asks on are listed and the desk confirm answers them, so a builder chat's research run reaches its own sources). Not done: manifests for `quick_edit` / `batch_dispatch` / `forge_tool` (they still pre-answer nothing); a per-server MCP trust flag; folding of anything but read_url and search_web; a fold across rounds (by design). |
| P3-6 | **implemented** (ES-F39–ES-F47) | **Map (B32):** `tools.TOOL_SCOPES` (tool → scope, action, value: read_file / search_files `filesystem.read`, run_shell `shell.run`, start_process / stop_process `shell.run_background`, read_url `network.fetch`, search_web `network.search`, call_mcp_tool `mcp.call`, and the six worker dispatchers `filesystem.write workspace` through the chat's chain) or `tools.ALWAYS_CARDS` (every other mutating tool, with the reason no rule or keep governs it; a refused keep shows it); a test fails on a mutating tool in neither. **Roles (B55, decision #4):** every chat the code creates records a role — `POST /api/chats` with no pick and no default, or `role: ""`, is `assistant` (`default_role` falls back to it), as are channel, analysis, hermes-import and policy-baseline chats; the web picker drops "no role" for new chats. A chat whose stored role is NULL (made before every chat recorded one) reads as `assistant` (`roles.role_for_chat`, `chat.chat_role`) on every face — a turn, `/btw`, a run-finish continuation, an unattended tick, a channel; no chat (REST, the CLI, the hook) is still no role. Stored rows are not rewritten: `skep role use builder --chat <id>` or `/role builder` gives the tools back; doctor and `onboard --check` count such chats ("run as assistant") and name that command; the web picker labels one "role: assistant (none stored)". The P3-4 / P3-3 / P4-1 frozen grids' "-" row meant a chat with no role layer, so it is now a chat in a custom role that binds no template (`tests/supervisor/conftest.no_layer_role`): every grants and net-lane cell holds; the taint grid's "-" dispatch cells deny (decision #7: a layer-less hop delegates no write; 60 per home); the surface grid's NULL web chat equals the assistant row cell for cell. **Hide (B22 residue, I9):** a tool whose map entry the chat's role denies for every value (`roles.role_denies_every`; role-only, never taint or turn state) is left out of the specs and the tool index, never described, and refused if called anyway (`role.deny.tool_hidden`, saying what to do) on every home — a lane that refuses keeps its own words, one that would card or allow is overruled (W2: where the run policy cannot resolve a dispatch, a tampered baseline or the assistant baseline's gated repo, the hidden dispatch tools used to card and fail on confirm; the surface grid's 7 and the taint grid's 72 such cells now deny). A role that hides tools adds one prompt line naming them and what to do instead; the static research sentence and the read_url / search_web descriptions say "where start_research is offered" (W3). Of the shipped roles only `assistant` hides anything: dispatch_run, quick_edit, run_code, batch_dispatch, start_research, forge_tool (start_research and forge_tool no longer card and 403). A custom role with no `policy_layer` hides the same six (its hop delegates no write, decision #7) and otherwise has the baseline's Queen lanes with no role layer — what every role-less chat had before P3-6. `dispatch_run role=` and a batch member's `role=` are refused at proposal when that worker role may not write the workspace. **B20 / NEW-F1L-01:** `set_operator_policy scope=shell` — an allow stores exactly what an approved run_shell keep=always stores (exact argv per segment, `global`, through `vet_write`, revocable), refused for a prefix, an interpreter (a versioned name too: `python3.12`), a launcher that runs the command after it (`nice`, `timeout`, `busybox`, …), an environment assignment, a network or remote-acting tool (`gh`, `docker`, `kubectl`, cloud CLIs, `open`), privilege (`su`, `pkexec`, `doas`), a package install or fetch-and-run (`npm ci`, `uvx`, `pnpm dlx`), `find -exec`/`-delete`, git configuration (`git -c`, `git config`), remote git, `sh -c` and opaque lines, a credential path, and any newline or `#`, answered at proposal (a denylist: an exact line it does not name — a task runner such as `make test` — stores, on a HIGH card); a deny lands in the overlay's `shell` scope (the Queen's lanes only), may be a prefix, binds and beats grants. Card-noise metric: 17 cards, noise 0. **Lines (B33 class):** read_file, search_files, run_shell, start_process, read_url, search_web and call_mcp_tool end with an "In this chat:" line rendered from `decide()` for the role and home (no chat: no taint, no chat-bound grant; nothing logged). A frozen table (`tests/fixtures/policy_v3/pre_p3_6_surface_verdicts.json`, 6 homes × 7 chat kinds × every tool) holds the surface to its pre-P3-6 answers. Differs from D7, on purpose: decision #4 overrides D7's role bullets (analysis is `assistant`, not `operator`); the "`queen.shell` overlay" is the overlay's existing `shell` scope for denies and the one learned list for allows (the overlay is already Queen-only; an allow there would match as a prefix); dispatch_run carries no generated line (its gate is the project's posture; the fresh-chat floor stays pinned). Not done: builder and operator chats measure ~25.5K / ~25.4K against the 25.1K fresh-chat pin, which measures a fresh (assistant) chat only. |
| Hardening B | **implemented** (ES-F45–ES-F53) | **HIGH** also covers provider changes (`set_assistant_model` with a base URL or another protocol, `add_provider`, `use_provider`) and a script schedule brought back (`set_schedule_enabled enabled=true`, `run_schedule_now`). **Folds:** an item that raises after an earlier item ran ends the card `failed`, the rest `interrupted`, never re-run. **Manifest:** also read roots, env allowlist, git mutation, plugin risks, allowed tools, auto-apply; `submit_run` and `dispatch_manifest` share `actions.compile_run`; the confirm recompiles, judges HIGH on the fresh manifest and refuses (409, stale) one that widens what the card showed. **Policy writes** take one in-process lock (`policy_write.policy_write`) across read → check → write; the policy document and the overlay are written compare-and-set (`RunStore.compare_and_set_setting`, 5 retries) against the CLI racing serve; the replay runs after the lock. **Operator roots:** `registry.too_broad_root` (`/`, `/home`, `/Users`, the home directory, any ancestor of it or of the skep store) on every binding face; project setup's write check sees the would-be roots (check 1 refuses a root holding a secret-floor path). **Check 2 warns** (operator decision): the write lands with a `policy_warnings` line. **Replay:** the line carries counts and the deciding layers, never a logged value or a rule id (a learned rule id is `scope:action:<pattern>`); values are plain text and only `migrate --dry-run` prints them; the log records `chat` and a fetch's `url` directory (no query, no userinfo), so fetch rows and chat-bound grants replay. **Discord:** a reaction or bare `/skep approve|deny` resolves a card only when exactly one waits. Not done: group, project and baseline writes from the CLI racing serve are not compare-and-set; a chat row on a baseline write and a fetch whose query was dropped are not replayed. |
| R2a (operator decisions 2026-11-13) | **implemented** (ES-F90–) | **D5 (ES-F90):** a dispatch's explicit `network` ADDS to the project's hosts (default, registries, remembered hosts); `[]` still means only the worker's own LLM, omitting it still means the project's hosts; a named list on a project whose hosts are `*` stays the named list (the stricter reading). The card names what the list adds (`network_added`); `network_dropped` now only applies to `[]`. The ceilings (project policy, role chain, locked baseline) bind as before. **D6 (ES-F91, supersedes v115-F9):** loopback is closed to a sandboxed worker except its local provider's port and the project policy's `loopback_ports` (registry knob, project layer; `skep project setup --loopback-port`, chat `setup_project` `policy_overrides`); serve's port never opens. `Permissions.loopback_ports` (contract 0.3.8) carries the project's list; `netproxy.run_loopback_ports` adds a local provider's port and drops serve's, and the Seatbelt profile, the proxy (`admit_target`), netshim (bubblewrap) and the worker's fetch gate all read that one set. The manifest and card show it; re-verification gets the project's ports; a closed port's refusal names the port and the setup command; doctor and `policy migrate --dry-run` name projects that open none. Not done: a bubblewrap `*` run shares the host's network namespace, so its loopback is not port-filtered (needs a netns plus the proxy); the diagnose and forged-tool sandboxes get no project ports (closed). |

### What ADR 0059 still leaves open

Collected from the rows above:

- **Roles:** builder and operator tool surfaces sit above the fresh-chat byte pin (P3-6); `face=headless` is no `decide()` input — unattended turns keep chat.py's refusal (P3-2, P3-3).
- **Lanes outside `decide()`:** the run compile and the ops bounds read the setup document's allows and raise on B23; `baseline_guard` still reads `policy_engine_v2_scopes`; the repo pen's background refusal and `no_such_path` (P3-2). `OPERATOR_LANES` stays, and overlay `network fetch` allows do not grant read_url (P3-3). Only a human's grant answers a role's `*` ask — not a template, phase, group or project allow (P3-2).
- **Grants:** no `subdomain`, `path_prefix` or `mcp_tool` matchers; the hook decides WebFetch by host; redirect hops are decided by host; `project` lifetime is not offered on Queen cards; legacy unbound session rows match until restart (P3-4). Check 1 probes fixed paths (P3-5).
- **Taint:** no per-server MCP trust flag; no taint into workers; worker-written observation memory carries none; a shell egress under `private` only is not gated (P4-1).
- **Cards:** no manifest for quick_edit / batch_dispatch / forge_tool; folding covers only read_url and search_web (P4-2).
- **Workers and egress:** loopback is closed but for a local provider's port and the project's `loopback_ports` (D6), except a bubblewrap `*` run, which shares the host's loopback; V6b (the worker never holds the provider key) is deferred until a relay exists (P2-1, decision #5); the proxy cards no ask host for a subprocess that the dispatch card did not list (P2-2).
- **Not verified this round:** P3-1's note that a delegator whose role has no rule holds `deny` (decision #7, `roles.within_delegator`).
