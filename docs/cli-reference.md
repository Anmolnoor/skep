# CLI Reference

Use `skep --help` and `skep <command> --help` for the exact parser output. This
reference summarizes the public command surface and the options most relevant to
the launch workflow.

## Global Options

```sh
skep [--home HOME] [--version] <command> ...
```

- `--home HOME` selects the Skep state directory. Defaults to `$SKEP_HOME`, else
  `~/.skep`. It goes before the command.
- `--version` prints the Skep package version and worker contract version.

## Setup and Health

```sh
skep onboard [--provider NAME] [--model MODEL] [--endpoint URL] [--api-key-env ENV]
             [--workdir DIR] [--sandbox auto|podman|none]
             [--template builder|operator|assistant] [--role ROLE] [--yes] [--no-chat]
skep onboard --check
skep onboard --quiet | --verbose
skep setup --personal [--provider NAME] [--model MODEL] [--endpoint URL] [--api-key-env ENV]
skep setup --template NAME [--dry-run] [--apply]
skep doctor
skep status [--personal] [--json]
```

- `onboard` is the guided first run (first-run O3): ① model (detects a local
  Ollama, makes a test call, prints where prompts go) → ② work directory
  (created if missing, registered as a project with its inferred
  `verify_command`) → ③ sandbox backend (`none` is a real answer: the baseline
  then asks on every write) → ④ policy template (default `assistant`, locked
  through `skep policy approve`'s path after a typed `yes`) → ⑤ role, recorded
  in `~/.skep/first_run.json`; then it attaches to (or starts) `skep serve` and
  opens `skep chat` unless `--no-chat`. Re-running resumes at the first
  incomplete step; a flag that differs from a finished step re-runs it.
  `--provider` takes `mock`, `ollama`, `openai-compatible` or a preset id from
  `skep provider presets`. `--yes` takes every default and skips the typed
  `yes`. See [onboarding.md](onboarding.md).
- `onboard --check` is `doctor` plus: baseline hash valid (a tampered one is
  named `baseline_tampered`), engine v2 scopes on, the chosen sandbox backend
  present, the work directory writable, the model reachable, all five steps
  done — then the egress-truth block, whose last line is `telemetry: none`.
- `onboard --quiet | --verbose` (first-run O4) turns the first-session
  `why:` line under every chat verdict card, and the per-turn tally, off or
  back on (`~/.skep/first_run.json` `dismissed`; REST:
  `POST /api/onboard/transparency {"verbose": bool}`).
  Exits 0 only when every check is ready.
- `setup --personal` creates or updates the personal profile.
- `setup --template NAME` applies a policy template (`builder`, `operator`,
  `assistant`). `--dry-run` prints the resolved policy table and writes
  nothing; `--apply` confirms switching an existing policy document.
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

- `--execution-mode workspace|sandbox` chooses host-visible workspace execution
  (no OS sandbox; skep's own worker only) or the host sandbox backend. An
  external engine always runs in `sandbox`.
- `--engine aider|builtin|claude_code|codex|pi` picks the coding engine for this
  run, over the project's pinned `coding_engine`.
- `--worker-cmd COMMAND` sets the worker adapter command for this run (default:
  `$SKEP_WORKER_CMD`, else skep's own worker). A project pinned to an external
  engine refuses it unless `--engine builtin` is passed.
- `--ref REF` bases the worktree on a git ref.
- `--template NAME` instantiates a saved workflow template.
- `--param KEY=VALUE` fills a template parameter; repeatable.
- `--no-template` skips learned-template auto-match for this run.
- `--minimal` starts with deny-all grants so the run learns through approvals.
  A worker that calls the LLM still reaches its configured provider.
- When multiple learned templates match in an interactive terminal, `run` shows
  a one-key picker; choose `no template` to start from the normal policy.
- `--network DOMAIN` allows one domain; repeatable; added to the project's
  hosts. Omitted, the policy's `default_network` applies: the builder and
  operator baselines allow their package-registry hosts, the assistant baseline
  and a bare home allow none. A worker that calls the LLM reaches its
  configured provider whatever this list says.
- `--env-allow ENV` passes one environment variable; repeatable.
- `--caste CASTE` selects the worker kind. The common launch path is `coding`.
- `--auto-approve` opts in to auto-applying a verified, re-verified,
  manifest-only fix.
- `--planning-protocol plan|react` chooses one upfront plan (the default) or the
  bounded act-observe loop; omitted, the project's `worker_protocol` applies.
- `--budget-wall-clock`, `--budget-max-iterations`, `--budget-max-actions` and
  `--budget-max-provider-calls` set this run's budget.
- `--resume-of TASK_ID` names the task this run supersedes.
- `--role NAME` gives the worker a role: its layer narrows the worker's grants
  (see Roles).
- `--delegated-by ACTOR@ROLE` dispatches as that delegator (repeat, outermost
  first); the worker holds no more than it does.
- `--answer-host HOST` answers, for this run only, a host the run would ask
  about on a home with no approved baseline; repeatable, and required off a
  terminal (see Roles).
- `--quiet` suppresses the live phase tail.

`--minimal` cannot be combined with `--template`, `--network`, or `--env-allow`.

## Agents

```sh
skep agents [--json]
```

Coding agents running on this machine right now (Claude Code, Codex, Cursor,
Gemini and ~18 more, matched by process name), each `working`, `idle` or
`blocked`, and whether skep governs it — running under a skep worker, with the
run's task id and the dispatching chat's role. `blocked` is read from output,
which skep only has for processes it started itself; other agents show
`working`/`idle` by CPU. A `blocked` agent skep does not govern is flagged as
the gap. Same view as `GET /api/agents` and the Queen's `list_agents`. The
detection patterns are data from herdr (Apache-2.0; see `NOTICE`).

## Review and Approval

```sh
skep review <task_id> [--approve | --deny | --allow-command | --reverify]
                      [--branch NAME] [--pr] [--pr-base BRANCH] [--note TEXT]
                      [--actor NAME] [--worker-cmd COMMAND]
```

- Plain `review` shows the run evidence, patch, approval state, and
  re-verification result.
- `--approve` applies a completed patch to a `skep/<task_id>` branch, or resumes
  a pending approval. A run with no re-verification record is refused.
- `--branch NAME` names the landing branch for `--approve` on a completed run;
  it must be a new branch, not the default branch.
- `--reverify` re-verifies (G10) a completed run that was never re-verified,
  with the project's pinned verify command.
- `--allow-command` approves and remembers a pending shell command, then resumes
  the run.
- `--deny` rejects the pending approval.
- `--actor NAME` records who made the decision; `--note TEXT` adds a note to it.
- `--pr` opens a GitHub pull request after applying a completed patch (needs
  `gh`); `--pr-base BRANCH` sets its base (default `main`).
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
skep schedule health
skep tick [--worker-cmd COMMAND] [--auto-approve]
```

- `schedule add` creates a recurring direct run or template-bound run.
- `--every` accepts intervals such as `30s`, `5m`, `2h`, or `1d`.
- `schedule add` also takes `--caste CASTE`, `--ref REF`, `--network DOMAIN`,
  `--env-allow ENV` and `--role NAME` (holds the schedule's runs to a role).
- `schedule health` reports how each schedule has been doing.
- `tick` dispatches all due schedules and is intended for cron or another
  scheduler. A running `skep serve` dispatches due schedules itself.

## Branches and Pull Requests

```sh
skep repo list
skep repo refresh REPO
skep repo push-baseline REPO [--base BRANCH]

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

- `repo list` shows every repo skep can reach: clones and workon directories.
- `repo refresh` fetches origin and fast-forwards the default branch. Run it
  before reasoning about how stale anything is.
- `repo push-baseline` creates the missing default branch on an empty origin.
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
skep serve [--host HOST] [--port PORT] [--worker-cmd COMMAND] [--auto-approve] [--no-readopt]
```

`serve` starts the local HTTP API and browser dashboard. It prints the URL and
access token on startup.

On startup, a run whose worker is still alive from a previous serve is
re-adopted: serve checks it is the same process (pid and start time),
restores its network proxy on the same port, and supervises it to the end
with the time it has left, sandboxed or not. A re-adopted (or recovered)
run lands only by a human approval, never by a project's auto-apply lane:
after a restart its chain of trust is reconstructed from the record, not
witnessed, so `skep review <task> --approve` lands it. `--no-readopt` (or
`SKEP_READOPT_WORKERS=0` in the environment or `serve.env`) kills such workers
instead, as older versions did; on Linux a sandboxed worker then dies with the
serve that started it. The variable also reaches runs that `skep run` and
`skep team` start: on Linux their sandboxed worker then dies with the CLI.
See ADR 0060.

## Chat

```sh
skep chat [--url URL] [--chat ID] [--continue] [--thinking] [--role NAME]
          [--oneshot MESSAGE] [--yes]
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
  scripting/cron face). With `--yes` it pre-confirms every card that turn
  proposes, once — never a policy card: allowlist and grant changes stay
  pending, named for `/approve`.
- A new interactive chat asks for its role once (Enter takes the default —
  `skep role use`, else the role `skep onboard` recorded); `--role NAME`
  skips the question. See "Roles" below.

Messages starting with `/` are the command deck — deterministic operator
commands the model never sees. The first 22 are the same in the web deck:

| Command | Purpose |
|---|---|
| `/help` | list the command deck |
| `/policy [repo]` | effective policy for a repo (default: last used) |
| `/repos` | registered repos |
| `/skills` | learned-skill candidates and admitted skills |
| `/runs [n]` | recent runs |
| `/approvals` | the pending approval queue |
| `/state <repo>` | a repo's git state: branches, HEAD, recent commits |
| `/setup <repo> [--pack X] [--phase Y]` | bind a repo to a trusted project (confirm card) |
| `/phase <project-id> <bootstrap\|build\|maintain>` | move a project's trust phase (confirm card) |
| `/unregister <project-id>` | unregister a project: skep forgets it, its folder stays (confirm card) |
| `/land <task-id> [branch]` | land a completed run's patch (confirm card) |
| `/approve <review-id\|card-id> [branch]` | approve a pending review (confirm card), or a pending card by its id |
| `/deny <review-id\|card-id>` | deny a pending review (confirm card), or a pending card by its id |
| `/workon <path> [--pack X] [--phase Y]` | make a local directory a first-class workspace: git baseline plus a trusted project (confirm card) |
| `/schedule <name> <repo> <every> <instructions…>` | create a recurring schedule the ticker dispatches; `every` takes 30m/6h/1d (confirm card) |
| `/role <builder\|operator\|assistant\|custom-role>` | switch this chat's role — prompt, voice and policy layer together (confirm card) |
| `/personality <concise\|technical\|friendly\|custom:text\|default>` | set this chat's reply style — style only, never policy (confirm card) |
| `/persona <text…\|default>` | set the profile-level identity every chat starts with — identity only, never policy (confirm card) |
| `/btw <question…>` | ask a side question without touching running work: a read-only turn, no cards, no mutations |
| `/steer <task-id> <text…>` | send a steering note into a running react run — input, never authority; acts at once, no card |
| `/resume <task-id>` | continue a crashed or timed-out run from its checkpoint, model-free (confirm card) |
| `/browser` | register the built-in Playwright browser under the browse scope (confirm card) |
| `/trust [private\|untrusted]` | trust this chat again: clear its taint bits (both by default); acts at once, never a model tool, never from a channel |

Four more exist only in the terminal:

| Command | Purpose |
|---|---|
| `/status` | re-print the startup banner: assistant readiness, approvals and cards waiting |
| `/model [name] [--scope chat\|default]` | show the effective model for this chat; with a name, propose a model change on a card |
| `/replay` | print the last messages of this chat |
| `/exit` | leave the REPL; a bare `exit` or `quit` also works |

A typed `/approve` or `/deny` in the terminal is the decision itself and is
not asked twice.

`skep trust CHAT_ID [--bit private|untrusted]` is the same verb outside chat.
A `/trust` that clears `untrusted` also covers the finish notice of the runs
this chat has in flight at that moment: when one of those runs finishes, its
notice is still shown as the worker's words but does not set `untrusted` again.
The receipt names the runs it covers.
It writes who cleared which bit, when and the message that had set it to
`audit/policy/taint-cleared.jsonl` and as a line in the chat. Rows already in
the chat stay wrapped as outside content; new outside content sets the bit
again.

## Skills

```sh
skep skill ...
```

The skill lifecycle promotes tested learned recipes into the same template
registry. Use `skep skill --help` for the full subcommand list.

## Projects and Policy

```sh
skep project preview REPO [--pack PACK] [--strategy STRATEGY] [--phase PHASE]
                     [--project-id ID] [--name NAME] [--no-seed-commands]
                     [--engine ENGINE] [--verify-command CMD] [--loopback-port PORT]
                     [--codex-chatgpt-login | --no-codex-chatgpt-login]
skep project setup   REPO [--pack PACK] [--strategy STRATEGY] [--phase PHASE]
                     [--project-id ID] [--name NAME]
                     [--no-seed-schedules] [--no-seed-commands]
                     [--engine ENGINE] [--verify-command CMD] [--group NAME]
                     [--loopback-port PORT]
                     [--codex-chatgpt-login | --no-codex-chatgpt-login]
                     [--unlocked | --no-unlocked]
skep project list
skep project show    PROJECT_ID
skep project set-phase PROJECT_ID PHASE
skep project remove PROJECT_ID     # unregister: skep forgets it, the folder is untouched
skep policy registry
skep policy rsop REPO
skep policy why  REPO SCOPE ACTION VALUE [--role NAME] [--delegated-by ACTOR@ROLE ...]
skep policy explain SUBJECT
skep policy approve [--template NAME | --file PATH] [--reapprove] [--yes] [--diff]
skep policy baseline [status|diff]
skep policy migrate --dry-run
skep policy decide SCOPE ACTION VALUE [--repo REPO] [--actor NAME] [--role NAME]
                   [--delegated-by ACTOR@ROLE ...] [--read-only]
skep policy hook [--actor NAME]
skep policy decisions [--actor GLOB] [--since ISO] [--limit N] [--team ID]
skep policy grants
skep policy revoke RULE_ID
```

`policy migrate --dry-run` replays the decision log under today's policy and
lists every verdict that flips; it writes nothing. `policy approve --diff`
does the same replay under the layer about to be locked, before locking.
`policy decide --read-only` marks an MCP tool, browse or email action as one
that reports itself read-only.

`policy grants` lists every learned grant (id, what it matches, lifetime,
what it is bound to) and `policy revoke` removes one by id — the same verbs
as `GET` / `DELETE /api/policy/rules` and the chat `revoke_policy_rule` card.

`project setup` binds a repo to a policy pack and infers a `verify_command`
from the repo's own entry point (v91); an explicit `--verify-command` always
wins. A repo that is not a project yet needs `--pack` or `--strategy` (for
example `--strategy trusted_local_dev`); without one setup is refused with
`project setup requires a pack or strategy`. `--no-seed-schedules` and
`--no-seed-commands` save the project without the pack's schedules or the
toolchain-seeded shell commands. `--codex-chatgpt-login` lets this project's
codex runs use your ChatGPT plan login when no `OPENAI_API_KEY` is set: an
access-token-only copy per run, never the refresh token (see
[codex-chatgpt-login.md](codex-chatgpt-login.md)); `--no-codex-chatgpt-login`
turns it off. `--unlocked` is the top rung of the trust ramp (ADR 0061): the
project's sandboxed workers stop asking about commands no rule names on
runs the serve dispatches (a CLI `skep run` still stops at every gate; a
host no rule names is still refused or asked, never auto-approved), dispatch
needs no card and a G10-confirmed patch lands on `skep/*`;
denies, named asks and the floor still hold. It needs a pinned verify
command, a usable sandbox and an approved baseline; `--no-unlocked` turns it
off. `set-phase` moves the trust-ramp defaults (build → maintain) and drops
`unlocked`. Run again on an existing project with no `--pack` or `--strategy`
(for example `skep project setup REPO --loopback-port 5432`), it keeps the
project's strategy and phase and changes only what the flags name;
`--loopback-port` lists every port, replacing the project's list.
`policy registry` lists every policy setting skep has (type, default, the
layers that may set it); `policy rsop` is the resultant policy for a repo —
each key with the layer that decided it and the layers it overrode, the
compiled scope rules with their source, the operator rules, the floor;
`policy why` answers whether one `SCOPE ACTION VALUE` would be allowed and
names the rule that decided it (ADR 0052, read-only). `policy decide` is
the same question asked by an outside agent and RECORDED in
`<home>/supervisor/audit/policy/decisions.jsonl`; `policy hook` is its
Claude Code PreToolUse face (payload on stdin, verdict as hook JSON — see
`docs/policy-hook.md`, ADR 0053 P3). `policy decisions` reads that ledger
back, newest first, with counts per verdict and per note source (`observed`,
`forwarded`); `--actor` is a glob, `--since` a time or bare-date floor
(also `GET /api/policy/decisions` and the chat `policy_decisions` tool). `policy explain` renders a policy template (`builder`, `operator`, `assistant`), the approved `baseline` or a `role:<name>` as a Markdown allow / ask / deny table grouped by scope, with its settings and the floor rows no layer lifts — the rule ids are the ones `why` prints (first-run O1, ADR 0056 D3; also `GET /api/policy/explain?subject=` and the chat `policy_explain` tool).
`policy approve` locks a template (or a `--file` layer) as the policy
baseline: it prints the explain table, takes a typed `yes` unless `--yes`,
and writes `baseline.json`, `baseline.lock` and a row in
`<home>/supervisor/audit/policy/baseline-approvals.jsonl`; a second approval
needs `--reapprove`, which shows the diff against the locked layer first
(also `POST /api/policy/baseline/approve` and the chat
`approve_policy_baseline` card). `policy baseline status` prints one line —
`baseline: TAMPERED since <time>` when the file no longer matches its lock —
and exits 1 when tampered; `policy baseline diff` prints what changed (also
`GET /api/policy/baseline`). `policy explain baseline` shows the locked
layer. See "The Policy Baseline" in `docs/configuration.md` (first-run O2,
ADR 0056 D1/D2).

## Roles

```sh
skep role list
skep role show NAME
skep role use  NAME [--chat ID]
```

A role (first-run R1, ADR 0055) is a record — `name`, `policy_layer`,
`system_prompt`, `default_castes`, `voice` — and a **policy context**:
`builder`, `operator` and `assistant` ship bound 1:1 to the templates of the
same name; custom roles live in `~/.skep/roles/<name>.json` and may bind to
any template; one that names none acts as `assistant` until it gets one
(ADR 0059 D14; `skep doctor` names it). The role's layer sits directly under the approved baseline and
only tightens it: the stricter of the two verdicts stands, a role with no rule
for an action has no opinion, and the floor is above every role. A chat runs
in one role — its prompt, its voice (when the chat has no `/personality`) and
the Queen's own tool lanes. `role list` marks the default new chats open in
with `*`; `role show` prints the record and its layer as the explain table;
`role use NAME` sets that default, `--chat ID` switches one chat (the chat's
`set_role` card and `/role` are the same verb). A chat made before every
chat recorded a role has none stored and runs as `assistant`; `skep role use
builder --chat ID` gives it another (`skep doctor` lists such chats). `skep policy why|decide
--role NAME` answers under a role: `decided_by` names `role:<name>` when the
role decided, `why` adds a `role` block naming the layer, and the ledger row
carries `role` (also `role` on `GET /api/policy/why`, `POST
/api/policy/decide` and the chat `policy_why` tool; `GET /api/roles` and
`role` on `POST /api/chats` behind the web picker).

Delegation is subset-only (first-run R2, ADR 0055 D4): `--delegated-by
ACTOR@ROLE` (repeat, outermost first — e.g. `--delegated-by queen@operator`)
holds the verdict to what that delegator itself holds — its own effective
verdict, under its role, bounded by the hops above it. Anything beyond it is
`exceeds_delegator` (`deny` where the delegator holds no grant, an ask where it
would itself have to ask) and the ledger row carries `delegation_chain`; `why`
adds a `delegation` block naming the chain and what the delegator holds. A
delegator whose role has no rule for the action holds nothing to pass on
(unset is not granted). The floor is above every chain. Also
`delegation_chain` on `POST /api/policy/decide`, `GET /api/policy/why` and the
chat `policy_why` tool. A Queen chat in a role dispatches as `queen@<role>`
(an operator-typed `/command` and REST or the CLI, with no chat, dispatch as the operator): the
worker's shell prefixes and egress hosts beyond what that role holds are
dropped at dispatch — the worker cards for them instead, so a human answers —
and each drop is a ledger row with `actor: dispatch`; a role denied workspace
writes cannot dispatch a worker at all. Chains are depth 1 at dispatch.

A worker can be dispatched with its own role (first-run R3, ADR 0055 D5):
`--role NAME [--delegated-by ACTOR@ROLE]` on `skep run`, `role` and
`delegation_chain` on `POST /api/runs`, `role` on the chat's `dispatch_run`
and per task in `batch_dispatch`. The worker's grants are its role ∩ its
delegator's — a grant either makes stricter is dropped the same way and
recorded under the worker's role. Every delegated or role-bearing dispatch is
one `source: delegated` row in the decisions ledger (`skep policy decisions`):
the delegator as `actor`, `worker@<role>` as `value`, `role`,
`delegation_chain` and `task_id`. The run's dispatch decision carries
`delegation` (`chain`, `role`, `team`), shown on the run cards. A worker never
dispatches: a `worker@…` hop refuses.

```sh
skep team --manager ROLE --task REPO INSTRUCTIONS [--task ...] [--role NAME]
          [--delegated-by ACTOR@ROLE] [--execution-mode workspace|sandbox]
          [--answer-host HOST] [--worker-cmd COMMAND]
```

A team run (first-run R3) is a batch of 1-3 runs with a manager as the
dispatcher: each member runs under `[<dispatcher>, manager@<ROLE>]` — held to
the manager, and the manager to whoever dispatched the team — with the
manager's role unless `--role` (or a task's `role`) says otherwise. This is the
one two-hop chain a dispatch accepts; anything deeper, and a manager anywhere
but innermost, refuses. The team is one ledger thread: the manager's
`delegated` row, every member's and every drop carry the team id, and `skep
policy decisions --team ID` reads it in order. `skep team` waits for its
members like `skep run`; the same verb is `POST /api/teams` and the chat's
`batch_dispatch` with `manager` (dispatched as `queen@<chat role>`). The
manager writes no briefs of its own — the Queen or the operator does.

On a home with no approved baseline (a fresh or onboarded one, where every
grant is an ask), a role run or team run asks first: a `skep run` with `--role` and
`skep team …` print the hosts the run would ask about and, on a terminal, ask
y/n; yes answers them for that run only (the same pre-answer a confirmed
dispatch card gives). Off a terminal they refuse unless `--answer-host HOST`
(repeatable) names the hosts to answer, each one a host the run asks about.
`POST /api/runs` with a role and `POST /api/teams` refuse with a 400 listing
the hosts unless the body's `answer_hosts` names them (a subset of what the
run asks).

## skep acp

```sh
skep acp govern [--repo PATH] [--actor NAME] -- AGENT_ARGV...
skep acp doctor -- AGENT_ARGV...
skep acp install-hint
```

`acp govern` is the process an ACP client (T3 Code, Zed, …) spawns in place
of the agent (ADR 0054). It relays every JSON-RPC line unchanged except the
agent's `session/request_permission`, which it answers from `policy decide`
— `allow` selects the agent's `allow_once` option, an explicit or floor deny
selects `reject_once`; `require_approval`, an unmatched action, or anything
skep cannot read is forwarded to the human behind the client. It never
selects an "always" option. Every decision, every tool call the agent ran
unasked (`source: observed`) and every answer the human gave
(`source: forwarded`) lands in `decisions.jsonl`. The session is decided
against `session/new`'s `cwd` unless `--repo` names the checkout;
`--actor` replaces the ledger actor (default `acp:<agent>`). It reads the
home directly — `skep serve` need not run; an unreadable store forwards
every ask with the reason. See what the governed agents did:
`skep policy decisions --actor 'acp:*'`.

At startup it prints one line to stderr, e.g.
`skep acp: governing claude-agent-acp (ACP v2) — rings: advisory — home …`.
**advisory** is the permission ask — decided only when the agent's own mode
chooses to ask. **hard** appears only on ACP v1 when the client advertised
`fs.writeTextFile` or `terminal`: the agent's `terminal/create` and
`fs/write_text_file` then pass only on `allow`; anything else is refused with
a JSON-RPC error, because that hop has no human to forward to.

`acp doctor` sends `initialize` and a dry `session/new` (never a prompt) and
prints the agent's name and version, the mapper preset it gets, the modes it
advertises with the default marked, the rings, and a WARNING when the
default mode never asks (`auto`, `agent`, `agent-full-access`,
`bypassPermissions`).

`acp install-hint` first names the locked baseline the governor answers from
(template, approval time, short sha256) — or says `run skep onboard first`
when none is approved — then prints what to paste into T3 Code's Settings →
Providers → Add provider → Show more → Local ACP command dialog, for
claude-agent-acp and codex-acp: the absolute path of `skep` (T3 launches it with no shell and no
PATH lookup) and one argument per row — `acp`, `govern`, `--`, `npx`,
`-y`, `@agentclientprotocol/<adapter>`. See
[`acp-governor.md`](acp-governor.md).

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
skep provider presets
skep provider add ID --preset PRESET [--model M] [--base-url URL] [--activate]
skep provider add ID --protocol P --base-url URL --model M [--api-key-env ENV]
                     [--cost-class C] [--order N] [--host H]... [--activate]
skep provider use ID
skep provider set-key ID [--clear]   # value from stdin, 0600 file
skep provider login ID --client-id CID [--device-url U --token-url U --scope S]
skep provider remove ID
```

`channel status` prints one honest line per messenger channel: config state,
secret presence, last delivery. `provider` reads the registered LLM providers
and their latest health probes. `presets` prints the built-in catalog (v108,
Hermes parity: OpenRouter, DeepSeek, GLM, Kimi, MiniMax, Copilot, Bedrock and
more) with each row's egress truth; `add --preset` fills protocol/endpoint/
model/key-env from it. `provider add/use/remove` (v108) are the
registry's write faces — the same verbs as `POST/DELETE /api/providers` and
the carded chat tools. `--api-key-env` names the env var holding the key;
key values never ride the command line, and `use` writes the profile through
to the saved assistant config so the Queen actually speaks it. `login` (v108)
runs the RFC 8628 device-code flow with the OPERATOR'S own `--client-id` —
skep ships no OAuth client id for any provider — and stores the access token
it returns exactly where `set-key` would. A profile on `chatgpt.com` (the
`openai-codex` preset, v118) uses your ChatGPT plan login (Codex) and takes
no key: `set-key` and `login` refuse it and point at `codex login`.

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
