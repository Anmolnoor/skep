# skep version history

This is the release-by-release landing record that used to live in the README.
The README stays focused on the current operator path; this file keeps the
historical implementation notes.

The public repository opens at **v1.0.1**, because that is what the code is:
`pyproject.toml` and `skep.__version__` both said 1.0.1 when the repo was
published. v1.0.0 was tagged only in the private development history, which
stays private (LAUNCH-1-L3).

## v0.1.0 (first public release)

The first public release is the launch baseline: sandboxed worker execution,
independent re-verification, patch approval, durable audit evidence, learned
approval templates, a Claude Code adapter, and a static launch page with a demo
GIF.

What works:

- macOS Seatbelt and Linux bubblewrap sandbox backends.
- Inline approvals with approve-once, approve-and-remember, deny, and skip.
- Learned template auto-match for similar later runs in the same repo.
- `skep review <task_id>` evidence review and `--approve` branch landing.
- `skep --version`, `skep doctor`, source install, package build, and release
  workflows.

Known launch limits:

- Source install is the documented path until the v0.1.0 package is published.
- Claude Code is the shipped real-agent adapter; Codex and Aider adapters
  were planned, not included. (Both shipped later, in v33 — Aider pinned to
  `--no-auto-commit` so it can never slip a commit past the approval gate.)
- Template narrowing, ledger UI, scheduled autonomous launch stories, and hosted
  service work are post-launch roadmap items.

## v1 (spine)

One worker, one lifecycle, proven end-to-end: create worktree -> mint contract
task -> spawn the worker with a strict env allowlist -> watch deadline +
heartbeats -> ingest verified result + event log -> durable audit record ->
teardown. Including the death path: hung workers are killed on heartbeat loss,
crashed workers get a supervisor-synthesized terminal event, and orphaned
worktrees are swept on startup and after every terminal.

- **The supervisor store supplies the brain-stem** (run records, approval queue,
  audit trail, and evidence indexing; see `docs/adr/0003`).
- **Skep's first-party coding worker supplies the hands** (invoked as a
  subprocess; it edits, runs, verifies, and never commits).
- **The internal worker contract is the nervous system**: JSON task in, NDJSON
  events out, verified result + evidence at the end.

## v2 (landing)

- **Physically enforced boundary (Q1).** Workers run under a macOS Seatbelt
  profile (`sandbox-exec`): outbound network and writes outside the worktree are
  physically denied, proven by escape tests and the full suite running the real
  worker sandboxed (`docs/adr/0005`). Scope, stated honestly: writes + network
  are enforced; reads are not confined (the G2 env allowlist already closes
  secret-env exposure); per-domain network allowlists need v3's proxy/container
  layer (Seatbelt filters by IP/port, not domain).
- **Re-verification (G10).** A `completed` claim is not trusted: the supervisor
  re-applies the patch to a clean worktree and re-runs the worker's own recorded
  verification command under the sandbox, comparing exit codes (`docs/adr/0006`).
  A worker that claims "passed" but whose patch fails is caught and flagged
  ("NOT CONFIRMED") in `status` and `review`.
- **Auto-approval rules (D3, mechanism).** Declarative rules can auto-apply a
  patch when conditions hold: verification + re-verification passed, no risk
  flags, diff in scope (`docs/adr/0007`). Built and tested in v2 but dormant by
  default (no rules -> the human loop is unchanged); active for real workloads
  in v3. Every auto-approval is audit-recorded `auto:<rule>`.
- **True suspend/resume (Q8).** `skep review <id> --approve` on a suspended
  (`pending_approval`) task resumes it: a fresh run carrying the granted verdict
  + `resume_of` that proceeds past the policy gate (`docs/adr/0008`), instead of
  v1's re-run-from-scratch. Zero schema change (both fields were reserved at
  v0.1).
- **Usage accounting (G8).** The worker meters provider calls + tokens into the
  reserved `result.usage`; the supervisor records it per task and shows it in
  `status` / `review` with an aggregate footer, so cost is answerable
  (`docs/adr/0009`). (`cost_usd` awaits a price map.)

## v3 (landing)

The nightly **dependency/audit bot** (use case U1) is the spine. Every v3 piece
is proven in one offline, deterministic acceptance demo (`make u1`):

- **Contract v0.2** (additive minor bump). `worker_kind` is now an open caste
  registry (D2) and `permissions.network` a per-task domain allowlist (D1, where
  `[]` means deny all). Every v0.1 envelope still parses; both consumers run
  `>=0.1,<0.3`.
- **Storage gate (G4).** SQLite-WAL, single writer (not Postgres): made
  concurrency-safe for parallel dispatch and stress-proven (`docs/adr/0010`).
- **Network allowlist, physically enforced (D1).** A per-task CONNECT-filtering
  proxy admits only allowlisted domains, and Seatbelt pins the worker's only
  egress to it: closing the v2 "Seatbelt can't filter DNS" gap on macOS without
  containers (`docs/adr/0011`). `skep run --network pypi.org`.
- **A second worker caste (D2): `audit`.** A deterministic, LLM-free contract
  worker that bumps flagged dependency pins and re-runs the suite: dispatched by
  caste, re-verified (G10) like any worker. `skep run --caste audit`.
- **Scheduling (`docs/adr/0012`).** `skep schedule add ... --every 1d` +
  `skep tick` (cron-driven; skep is not a daemon). Parallel dispatch runs a
  fleet of workers at once over the single-writer store.
- **GitHub PR + auto-approval active (D3, `docs/adr/0013`).** `skep
  tick --auto-approve` auto-lands a safe fix (verified, re-verified, no risk
  flags, manifest-only) on a branch, never a push to the default branch; a
  major-version bump is risk-flagged and filed for review. `review --approve
  --pr` opens the PR.
- **Containers (Q1-B / G3, `docs/adr/0014`).** A worker runs in a container with
  the same host proxy enforcing D1, proven live (`make container`, opt-in). The
  egress pin (iptables in the container netns) and a container spawner backend
  are honestly logged as the remaining subset; the v0.1.0 launch baseline now
  has Linux/macOS CI.

## v3.5 (landing)

**Workflow templates**: user-authored, parameterized task recipes (use case U2;
the human-authored precursor to v4's skill registry). A template names the
instructions and the v3 knobs (caste, repo, network, env, budget) with
`{{param}}` placeholders; instantiating it mints a completely normal task, so
this is zero contract change (`docs/adr/0015`).

```sh
skep template add dep-audit --caste audit \
  --instructions "Audit {{ project }} dependencies and bump known advisories." \
  --param project                       # or: skep template add --from audit.toml
skep template list | show dep-audit

skep run --template dep-audit ~/code/acme --param project=acme   # on demand
skep schedule add nightly ~/code/acme --template dep-audit \
  --param project=acme --every 1d                                # bind to a schedule
skep tick                                                        # "run the audit template"
```

A schedule bound to a template is a live reference: `tick` re-instantiates the
current template each time, then dispatches through the same spine (sandbox, D1,
G10, D3). Proven end-to-end, offline, in `make templates` (author once -> run on
demand and schedule; both G10-confirmed).

## v4 (landing)

**Learned skills**: generated task recipes with a draft -> tested -> approved
promotion pipeline (`docs/adr/0016`). v3.5 was the human-authored floor; v4 adds
the learning loop on top, with the honesty kept front and center: the "learning"
is a deterministic heuristic generalizer, not a trained model, and the real
substance is the governance: a test gate and a human-approval gate. A candidate
never self-promotes.

```sh
skep run ~/code/acme  "Audit acme dependencies and bump known advisories."  --caste audit
skep run ~/code/globex "Audit globex dependencies and bump known advisories." --caste audit
# ...skep noticed you keep doing the same shape of work:
skep skill propose                       # generalize successful, G10-confirmed runs -> a draft
skep skill list | show <name>            # the candidate + its evidence
skep skill test <name> ~/code/foo --param arg1=foo   # the G10 test gate: pass -> 'tested'
skep skill approve <name> --as dep-audit             # HUMAN gate -> joins the registry
skep run --template dep-audit ~/code/bar --param arg1=bar   # then run/schedule it like any template
```

- **Generation** (`skills.py`) clusters completed + G10-confirmed runs by their
  fixed knobs and instruction shape, and extracts the parts that vary into
  `{{argN}}` parameters. Deterministic and conservative: it names slots
  structurally (`arg1`, not `project`), refuses over-general or mixed clusters,
  and proposes nothing when in doubt. It is honest heuristic pattern-extraction,
  not ML.
- **Two gates with teeth.** `skill test` instantiates a draft and dispatches it
  through the same `run_task` spine; it is promoted to `tested` only if the run
  completes and the supervisor re-verifies it (G10). A failed test is
  auto-rejected (`auto:test-gate`, fail-closed). `skill approve` is the only path
  into the registry, and only a human walks it.
- **Unified registry.** An approved skill is inserted into the same v3.5
  `templates` library, tagged `provenance="learned"`, and is run/scheduled by
  the unchanged spine. Nothing downstream branches on provenance: it is a tag,
  not a fork. Candidates live in a separate table until approved, so an
  unapproved one is structurally unable to run.
- **Audit trail.** Every promotion and rejection is recorded in the existing
  approval queue with the actor (`operator` or `auto:test-gate`) and the evidence
  it passed on.

Proven end-to-end, offline, in `make skills`: from observed runs -> generate ->
test -> human-approve -> run and schedule like a template. A candidate that fails
its test, or is denied, never enters the registry. Zero contract change: a
learned skill is still just a template -> a normal task.

## v5 (landing)

**The face and the box**: a web UI served by the supervisor itself
(`docs/adr/0017`), and the whole thing packaged as one container image
(`docs/adr/0018`). A user who has never opened a terminal runs one command,
opens a browser, and operates the full loop: assign -> watch the events stream
live -> approve / deny / open a PR -> edit templates, learned skills, schedules,
policies, and the worker model. Zero contract change: the UI is an API layer
over the same core the CLI calls; both surfaces stay.

```sh
# After the first public version tag publishes an image:
docker run -d -p 8765:8765 -v skep-data:/data \
  -e ANTHROPIC_API_KEY ghcr.io/anmolnoor/skep        # or: docker compose up -d
docker logs <container> | grep "access token"        # paste it into the browser
open http://localhost:8765
```

- **`skep serve`**: a FastAPI daemon over the existing core: thin handlers
  (validate -> call the core -> JSON), `POST /api/runs` answering 202 + task id
  while the run continues on a background pool, SSE event streaming (live
  worktree tail during the run, audit trail after ingest), and an in-process
  scheduler ticker that replaces cron inside the container.
- **Same gates, new surface.** Approve over HTTP is the CLI's exact semantics: a
  completed run's patch lands on `skep/<task_id>` (Q5, patch-as-approval); a
  suspended run resumes past the gate with the granted verdict (Q8). G10
  re-verification and the D1 allowlist are enforced in the core beneath both.
- **Mutable, persisted settings**: the one structural change to existing code: a
  `settings` table + rebuild-and-swap of the still-frozen `SupervisorConfig`, so
  a policy or model edit applies to the next run and survives restarts.
- **Auth**: a first-boot token in the boot log gates every `/api/*` route; the
  browser authenticates SSE by cookie (`EventSource` cannot set headers).
- **One volume.** Everything lives under `SKEP_HOME=/data/skep`: store, audit
  evidence, cloned repos (`/api/repos` clones by URL; no host paths needed),
  model settings, and the token. The container is disposable; the volume is not.
- **The honest Linux posture**: the container is the isolation boundary; the D1
  proxy enforces the allowlist but the iptables egress pin is still future work,
  and mounting the docker socket (root-equivalent) is refused (`docs/adr/0018`).

Proven offline in `make serve`: boot -> token auth -> assign over HTTP ->
SSE-stream the run -> approve -> the fix is on a branch with main untouched ->
policy edit round-trips. `make image` builds the box from this Skep checkout; CI
boot-checks it and publishes to GHCR on version tags.

## v6 (landing)

**The voice**: chat with the Queen's own model, in the browser
(`docs/adr/0019`). A Chat workspace talks to any Ollama-API endpoint (Ollama
Cloud or a local daemon), and the model gets gated hands over the supervisor.
Zero contract change; the worker's provider (A6) is untouched.

- **Setup is the exact flow you'd want:** Settings -> paste base URL + API key
  -> Test & save connection -> the live model list appears -> pick the default.
  The key is the one deliberate G2 exception: a 0600 `llm-secret` file beside
  the serve token, never in SQLite, never in any GET response;
  `SKEP_LLM_API_KEY` overrides it.
- **Durable sessions.** Chats and every message persist in the store; replies
  stream token-by-token (SSE over fetch); a refresh replays the conversation.
  Chats title themselves; a per-chat model can override the default.
- **Reads run free, mutations confirm-carded.** The model may always look (runs,
  approvals, policy, templates, skills, schedules, repos). It can only propose
  `set_policy` / `approve_review` / `deny_review` / `dispatch_run`: each
  proposal is a card in the chat, nothing executes until you click Approve, and
  a confirmed action runs the same `actions.py` verbs, and lands in the same
  audit trail, as the buttons in the UI, as actor `chat-user`. The model never
  holds the trigger.

## v7 Stage A (landing)

**More local-model protocols**: the same chat workspace can now speak either
native Ollama or OpenAI-compatible chat APIs (LM Studio, vLLM, OpenRouter-style
servers) by selecting the protocol in Settings. `serve/llm.py` normalizes model
listing, SSE chat chunks, and streamed OpenAI tool-call argument deltas so the
existing confirm-card gate is unchanged. Proven with a fake OpenAI-compatible
server over localhost: config/test/model-list round-trip plus a streamed
`set_policy` tool call that pauses as the same confirmation card.

## v7 Stage B (landing)

**Notes & Tasks**: a lightweight workspace plus chat tools for local intent.
Notes and todo tasks live in the supervisor store, with REST routes and browser
CRUD. Chat can freely `add_note`, `add_task`, and `complete_task` because those
only change inert local state and are audit-recorded as `chat-user`;
`set_task_due`, `delete_note`, and `delete_task` remain confirmation-carded
because they schedule future behavior or destroy data (`docs/adr/0020`).

## v8 (landing)

**In-repo coding worker**: the default `coding` worker dispatches
`python -m skep.workers.coding` unless `--worker-cmd` or `SKEP_WORKER_CMD`
explicitly names another adapter. Relative `--home` paths are resolved at
startup so sandbox evidence paths are absolute.

- **Assistant-config bootstrap.** If no worker `profile.json` exists, the worker
  reads the saved assistant LLM config and 0600 `llm-secret` from `SKEP_HOME`.
  An explicit worker provider profile still wins.
- **Provider network default.** Coding runs dispatched through `serve` inherit
  the configured provider host in their network allowlist when the request omits
  `network`; an explicit `network: []` still means deny all.
- **Capability registry.** File writes, verification shell commands, git gates,
  and network fetches are explicit worker capabilities with event evidence.

## v19 (first-session friction fixes)

Twelve fixes derived from a live field test where one "add a README" task took
74 chat messages, 12 runs, and 6 manual approvals. Contract bumped to `0.2.1`
(additive/optional only — the claude adapter's `>=0.1,<0.3` range still holds).

- **F1 — Batch plan approval.** A tool plan is pre-flighted before step 0: every
  not-yet-run non-verify `shell.run` step that needs approval is collected into
  ONE `approval.requested` gate carrying the full command list, and one approval
  grants them all on the single resume. The field-test add-README chain collapses
  from 5 runs + 4 approvals to 2 runs + 1 approval.
- **F2 — Provider host always in the allowlist.** `resolve_run_policy` merges the
  configured LLM provider host into every coding run's `network` on all creation
  paths (chat, API, CLI, scheduler, resume), not only when `network` was unset.
- **F3 — No `git push` from the worker.** `git push`/`pull`/`fetch` are denied at
  the worker before any grant, removed from the git preset, rejected by the policy
  editor, and swept out of poisoned stores. Landing stays patch → approve →
  `skep/<task_id>` branch.
- **F4 — Safe "remember command".** Remembering persists the exact normalized
  command (drops a `git -C <path>` pair), refuses remote-git, runs the too-broad
  guard, and prefers the repo's bound project policy over the global setting.
- **F5 — Detached-HEAD worktree context.** The worker prompt states it runs in an
  isolated detached-HEAD worktree, and `git checkout`/`switch` are denied with a
  teaching message (the `git checkout -- <path>` restore form stays legal).
- **F6 — Exit-code verification.** A run passes on the verify command's exit code
  alone; a wrong `expected_stdout` guess is a note, never a failure.
- **F7 — One recovery replan.** A failed command or a denied step feeds the real
  error back to the model for one corrected plan instead of an instant death.
- **F8 — Superseded runs.** Approving a run supersedes it (new terminal state);
  the successor's worktree stays in the durable orphan-sweep keep-set, and
  superseded runs leave the pending counts and the default status listing.
- **F9 — Unified provider config.** UI provider setup writes through to
  `profile.json`, so `skep doctor` agrees with the daemon; the worker's profile
  path falls back to the daemon secret, and doctor prints an advisory when only
  the sqlite store is configured.
- **F10 — Visibility-aware polling.** The dashboard skips polling while hidden,
  backs off on failure, and the store checkpoints (truncates) the WAL on startup.
- **F11 — Deterministic allowlist.** The resolved `network` is sorted + deduped
  (byte-equal task.json for equal inputs), and the dispatch decision records the
  requested vs resolved lists as a reproducibility breadcrumb.
- **F12 — Remediation hints.** Known failure classes render a one-line "what to do
  next" in the run view and in chat.

## v9–v17 (the roadmap, landed 2026-07-08)

Nine versions implemented in one unattended run; the authoritative per-step
record is `plans/EXECUTION_LEDGER.md`. Headlines: contract v0.2/v0.3
(network allowlist, open castes, plugins), the autonomy scorecard (v12),
curated durable memory (v13), the provider registry + routing engine (v14),
nodes + governed local ops (v15), channel adapters (v16), MCP-as-capability
and deep research (v17). v18 (public release) was deliberately not executed;
its mechanics landed later via v27/v37.

## v20 (landing-funnel fixes, 2026-07-08)

Six fixes from the v19 re-test: landing branch validation + named landing
branches, supervisor-side baseline diffs, re-verification warnings surfaced
at landing, `reverification_summary` on every run view.

## v21 (the vestigial commit tail, 2026-07-08)

One fix: a worker commit tail is requested ONLY by contract intent
(`requested_actions=["git.commit"]`) — instruction keywords are inert, so
"…commit to branch X" no longer costs a second run and a second approval.

## v22 (deterministic baselines, 2026-07-08)

Runs pin their baseline to the repo's default branch (not the operator's
checkout); worker-side git-commit denial is a capability guard
(`is_worker_commit_command`); the Queen gets `repo_state` eyes.

## v23 (policy that actually applies, 2026-07-09)

Effective-policy resolution per repo (`/api/repos/{name}/effective-policy`),
managed repos root, trusted-dev registry hosts, the `auto_approve`
deprecation notice, `land_run` as a shared verb.

## v24 (scheduler blind spots, 2026-07-10 area, landed 2026-07-09)

Dispatch `ref` + append-landing semantics, scheduler `repo_slug` binding,
idempotent project setup (`seeds_skipped`). The maintain-phase integration
branch was deferred here and landed as v30.

## v25 (the command deck + local work, 2026-07-10)

Deterministic `/commands` in the chat composer (operator-sourced cards,
actor `operator-command`), `/workon` local-dir on-ramp (confirmed git
baseline + trusted project), and the drift pin that keeps the deck table and
its executor one surface.

## v26 (live channels, 2026-07-10)

`ChatEngine` extracted (one turn loop, many faces); Telegram long-polling
live; Slack signed webhooks live; channel identities allow-listed and
fail-closed; only low-risk actions channel-confirmable.

## v27 (public release mechanics, 2026-07-10)

`scripts/install.sh`, ADR index, release runbook, version reconciled to
1.0.1; the PyPI publish step wired but parked pending a trusted publisher
(operator action).

## v28 (Linux egress parity, 2026-07-10)

`netshim` + AF_UNIX proxy bridge under bubblewrap: per-domain egress is now
ENFORCEABLE on Linux, closing the long-standing v14-7 skip; the suite's last
allowed failure retired — fully green everywhere since.

## v29 (governed web reading, 2026-07-10)

`network.read` as a governed capability over the existing fetch gate;
HTML→text extraction (deliberately naive tag-strip, upgrade path noted).

## v30 (maintain-phase integration branch, 2026-07-10)

Maintain-phase auto-apply accumulates on ONE `skep/maintain` branch
(project policy `auto_apply_branch`, `skep/` namespace enforced);
`approvals.landing_branch` persisted so `applied_branch` is accurate on
every landing path. `main` never advances automatically.

## v31 (skill distribution, 2026-07-10)

Signed skill bundles (`skill export` / `skill import --approve` with a
human gate), canonical bytes + signature verification, REST preview/export
routes.

## v32 (gated ops execution, 2026-07-10)

`ops_executor` executes ops plans for real behind `ops run --approve`
(last-guard re-validation), closing v15's dry-run-only partial.

## v33 (more agent adapters, 2026-07-10)

`AdapterSpec` + `cli_adapter` extraction; Codex and Aider adapters shipped
(Aider pinned to `--no-auto-commit` so it can never slip a commit past the
gate); bonus first-party Ollama LLM-planning worker.

## v34 (worker PATH hygiene, 2026-07-10)

The supervisor's venv is stripped from worker PATH (system toolchains
resolve for real); LLM plan semantic validation moved to parse time.

## v35 (plan only)

"Chat as a narrated timeline" was designed but never implemented — see the
v39 audit; it remains a standing arc.

## v36 (plan only)

"Policy-First Skep" (unified policy schema, `decided_by`, MCP fail-closed
wiring, templates) was designed but never implemented — see the v39 audit;
its fail-closed kernel landed as v39-F1, the rest remains a standing arc.

## v37 (landing)

**Closing the product gap** (the 2026-07-10 comparison review's open items):

- **F1 — Publish rehearsal.** A manual-only TestPyPI lane in `release.yml`
  exercises the exact trusted-publisher OIDC path with zero public
  commitment; the package/GitHub-release job is now explicitly tags-only.
  `scripts/mirror-demo-repo.sh` mirrors `examples/skep-demo` (never pushes
  without `--push`). `docs/releases/README.md` gains the go/no-go runbook:
  everything below the operator's risk acceptance is a numbered command.
- **F2 — Docs curation.** `docs/README.md` is the curated index (a test pins
  that every doc is listed); the landing page links the docs; SECURITY.md and
  GitHub issue templates exist. No site generator — GitHub renders markdown.
- **F3 — Onboarding wizard.** `skep setup --personal` on a TTY with no
  provider flags asks for provider/model/endpoint and the API-key env var
  NAME (never the key). Flags, env vars, and non-TTY stdin behave exactly as
  before.
- **F4 — Discord, live.** A gateway websocket thread (new dep: `websockets`)
  beside the Telegram poller: messages run real Queen turns, confirm cards
  render as embeds, ✅/❌ reactions and embed buttons resolve through the
  same fail-closed channel gate as Slack buttons — shell/policy cards are
  never channel-confirmable. `live: true` for all three channels, and the
  posture docs say exactly what runs.
- **F5 — ADR 0021.** Interactive browser automation deferred with named
  revisit triggers; v29's governed `network.read` covers the read use case.

## v38 (landing)

**The terminal face** — `skep chat`, a REPL client of the serve daemon over
the same token-gated HTTP+SSE surface as the web UI (same gates, same
actors, same audit rows; the REPL never opens the store):

- **F1 — REPL core.** `ServeClient` + SSE parser + streaming turn renderer;
  `--chat`/`--continue` sessions; readline history and editing; daemon down
  is a teaching error, never a shadow dispatcher.
- **F2 — Cards inline.** Confirmation cards pause at the prompt:
  `[y] confirm  [n] deny  [s] skip`; skipped cards stay pending and
  resolvable in the web UI (one store, two surfaces).
- **F3 — The deck in the terminal.** The same 12 `/commands` as the web
  composer, tab-completed; mutations audit under `operator-command`; two
  drift pins keep the Python deck, its executor, and the web deck identical.
- **F4 — Run telemetry inline.** Confirmed dispatches auto-tail run events
  at the prompt; Ctrl-C stops watching, not the run; approval gates prompt
  right there and resumed successors are tailed too.
- **F5 — Banner + `--oneshot`.** Entry banner says what needs a human;
  `--oneshot` is the scripting face (stream one reply, exit 0, cards
  skipped and reported by id).

## v39 (landing)

**The reconciliation round** — closing the gaps a full plan audit (v1–v38)
surfaced the day v38 landed:

- **F1 — MCP fails closed.** An MCP tool name the risk heuristic cannot
  classify now requires approval instead of riding the read auto-allow;
  granting the `unknown` risk is an explicit policy act.
- **F2 — Stale claims healed.** Three modules stopped claiming the Linux
  per-domain egress gap that v28 closed; pinned so it cannot regress.
- **F3 — One contract range.** `SUPPORTED_CONTRACT_RANGE` is declared once,
  in the contract package; six importers, drift-pinned.
- **F4 — Routing wired.** `resolve_routed_provider` (built in v14, never
  called) now runs at dispatch: the decision is recorded on the run and an
  ollama-protocol profile configures the first-party worker via env —
  without ever widening the run's network grant.
- **F5 — History matches git.** v9–v36 landing entries backfilled (v35/v36
  marked plan-only), stale launch-doc claims corrected, and a pin keeps
  every future round in this file.

## v40 (landing)

**The standing arcs executed** — v35 (chat as a narrated timeline) and v36
(policy-first), the two plans the v39 audit found designed-but-unbuilt:

- **F1–F4 (v35).** The web chat reads like a document: consecutive tool
  calls fold into one deterministic activity row; supervisor tools summarize
  from result json; dispatched workers stream live INSIDE the chat
  (heartbeats, commands with failures and output tails, a read-only approval
  pointer, a diffstat pill) with refresh converging live and replay; prose
  styling and mobile output scrolling finish the reading.
- **F5–F6.** ADR 0022 and `policy_schema`: one unified scope-policy schema
  (coding/shell/filesystem/network/mcp), default deny, deny-wins-ties,
  `decided_by` on every decision, learned rules that can never promote into
  denied space (the worker-git floor sits above the schema).
- **F7–F9.** `resolve_run_policy` compiles the schema — the contract's
  `Permissions` is the compiled artifact (zero behavior change, corpus
  untouched); `decided_by` threads worker events → approvals → views
  (contract 0.3.1); the ops engine reads its bounds from the document's
  scopes.
- **F10.** MCP goes live, fail-closed: registered servers persist, the
  Queen can discover and call MCP tools, unknown tools card, explicit
  denies refuse without a card, allow-always writes a vetted learned rule.
- **F11–F13.** Four templates as data with golden resolved fixtures;
  `skep setup --template` previews, applies, and diffs switches (v19 replay
  pinned at one run / one approval); user-facing vocabulary becomes
  Policy / Scope / Gate / Template / Audit.

The v36 Stage F Hermes-replacement field test remains operator-owned and
pending — it is the release bar, whatever the suites say.

## v41 (landing)

**The last code seams** — the three items the remaining-work ledger named as
the whole code backlog; after this round what's left is Stage F and the
publish ritual, both operator-owned:

- **F1 — conversational scheduling.** `propose_schedule` joins the chat
  tools (always cards — the `set_policy` shape; the create mirrors
  `POST /api/schedules`, trust stays at tick time) and `/schedule` lands on
  both command decks (web + `skep chat`), so "check my repo every morning"
  said in chat becomes a confirmed, ticking schedule.
- **F2 — Telegram inline approvals.** The third transport of the v16
  posture: low-risk cards carry an inline Confirm/Deny keyboard when
  `channel_can_confirm` is on (default off — byte-identical to v40
  otherwise), resolved through the same shared fail-closed gate as Slack
  buttons and Discord components. A press is admitted only when BOTH the
  card's chat AND the pressing user are allow-listed (equal in the
  operator's DM; a group bystander fails closed). Shell/policy/patch cards
  never grow buttons anywhere.
- **F3 — email scope live (resolves ADR 0022 N1).** `email` gains
  `read`/`send`; a registered MCP server may bind `scope: email`, and its
  tools decide under that scope — read-shaped names flow as `email/read`,
  everything else (unknown included) cards as `email/send`. Hard denies
  refuse inline and survive confirmation; learned allow-always rules land
  in the server's scope, vetted against every deny. locked-down /
  personal-dev / homelab-ops gate email like they gate mcp; assistant keeps
  its risk-ladder character (reads flow, sends card). Binding a real mail
  server is the operator's half; the path is proven against the fake.

## v42 (landing)

**The researcher caste actually runs** — field test 2026-07-14: the first
real `start_research` was rejected, because v17 Step 5 shipped `run_research`
as a fetcher-injected library with no runnable contract worker and no caste
registration, so every research run fell back to the default coding worker
and was refused on arrival:

- **F1 — researcher contract worker + registration.** `workers/researcher.py`
  gains the standard `--headless` contract entrypoint (the audit/curator
  skeleton) around the existing `run_research`: sources are the allow-listed
  hosts, fetched via stdlib urllib + `html_to_text` (v29-F1) inside the
  sandboxed allowlist, artifacts are `.artifacts/report.{md,html}` +
  `sources.json` (never a patch — nothing lands), and verification is honest
  (zero fetched sources → `failed`, never a completed claim). `build_config`
  registers the caste; the deep-research template's instructions now describe
  the real mechanism instead of a tool the worker doesn't call. Deterministic
  and LLM-free — a research run needs no provider.
- **F2 — reports carry the fetched content.** The first real run completed
  but answered nothing: excerpt-only report lines, plus two sources 403-ing
  urllib's default User-Agent. Reports gain a `## Content` section with the
  readable page text (10k cap per source); fetches send a browser-like UA
  suffixed `skep-researcher` — the allowlist, not the UA string, is the
  security boundary.

## v43 (2026-07-14 → 2026-07-15)

**Research as a daily driver** — field test 2026-07-14, session 2 (plan:
`plans/v43/README.md`; F5/F6 landed same-day, F1–F4 completed 2026-07-15
after the v44 run):

- **F5 — note-kind schedules (operator request, landed first).** "Send me a
  joke every 30 seconds" had no home: every schedule was a repo-bound worker
  dispatch. `worker_kind="note"` is now a schedule kind the tick resolves by
  posting the instructions text as an inert note (actor `schedule:<name>`,
  state `note_posted`, a health success) — no repo, no worker, no policy
  surface. Chat `propose_schedule` and `POST /api/schedules` accept caste
  `note` with `repo` optional; worker schedules still require one. The text
  is static — a recurring reminder, not generated content.
- **F6 — note schedules deliver into the creating chat.** Operator, same
  session: reminders in a panel you don't watch aren't reminders. Schedules
  gain a nullable `chat_id`; a `propose_schedule` confirmed in a chat binds
  to it (threaded supervisor-side through `execute_mutation` — the model
  never names the target), and the tick posts the text as an `assistant`
  message there, falling back to the inert note when the chat is gone or
  the schedule was created outside chat (API/CLI).
- **F1 — report.html is dark-mode + tables natively.** `run_research` emits
  a self-contained styled document (inline dark CSS, sources as a real
  status/url/evidence table, per-source content sections); every escape pin
  holds and `report.md` is unchanged. The restyle-coding-run class dies.
- **F2 — reports delivered to `~/.skep/workspace/<slug>/`.** On completion
  the supervisor copies `report.md`/`report.html`/`sources.json` to a
  kebab-slugged, collision-proof directory and records it as a
  `workspace_delivery` artifact `get_run` quotes verbatim. The copy-run
  pattern (and its shell approvals) is gone.
- **F3 — the empty-file chain, reconstructed then closed.** The audit trail
  of `019f6222-298f` shows a `+placeholder` patch passing an existence-only
  `test -f` verify, then a copy run reading a baseline without the file
  (0 bytes). Fix at the worker's verification evaluation: a PASSED outcome
  is rejected when the run's ENTIRE output is empty files; the copy-run
  half died with F2. Full addendum in `plans/v43/README.md`.
- **F4 — heartbeat progress in the dispatching chat.** Ephemeral SSE status
  lines (`GET /api/chats/{id}/status`, every Nth heartbeat, default 2,
  0 disables, never a transcript row) plus one PERSISTED honest failure
  line — state and reason — pushed into the dispatching chat (and its
  messenger via v44-F2) the moment a chat-dispatched run dies.

## v44 (2026-07-15)

**Hermes parity — skep as the daily driver** (plan: `plans/v44/README.md`).
The operator retired the Hermes agent; the ten deployed-Hermes features skep
lacked, rebuilt at skep's posture. Landed fix-by-fix, gates green each step:

- **F1 — Discord routing parity.** `require_mention` (threads skep created
  and DMs exempt), `auto_thread` per routed mention (session rebinds to the
  thread), and a user-level `allowed_users` allowlist on top of the channel
  one — all fail-closed, decided in the pure v16 adapter.
- **F2 — outbound push.** `channels/outbound.py` + the scheduler's `notify`
  hook: scheduled/system messages bound to a messenger chat are pushed OUT
  over the existing REST sends. `schedules.once` + `start_at` make one-shot
  reminders honest.
- **F3 — inbound webhooks.** `POST /hooks/{name}` (outside the token gate;
  GitHub HMAC or constant-time shared secret), `{a.b.c}` templates, delivery
  as a notification into the bound chat — never a model turn. Operator-only
  management via `/api/webhooks` + a settings card.
- **F4 — script-kind schedules.** The `--no-agent` cron lane: `sh -c` on the
  supervisor host at tick time, output capped and delivered like a note
  tick; non-zero exit is an honest health failure. Creation is operator-
  gated (model proposals always card with the command verbatim).
- **F5 — discord_admin.** `discord_delete_message` / `discord_timeout_member`
  as ordinary confirm-carded tools, deliberately NOT channel-confirmable —
  a hijacked Discord account can't confirm its own moderation.
- **F6 — `skep skill import-md`.** Hermes SKILL.md packs become v31 registry
  skills through the SAME grant gate; shipped scripts grant NOTHING unless
  each is explicitly `--allow-script`ed.
- **F7 — podman sandbox backend.** Opt-in `sandbox_backend=podman`: skeleton
  rootfs overlay + host toolchain RO binds (bwrap semantics in container
  terms), `--network=none` deny-all, domain lists fail closed, loud fallback
  to the native backend — never toward no sandbox. Live smoke proves no
  route out on this host.
- **F8 — `search_web`.** Keyless Queen-side READ tool (DuckDuckGo HTML);
  discovered hosts only become an egress allowlist by riding a
  `start_research` confirm card — the proxy's exactness guarantee is
  untouched.
- **F9 — image input in chat.** Raw-bytes upload, magic-byte sniffed, 5 MiB
  cap; composer picker + paste; thumbnails in the transcript; images reach
  the model only when the new llm `vision` flag is on (honest text
  placeholder otherwise); Discord attachments ride the same path.
- **F10 — personalities.** Per-chat style preamble (three presets +
  `custom:`), APPENDED to the operative prompt and never overriding it;
  `/personality` on both decks, confirm-carded.

A concurrent session landed the chat-source UI arc in the same window
(`chats.source` column + backfill, faces stamping their source, the sidebar
grouping non-web chats) under its own v44-F1..F3 labels — same version,
different fix track; `plans/EXECUTION_LEDGER.md` disambiguates by commit.

## v45 (2026-07-15)

**Search parity with Hermes + a live smoke lane** (plan: `plans/v45/README.md`).
Comparing v44-F8 against the deployed Hermes search stack showed three gaps:
no snippets (the small Queen picked sources on titles alone), rate-limit
anomaly pages degrading to a silent `[]`, and one hand-rolled regex against
one endpoint. And nothing proved the feature surface against the real ollama
backend.

- **F1 — `search_web` speaks ddgs.** The `ddgs` package (Hermes' own free
  backend) replaces the regex scrape: `{title, url, host, snippet}` rows,
  multi-engine rotation, transport failures raise `WebSearchError` (the tool
  wrapper still degrades to a clean tool error), and a per-call worker
  thread enforces a hard 30s wall-clock cap — Hermes' #36776 lesson: the
  ddgs retry loop has no overall bound. Zero hits is a result; a transport
  failure is an error; never conflated. Posture unchanged: READ tool,
  discovered hosts still only become egress by riding a `start_research`
  card.
- **F2 — live feature smokes.** `tests/external/test_feature_smokes.py`
  (existing `external_app` opt-in): LLM probe, chat turn, read-tool lane,
  live search with snippets, card deny AND confirm + ticker note delivery,
  memory propose→approve→search, research card carrying discovered hosts,
  and `skep chat --oneshot` against a real serve subprocess. 8/8 green with
  the operator's key (glm-5.2:cloud), alongside the pre-existing whole-app
  coding E2E.

Field evidence (2026-07-15): a real research request through the terminal
face flowed search → snippet-rich source table → `start_research` card →
confirm → sandboxed researcher (allowlist blocked a stackademic→medium
redirect, as designed) → cited report delivered under `workspace/<slug>/`.
Field finding for v46: the researcher fetches allowlist *homepages*, not
the specific article URLs the search discovered — the card carries hosts
only; report quality pays for it.

## v46 (2026-07-15)

**Research reads the articles; the Queen never replies with nothing**
(plan: `plans/v46/README.md`). Both fixes close the v45 field findings.

- **F1 — seed URLs into research runs.** `start_research` gains optional
  `seed_urls`; they ride the deep-research template's new `Sources:` line
  and the researcher fetches THOSE URLs instead of each allowlist host's
  homepage (`parse_sources`, falling back to homepages for free-form
  dispatches and pre-v46 schedules). The allowlist stays the only egress
  boundary — an off-list seed is refused and recorded, pinned by test. The
  card now shows the exact reading list beside the egress hosts.
- **F2 — thinking-only turns surface the thinking.** glm sometimes streams
  a terse reply entirely into the thinking channel; both turn paths now
  fall back to the thinking text when content is empty and no tools were
  called, emitting it as a live content delta. The v45 live-smoke
  workaround is reverted — visible text is the pin again.

Field evidence (2026-07-15): a live research run's card carried 8 article
URLs; `sources.json` shows 6 fetched verbatim (reddit/x.com bot-block,
recorded as unreachable), and the report body carries the actual
free-threading HOWTO text instead of homepage boilerplate. Full
`external_app` lane 10/10 with the real key (Docker test N/A — podman).

## v47 (2026-07-15)

**Operator-verb completion under the same spine** (plan:
`plans/v47/README.md`). The operator's prioritized backlog — every new verb
a MUTATING carded chat tool through the existing confirm/audit flow, no
side channels; workers still cannot touch git remotes; the channel confirm
allow-list stays `{dispatch_run, scheduled_result_ack}` (pinned).

- **F1 — schedule CRUD honesty.** `delete_schedule` + `set_schedule_enabled`
  over the existing store verbs.
- **F2 — MCP unregister.** `unregister_mcp_server` + `remove_mcp_server`
  (learned scope rules kept, and the description says so).
- **F3 — open_pr.** actions.open_pr_for_run = land_run's pending-or-new
  landing + the (now shared) PR assembly; the route behavior is unchanged.
- **F4 — read_url.** Queen search ≠ Queen fetch: the card shows the exact
  URL and NOTHING is fetched until confirm; http(s) only, 64KiB/10k-char
  caps, html_to_text.
- **F5 — merge_pr.** github.merge_pull_request (gh, honest failure). The
  ONLY base-branch advance, web-UI-confirmed only.
- **F6 — digest schedules.** Caste 'digest' composes pending approvals /
  recent run states / schedule health / memory proposals at tick time,
  delivered like a note tick (chat + outbound push). Delivery tail for
  note/script/digest is one shared helper.
- **F7 — opt-in completion notify.** `notify_run_completion` setting
  (default OFF) + carded toggle; completed runs get the v43-F4 one-line +
  push treatment when on.
- **F8 — Discord typing.** Field report: replies "just appeared".
  _TypingPulse re-fires POST /channels/{id}/typing every ~8s around the
  gateway turn; injectable, swallowed failures, never blocks a reply.

Live evidence (2026-07-15, throwaway home + glm-5.2:cloud): digest schedule
proposed → confirmed → ticked → "skep digest" in the chat; delete_schedule
card emptied the schedule list; read_url card returned the actual
whatsnew/3.13 page text (10k chars) only after confirm.

## v48 (2026-07-16)

**Field-test hardening: worker LLM reliability + deck honesty** (plan:
`plans/v48/README.md`; source: `reports/field-test-2026-07-15.md`, a Hermes
agent end-to-end test of 15 surfaces — 13 passed, and the misses are these
fixes). The headline: every LLM coding-plan run was failing on ollama.com's
intermittent streaming 404, so skep's primary value proposition was down
while the whole supervisor spine kept working as designed.

- **F1 — provider stream retry.** ollama.com intermittently 404s streaming
  `POST /api/chat` requests that succeed on the next attempt; the worker's
  plan request rode the same no-retry `chat_stream` as the Queen. Both
  protocols now open the stream through `_open_stream_lines` — up to 3
  attempts on transient statuses ({404, 408, 429, 500, 502, 503, 504}),
  linear backoff. The status check precedes any yielded line, so a retry
  can never replay partial output.
- **F2 — setup rejects a pasted key.** `--api-key-env` with a literal API
  key corrupted profile.json (the worker skipped the llm-secret fallback
  and failed auth). `run_personal_setup` now requires an env-var-name shape
  and says the correction without echoing the secret.
- **F3 — deny resolves the run.** All four deny paths route through
  `store.resolve_approval`, which resolved the approval row but left the
  run in `pending_approval` forever — doctor kept flagging denied runs as
  stale. Deny now transitions a gated run to `rejected` (unless another
  approval is still pending); denying a landing review of a completed run
  still refuses only the landing.
- **F4 — path-bound repos over HTTP.** `{name:path}` on
  `/api/repos/{name}/state|effective-policy`: the ASGI server decodes %2F,
  so the single-segment converter could never match a /workon absolute
  path — the deck's /state and /policy 404ed for every workon workspace.
- **F5 — oneshot deck parity.** `--oneshot "/help"` went to the model;
  `/`-input now runs the same client-side deck as the REPL and web
  composer, with proposed cards left pending (EOF = skip, never act).

Not fixed on purpose: personality adherence (the style preamble injection
is correct; glm-5.2 following it weakly is a model property, revisit with
an A/B observation). v49 seeds from the report: a worker-path LLM health
check in doctor, and a worker dry-run mode.

## v49 (2026-07-16)

**The worker-path doctor + the last chat-surface gaps** (plan:
`plans/v49/README.md`; sources: the v48 ledger seeds and
`reports/real-user-simulation-2026-07-15.md`, an 11/12 simulation that
also field-confirmed v48 — its 7 rejected runs are F3 denies working).

- **F1 — doctor checks the WORKER provider path.** The hardcoded
  `available via supervisor` stub becomes a real check: resolve the
  provider exactly as a run would (`worker_provider_from_home`, incl. the
  api_key_env trap and llm-secret fallback) and probe the endpoint with
  the worker's own credentials. The dry-run seed is folded here (sandbox
  was already a doctor check; the audit caste exercises the no-LLM
  pipeline; the missing signal was only this credential path).
- **F2 — allow_shell_command chat tool.** "Add pytest to the allowlist"
  works from chat: union-of-one through the same guard as the presets,
  never a replace. Found while testing: a leading `sudo`/`doas` laundered
  every deny in the shared guard (`sudo git push` was allowlistable);
  privilege escalation is now rejected first, on every surface.
- **F3 — the confirm stream carries the card's result.** Both verdict
  streams open with the same `tool` event free-executing tools emit; API
  consumers no longer watch a continuation with no outcome.
- **F4 — memory classes discoverable from chat.** The
  list_memory_proposals description enumerates MEMORY_CLASSES itself and
  names the `skep memory propose` command.

## v50 (2026-07-16)

**The oneshot card cliff** (plan: `plans/v50/README.md`; source:
`reports/black-box-user-test-2026-07-15.md` — a pure chat user: reads are
excellent, but "why can't I just say yes right here?").

- **F1 — oneshot resolves cards at a TTY.** Oneshot sends through
  `ChatRepl.send`, the exact REPL resolution path with the house [y/n/s]
  prompt. Without a TTY, EOF reads as skip (never act) — cron and pipes
  keep the old contract. No `--yes` flag, deliberately: blanket
  pre-approval of an unseen card is a shadow permission system.
- **F2 — --continue / --chat compose with --oneshot.** Real continuity:
  oneshot resolves its chat like the REPL (--chat wins, then --continue,
  else a fresh chat). The flagless cron default is unchanged.
- **F3 — the pending-card hint is actionable.** The skipped-card message
  names the serve URL and the exact resume command instead of a URL-less
  "resolve it in the web UI".
- **F4 — ask on ambiguity.** One system-prompt sentence: with more than
  one plausible project match, ask which one before dispatching.

Recorded, not fixed: the Queen occasionally re-asking for context already
in the transcript is a glm-5.2 property; revisit on a model upgrade.

## v51 (2026-07-16)

**Closing the Hermes gap, governed** (plan: `plans/v51/README.md`; source:
the skep↔Hermes gap analysis + the 2026-07-16 REPL field-test seeds). The
Queen becomes a more capable agent by getting GOVERNED versions of each
Hermes capability — never raw tools.

- **F0 — /approve and /deny accept the pending-card id.** The field-test
  bug: the pending-card hint printed an id no deck command accepted.
- **F1 — search_chats.** FTS5 over the durable transcript (external-content
  table + triggers, one-time backfill); "what did we decide last week?" is
  now answerable.
- **F2 — Queen file reads (ADR 0023).** read_file + search_files behind
  the filesystem scope: operator roots read in the turn, any other path
  cards on the exact resolved path, an explicit deny never cards. Writes
  deferred until observed demand.
- **F3 — run_code via the script caste (ADR 0024, contract 0.3.2).**
  Inline code runs as a sandboxed worker: deny-all egress, file-not-string
  execution, output as the tool result, no path to a commit. Auto-runs
  exactly where dispatch_run would auto-dispatch.
- **F4 — skill management from chat.** view free (grants visible);
  create/patch/delete carded; chat skills carry zero grants by
  construction.
- **F5 — batch_dispatch (ADR 0025).** Up to 3 parallel runs, one card
  showing all, auto only when every member matches; each run is
  independently governed and audited.
- **F6 — the script-schedule watchdog.** Reality check recorded: v44-F4
  already shipped script schedules; the missing piece was silence on empty
  success (failures still post, named).
- **F7 — ask_clarifying_question.** A turn-ending prompt: the question is
  a normal assistant message plus web-UI choice buttons; the next message
  answers it.

Cut on purpose: browser automation — ADR 0021's trigger has not fired; a
gap analysis is not a failed inventoried action.

## v52 (2026-07-17)

**Operator-policy resolution: the Queen's standing policy** (plan:
`plans/v52/README.md`, reconciled from the v51-review draft; ADR 0026).
Before v52, "Queen-side" meant "no policy applies" for network tools.
The verified leak that shaped the design: the stored global document also
feeds ops-worker bounds, so Queen-only rules need a document workers
never read.

- **F1 — the operator-policy document.** `operator_policy_document` in
  settings; default allows exactly keyless web search (`net:search`).
  The `search` action joins the network scope (ddgs rotates engines — no
  domain pattern could honestly govern it; the v41-F3 email precedent).
- **F2 — resolve_operator_policy.** Composes the global document with
  the operator overlay via the native `resolve(base, overlays)`; deny
  wins ties across both; per-call load (the draft's cache holder was
  dropped — the house pattern is per-call policy consults).
- **F3 — the Queen's scoped tools go through it.** File reads keep their
  v51-F2 semantics exactly (empty overlay ≡ global document) and gain
  operator-document rules; `search_web` runs only on an allow, named in
  the result; `read_url` stays card-gated with audit-only domain
  attribution (Option A — the card is the human gate).
- **F4 — set_operator_policy.** Carded edits bounded to the scopes the
  Queen consults; allows cannot reach into composed deny space; denies
  that would strand a learned rule fail a dry-run composition at write
  time.
- **F5 — decided_by everywhere.** Every scoped Queen tool result names
  the rule that admitted it; the transcript is the audit record.

## v53 (2026-07-17)

**The Queen learns: skills from conversation, memory in chat, identity,
voice, and cron chaining** (plan: `plans/v53/README.md` — review-corrected
before implementation; sources: the 9-item Hermes-vs-skep comparison and
the operator's priorities; ADRs 0027–0031). Six review corrections were
applied to the draft plan first: the curator surfaces instead of
archiving, the observer is opt-in and heuristic-first, the persona is
capped and bridge-labeled, the per-schedule model field was cut (YAGNI),
the memory label reuses the context-NOT-authority phrasing, and the voice
privacy claims were rewritten (edge-tts is Microsoft CLOUD, not local;
Chrome STT is Google-cloud-backed).

- **F2 — memory in every chat turn (ADR 0027).** Approved global
  memory_items ride the system prompt: class-prioritized, ~2k-token cap,
  context-NOT-authority label. The Queen is the same person across chats.
- **F4 — persona.md (ADR 0028).** One capped identity file leads the
  prompt, always followed by the rules-win bridge line; carded
  set_persona + /persona in both decks.
- **F3 — session browse.** list_chats, get_chat_messages (paginated,
  truncated), chat_id-scoped search_chats.
- **F7 — the skill index (ADR 0027).** Registry templates, names + one
  line each, capped; full recipes load on demand via view_skill.
- **F1 — conversation skills (ADR 0029).** Opt-in heuristic observer on
  the ticker proposes DRAFTS from multi-tool turns; skill approve admits
  conversation drafts without the (meaningless) worker test; the curator
  surfaces stale drafts in the digest and never acts.
- **F5 — cron context chaining (ADR 0030).** Schedule B reads A's last
  output as labeled context (stdin for scripts); acyclic, depth ≤ 3,
  validated at creation; synchronous castes source, task castes consume.
- **F6 — voice (ADR 0031).** Web-first mic + spoken replies (honest
  cloud tooltips); config-gated server TTS (default none; piper local,
  edge/openai labeled cloud) with Discord voice-message delivery;
  messenger STT deferred with a named trigger.

## v54 (2026-07-17)

**Confirmation cards grow up: auto-deny, verdicts, plain English — and
one PR per topic** (plan: `plans/v54/README.md` — review-corrected
before implementation; source: the 2026-07-17 field-test screenshots
and the operator's words; ADRs 0032–0034). Plan corrections first: ADRs
renumbered to 0032–0034, duplicated sections removed, stale anchors
re-pinned, F1's settings surface completed, F3's channel change cut
(wrong renderer), F4 re-scoped onto the v24-F1 append mechanism.

- **F1 — card auto-deny on timeout (ADR 0032).** A proposed card older
  than `card_timeout_seconds` (default 300; 0 disables; full policy
  surface) is DENIED by a ticker sweep — never confirmed: the model
  never holds the trigger. Transcript + channel push like a manual
  deny; a manual verdict racing the sweep always wins.
- **F3 — human-readable cards (ADR 0033).** Cards show the tool's
  plain-English spec description and labeled key-value args instead of
  a raw name + raw JSON, on the live event, the replay, and the
  command deck alike. Derived, never stored.
- **F2 — verdicts instead of dimmed buttons.** Resolved cards hide the
  button row and show ✓ Approved / ✗ Denied (✓ Confirmed / ✗ Canceled);
  a failed verdict call leaves the card pending with buttons back.
- **F4 — multi-run PR grouping (ADR 0034).** `open_pr` with `task_ids`
  + `title` lands related same-repo runs as commits on one
  `skep/<slug>` branch and opens ONE PR carrying every run's evidence.
  Presentation, not governance; the system prompt teaches the Queen
  when to group.

## v55 (2026-07-18)

**The stale clone: repo freshness, boundary teaching, policy copy — and
the chat stops going blank** (plan: `plans/v55/README.md`; source: the
2026-07-17/18 field test — branches pushed to GitHub after registration
were invisible, and the Queen recommended two impossible fixes
(allowlist `git fetch`, a schedule-script sandbox escape) while missing
`register_repo`; ADRs 0035–0036).

- **F1 — refresh_repo (ADR 0035).** The supervisor finally mans the
  `remote_git_managed_by_supervisor` station: `git fetch --prune` +
  fast-forward of the default branch on a managed clone — mirroring
  upstream, never landing work. Carded chat tool + HTTP route, honest
  report (updated refs, behind counts, or why it refused).
- **F2 — dispatch auto-fetch + origin/&lt;ref&gt; fallback (ADR 0035).**
  Runs against registered repos fetch first (offline-tolerant, managed
  clones only), and a branch that exists only as `origin/<name>` is now
  dispatchable — `worktree add --detach` does no remote DWIM, the
  hidden second half of the field failure.
- **F3 — boundary teaching.** allow_shell_command names every forbidden
  git verb (fetch/pull were missing — the exact hole); register_repo is
  THE clone path; the system prompt carries the operator's checklist:
  registered? → on latest? → dispatch with ref.
- **F4 — copy_project_policy (ADR 0036).** "Govern this project like
  that one" in one card: copies only the policy overlay, keeps the
  target's identity, phase, pack, and bindings.
- **F5 — repo_state sees the remote.** remote_branches, last_fetched,
  behind_origin — "is it on the latest code?" is one read tool away;
  the unknown-ref error teaches refresh_repo.
- **F6 — policy preflight.** Before dispatching, the Queen compares the
  task's needs with effective_policy and says "not possible under the
  current policy" + proposes the fix card — never dispatches into a
  known gate.
- **F7 — the chat narrates its silent gaps.** A pulsing `.chat-working`
  line ("Ran repo_state — thinking…") fills the blank stretch between a
  tool result and the next model token; client-side only.

## v56 (2026-07-18)

**Per-chat context that fits, approvals that arrive** (plan:
`plans/v56/README.md`; source: operator asks after v55 — "fix the
context on per chat", "sometimes approval don't hit the chat; stale
somewhere" — grounded in two code audits; ADRs 0037–0038). The audits
found the Queen resending the entire transcript every round into a
window ollama silently truncated (no `num_ctx`, ~14k-token fixed floor,
fictional meter), and gated runs notifying nobody.

- **F1 — the window is explicit (ADR 0037).** `llm_num_ctx` (default
  16384) rides every ollama call as `options.num_ctx`; openai-compat
  servers manage their own.
- **F2 — bounded replay + rolling compaction (ADR 0037).** The store
  keeps the full transcript forever; the model gets a budgeted slice —
  prior-turn tool results capped with an honest marker, overflow folded
  into a deterministic per-chat digest riding the system prompt.
- **F3 — the meter tells the truth.** Chat detail carries the context
  the NEXT turn will send (same math as the replay); the composer
  renders it and stops guessing.
- **F4 — chats bind to their project.** Dispatch/workon stamp
  `chats.project_id`; that project's scoped memory finally rides the
  prompt beside the global items.
- **F5 — gated runs announce themselves (ADR 0038).** `pending_approval`
  lands one transcript line + channel push naming the reason and
  `/approve <review_id>`; `get_run` guidance says WAITING ON THE
  OPERATOR instead of leaving the approvals array to be noticed.
- **F6 — the badge poll refreshes what it counts (ADR 0038).** The
  approvals list, Home panel, and card-locked composers re-render
  within one 5s poll cycle of the truth changing — including cards
  resolved in another tab or by the auto-deny sweep.
- **F7 — the status stream stops missing fast transitions (ADR 0038).**
  The tracked set re-derives every iteration with a grace window; runs
  gating faster than the subscribe still report, and the v53-era
  approvals-test flake dies at the root.

## v57 (2026-07-18)

**The complete git surface: branches, PRs, worktrees** (plan:
`plans/v57/README.md`; source: the operator's ask — "create all the
tools regarding the git / pr / merge and all we gonna need to work
with git and worktrees"; ADR 0039). Rulebook: reads free, mutations
carded, remote ops on operator credentials only, no force flags
anywhere, workers untouched.

- **F1 git_log / F2 git_diff** — history of any ref and a capped,
  honestly-truncated diff (default `<default>...HEAD`) so a landing
  branch is reviewable from chat.
- **F3 list_worktrees** — git worktrees joined with run states: "what
  is skep physically working on right now."
- **F4 list_prs** — the PR queue via gh on operator credentials,
  honest failure without gh/auth.
- **F5 create_branch / F6 delete_branch / F7 push_branch** — the
  carded branch lifecycle: create refuses existing names, delete
  refuses the default branch and unmerged work (safe form only),
  push refuses the default branch and never forces.
- **F8 unregister_repo** — chat parity with the HTTP delete, plus the
  in-flight-run guard both now share.

## v58 (2026-07-18)

**close_pr — the un-merge verb** (source: the operator's ask — "create
the delete branch and delete pr"; delete_branch already landed in
v57-F6, and the v57 ledger had deferred close_pr until the first field
test that wanted it).

- **F1 close_pr** — `gh pr close` on operator credentials, carded like
  every mutation. Reversible by design: the branch and commits stay
  (optional `delete_branch=true` sweeps the branch via gh), a closed PR
  reopens on GitHub — so it rides the standard card, same tier as
  delete_branch. No ADR: no new rule, just the existing card law.
- **F2 preflight waits for the verdict** — the checklist's policy
  preflight now ends "propose the fix BEFORE any run, then wait for
  that card's verdict"; the day's field data showed a clean
  auto-dispatch stalling on an unannounced mid-run shell gate.
- **F3 big asks decompose** — each dispatch_run carries ONE step a
  worker can finish and verify alone; dispatch → get_run → next.
- **F4 provider retry** — `chat_stream_with_retry`: transient failures
  (connection lost, timeout, 5xx/429) get 3 attempts; 4xx fails fast;
  a stream that already yielded never restarts (half-replies are kept,
  the error stays honest). Every engine provider call rides the seam —
  a source pin enforces it.
- **F5 anti-confabulation** — state reports (repos, runs, approvals,
  schedules, history) require a tool result from the conversation;
  empty results are reported as "nothing found"; identifiers are never
  invented. Field case: a seven-run history confabulated for an
  unregistered repo.
- **F6 the question always rides** — current-turn tool results cap at
  8000 chars (honest marker); the newest user message is pinned into
  the replay unconditionally. Field case: a ~50KB list_runs result
  evicted the question and the model answered nonsense.
- **F7 repo_state 404s for ghosts** — a name resolving to a
  nonexistent path gets a 404 with the register_repo/workon teach
  instead of empty repo state.

## v59 (2026-07-18)

**First-boot field test: lost landings, silent failures, weak-model
workers** (source: fresh-machine field test — three verified docs
patches finished invisible and unlanded, four runs died on provider
flakes, the Queen doom-looped over identical reads and invented paths;
plan `plans/v59/README.md` carries the full reconstruction).

- **F1 run list shows landing state** — `applied_branch` +
  `unlanded_patch` on every listed run, plus list-level guidance naming
  land_run when finished work is not on any branch yet.
- **F2 unlanded patch notifies** — a completed run whose patch has not
  landed always sends one line (+ outbound push) naming land_run and
  the file count; patchless completions stay opt-in.
- **F3 failure reasons travel** — the failed transition row carries the
  envelope's verification details, and the chat notification falls
  back to them; "no detail recorded" no longer hides a real error.
- **F4 worker transport retries** — a dropped/reset provider connection
  retries up to twice with backoff; every attempted call counts in
  provider_calls.
- **F5 plan repair + verify injection** — three repair rounds for
  invalid plan JSON (repair prompt carries a minimal valid example); a
  file-writing plan that forgot verification gets a default read-only
  listing injected instead of failing (G10 re-verification still
  governs; resumed checkpoints refuse synthesized steps).
- **F6 provider health probes run** — the serve ticker probes provider
  health on an interval (the v14 checks finally have a caller);
  routing distinguishes "never probed" from "probed and failing".
- **F7 unchanged-repeat breaker** — an identical read call returning an
  identical result gets a nudge; two ignored nudges force a text
  answer. Changed results never nudge (polling stays legitimate).
- **F8 task-id prefix resolution** — require_run resolves the chat's
  own truncated id rendering (unique >=8-char prefix); ambiguous
  prefixes 409 naming candidates.
- **F9 no cards for ghost paths** — a nonexistent path outside the
  operator roots fails fast with no confirmation card; deny payloads
  carry the specifics so the model corrects course.
- **F10 restart recovery** — serve startup reaps runs stranded by a
  supervisor death: valid late-deposited envelopes ingest through the
  standard path with G10 re-verification, the rest become an honest
  worker_crashed(supervisor_restart); the dispatching chat hears
  either way.

## v60 (2026-07-18, F1)

**The confirm-stream flicker** (source: same-evening field test — an
approved card's continuation streamed while the v56-F6 badge poll saw
"no proposed cards" and re-rendered the whole view; the model's
follow-up card landed in detached DOM and auto-denied unseen).

- **F1 poll defers to the live stream** — a module-level
  `chatStreamActive` flag, set for the duration of every chat stream
  (message or verdict) and cleared in its finally; the poll's
  card-unlock branch skips while it is up. The stream already
  reconciles the composer itself. Cards resolved from a second tab,
  the deck, or the auto-deny sweep still refresh the view — idle-only.
- **F2 remote-base precheck** (2026-07-19) — open_pr probes
  `git ls-remote --heads origin <base>` before any side effect; an
  empty GitHub repo gets a one-line teach (push main once yourself —
  skep never pushes a default branch) instead of a pushed branch
  followed by GraphQL soup.
- **F3 patch-less group members skip with a note** (2026-07-19) — a
  grouped open_pr lands every member with a patch and names the rest
  in `skipped_no_patch` instead of failing the whole approved card;
  an all-patch-less group fails at once naming every member.

## v61 (2026-07-19, F1)

**The unroutable auto-dispatch** (source: store forensics on the
2026-07-18 docs field test — the three silent patches v59-F2 was
built for were auto-dispatched, so no chat_actions row existed and
chat_for_task could not route their notifications; the fix as
shipped would still have been silent for its own motivating case).

- **F1 auto-allowed mutations record their action row, born
  resolved** — record_resolved_chat_action inserts the row already
  confirmed (resolved_at set, decided_by naming the allowing rule);
  the chat stream's auto-allow branch calls it with the execution
  payload. chat_for_task now routes auto-dispatched runs, so the
  v59-F2 unlanded-patch call-to-action, the v59-F3 failure lines,
  and the v56-F7 status stream reach the dispatching chat. No
  transient proposed state: the auto-deny sweep and the badge poll
  never see a phantom card, and the transcript replay renders only
  proposed rows as cards.

## v62 (2026-07-19, F1–F3)

**The silent turn** (source: same-morning field test — three prompts
answered with "Let me pull the data…" + tools and then nothing; the
v59-F7 off-ramp fired but its terminal branches persisted no message,
and the only error signal was a transient toast).

- **F1 no turn ends silently** — a provider drop with nothing
  collected persists "the provider dropped before any reply arrived —
  the tool results above stand"; the forced-summary pass keeps its
  text when the model attempts another tool call (previously
  discarded) and persists a cap line when nothing arrived at all.
  Confirm continuations inherit via turn_events.
- **F2 the forced-final pass carries an instruction** — one trailing
  system message: summarize in one or two short lines, name each
  run's state (done / failed / still pending), no promises.
- **F3 inline think tags are stripped at aggregation** — <think>
  spans (including stray closers and unclosed openers) move to the
  thinking channel instead of masquerading as the reply.

## v63 (2026-07-19→20, F1–F4)

**The second smoke test** (source: two field tests 2026-07-19 on two
stores — the Linux taskmate build whose verify hit the sandbox wall,
and a macOS end-to-end landing that exposed the oneshot approval
regress and the sweep's "timed out" lie; the 2026-07-18 docs-run
deaths re-anchored from the audit trail).

- **/approve finds its card in any chat** — the pending-card id
  resolves cross-chat (flagless oneshot mints a fresh chat, so the
  hint's id was never in "this" one), and an operator-typed
  /approve|/deny with an explicit id auto-confirms its own command
  card instead of minting a card no EOF stdin can answer. Scripted
  approval completes; unnamed cards still skip on EOF.
- **Resolution elsewhere reconciles the cards** — resolve_approval
  supersedes the proposed cards asking the question it just answered,
  with an honest "resolved elsewhere: approved by …, applied on …"
  tool row; the auto-deny sweep records superseded, never
  "timed out", when the underlying review or run already fell.
- **A string verify.argv verifies** — shlex-split like shell.run's
  command string; unusable strings on file-writing plans fall to the
  v59-F5 default listing, and G10 re-verification still governs what
  "verified" means.
- **The planner prompt states the sandbox walls** — workspace-only
  writes, HOME not writable, allowlisted network, verify within
  them; permission-shaped verify failures feed the same teach into
  the v19-F7 recovery replan.

## v64 (2026-07-20, F1–F4)

**The verify step is the weak link** (source: second smoke test
2026-07-20 — two runs wrote correct patches and died on their own
verify commands: pytest in a pytest-less sandbox, a `-c` one-liner
that cannot parse; the Queen burned four approval rounds granting
verify commands that never gate).

- **A failed verify earns the one recovery replan** — same
  _PlanRecoverable as a failed work step, both plan paths; the model
  may fix the verify command or the code; a second failure is
  terminal; G10 still governs "verified".
- **The Queen learns verify commands never gate** — the
  allow_shell_command description and effective_policy both teach the
  shell_verify fast-path; the description's example is no longer the
  one command that never needs it.
- **The too-broad rejection teaches the acceptable shape** — narrow
  with arguments; bare interpreters and -c/-lc forms can never be
  allowlisted; verify commands never need the allowlist.
- **The prompt states the toolchain** — only the system toolchain
  exists in the sandbox; verify with the stdlib, prefer a script file
  over a -c one-liner; missing-module stderr aims the recovery replan
  at the toolchain.

## v65 (2026-07-20, F1–F2)

**Re-verify tells the truth about patch-less runs** (source: operator
field test — a no-change audit run wore the lying-worker shape:
"unavailable / not confirmed / no patch artifact", plus a "re-ran"
that never ran; 58 of 94 reverification rows ever recorded were this).

- **not_applicable joins the reverify vocabulary** — a run that
  claimed no changed files has nothing to re-verify (benign); claimed
  changes WITHOUT a patch artifact gets a louder detail; an absent
  result envelope keeps today's shape. confirmed stays 0 everywhere.
- **The surfaces stop crying wolf** — "re-ran" only when exit codes
  exist; the approve-surface warning quotes what actually happened
  instead of pointing at a ghost patch; DO-NOT-TRUST keeps its teeth
  for every genuinely unconfirmed state.

## v66 (2026-07-20, F1–F3)

**Approvals reach Discord** (source: operator report — a Discord
thread locked behind web-UI round-trips for every read_url card).

- **The confirm gate admits skep's own threads** — a session binding
  to the card's own chat is the admission auto_thread ids can never
  get from the static allowlist; foreign bindings and strangers still
  fail closed.
- **read_url and start_research become channel-confirmable** — the
  v16 line holds: shell, policy, landing, git stay web-UI-only.
- **The web-UI-only card carries its link** — the Discord embed and
  the pending-card lock message both name the web UI URL; the lock
  message offers the buttons too.

## v67 (2026-07-20, F1–F7)

**The backlog lands, round one** (source: docs/invariants.md Part II —
the refinement backlog; this round is every prompt-, deck-, or
message-shaped item, with the architecture-scale items staged for
their own designs).

- **SKEP.md repo briefing** — a repo-authored briefing rides every
  worker planning prompt ahead of the snapshot, authoritative for how
  to verify in that repo.
- **The ask-list** — multi-ask prompts become a numbered list the
  Queen tracks to done/blocked; a dropped ask is visible, not silent.
- **/btw** — a read-only side question in both decks: no cards, no
  mutations, runs beside a pending confirmation.
- **Denies that teach** — path, network, and plugin denies name the
  acceptable shape instead of a bare no.
- **Descriptions with when-NOT** — the six high-traffic card classes
  teach when not to reach for them and their one known trap.
- **Acceptance-check-first** — dispatches state 'verify by …' up
  front, in the checklist, the tool description, and the seeded
  maintenance templates.
- **Repair boundaries pinned** — malformed model output feeds back at
  both boundaries; nothing hard-fails on the first bad shape.

## v68 (2026-07-20, F1–F2)

**The hollow pass** (source: the SKEP.md briefing A/B smoke test — the
briefing demonstrably fixed verify choice; its first attempt exposed a
run that read two files, wrote nothing, and passed).

- **A hollow plan can never pass** — a non-empty all-reads tool plan
  repairs or fails honestly; completed+passed with changed_files=[]
  on a write task is no longer a reachable outcome.
- **check.py is the canonical scratch check** — overwritten per task;
  the repo briefing decides whether it stays.

## v69 (2026-07-20, F1-F8)

**The loop** (source: ADR 0040 and the invariants backlog — reactive
execution, steering, and the honest corrections).

- **The react protocol and executor** — an opt-in bounded act-observe
  loop: one action per turn through the full capability gate, denies
  as teaching observations, budgets binding, hollow traces failing,
  the same verification brain and G10 as the plan path, and approval
  gates that suspend to a conversation checkpoint and resume in place.
- **Mid-run steering** — /steer drops operator input into a running
  react loop as its next observation; input never authority.
- **Crash evidence survives** — per-round checkpoints salvaged by
  crash ingest; the audit trail shows where a dead loop stopped.
- **The record corrected itself** — R6 compaction was already built
  (v56); R9's foundation already exists; marked, not rebuilt.

## v70 (2026-07-20/21, F1–F8)

**The stalled turn and the readable schedule** (source: field test
2026-07-20, chat `73d93f04` — reconstructed from the store alone; field
addendum 2026-07-21, chat `4121496a` — the morning-ritual hunt).

- **A stall is not an answer** — a round with no user-facing text and
  no tool call gets one nudge, then the forced text-only pass; a turn
  can no longer end on an unexecuted plan.
- **The serve log exists** — `skep serve` writes `<home>/serve.log`
  (rotating) regardless of the launch shell; the access token stays
  stdout-only.
- **worker_protocol reaches dispatch** — react is a project-policy
  choice (fail-closed validation), with `skep run --planning-protocol`
  as the per-run override; ADR 0040's loop closes.
- **The schedule's recipe is readable** — list_schedules shows caste
  and (capped) instructions; the Queen stops hunting old chats for a
  script the store already holds.
- **run_schedule_now** — one carded verb makes an enabled schedule
  due; the ticker dispatches it on its next tick with the schedule's
  own delivery and health tracking. Run-now moves WHEN, never HOW.
- **Read repeats survive the turn boundary** — seen_reads is seeded
  from the transcript tail, so "yes do it" can no longer re-arm the
  same search cycle as fresh diligence; the v59-F7 nudge and off-ramp
  fire on the first cross-turn repeat.
- **The plan-failure record names its reason** — the summary carries
  the final validation error (or the hollow teach) everywhere only the
  summary shows; 'LLM coding plan failed.' alone blamed nothing.
- **The prompt states the isolation boundary** — every run has a
  private /tmp and filesystem; content reaches a worker inline in the
  dispatch instructions or through the repo, never via /tmp paths.

## v71 (2026-07-20, F1–F6)

**The forge and the daily companion** (source: the Stage F gap analysis —
docs/field-tests/hermes-replacement.md + the operator's gap list; motive:
skep authors its own MCP tools and keeps them).

- **The forge** — skep authors, lands, trials, and registers its own
  MCP tool servers: a coding run writes one stdlib-only Python file in
  `<skep home>/forge`; the patch lands via normal approval; promote_tool
  runs a sandboxed no-network trial (tools/list + a zero-argument
  self_test) and, on the confirmed card, installs and registers it as an
  ordinary stdio MCP server. Suspension deregisters; rollback is
  terminal. The v17 plugin lifecycle finally has its consumer.
- **The browser is an MCP server** — scope `browse` for e.g.
  `npx @playwright/mcp`: page-state reads flow, every page act
  (navigation included) cards until allowed; doctor warns when the
  launcher is missing. See docs/browser.md.
- **await_runs** — dispatch → await → synthesize: block on up to 5 runs
  and collect each run's view; timeouts report live snapshots honestly.
- **The Obsidian vault bridge** — carded sync_notes writes notes as
  markdown into `<vault>/skep/`; hand-edited files are never
  overwritten (conflicts land as .skep-conflict.md siblings).
- **The observation memory class** — curator-written without a proposal
  because it grants nothing and expires (14-day TTL, swept on the tick
  with an honest 'expired' event); durable memory keeps its human gate.
- **run_code latency measured** — p50 0.20s over 20 real sandboxed
  dispatches; the "heavyweight" gap claim closed as a no-op. Desktop /
  computer use stays a recorded deferral until Stage F evidence exists.

## v72 (2026-07-20/21, F1–F8)

**The daily driver** (source: the operator's all-day-assistant gap list;
seed evidence in docs/competitive-analysis.md — "it's a content gap, not
a design gap").

- **The brain dial** — the assistant LLM grows a third protocol,
  `anthropic` (Messages API, translated at the client boundary), and a
  carded `set_assistant_model` verb (saved default or one chat). Default
  workers inherit the saved config, so one dial upgrades both brains.
  See docs/brain.md.
- **The document caste is real** — drafts and summaries as deliverables:
  `.artifacts/draft.md`, nothing ever lands, acceptance from the task's
  `Must include:` line, workspace grounding via `Files:`.
- **Push, don't poll — finished (R5)** — scheduled-run terminals,
  schedule auto-disables, G10 re-verify disagreements, and provider
  health transitions all reach the operator unasked; the
  deliberately-quiet list is on the record.
- **The observation harvest** — v71's observation lane finally has
  feeders: observation-phrased chat lines and run terminals become
  expiring, grant-free memory on the tick; the digest remembers the
  week.
- **First-party mail + calendar MCP servers** — stdlib-only, forge-
  shaped, zero permission logic: email reads flow, sending always
  cards; calendar is read-only ICS. See docs/assistant-tools.md.
- **The pocket story** — docs/mobile.md says out loud what was already
  true: the messenger apps are the mobile face; four pinned
  confirmable action classes, everything else web-UI with the URL in
  the embed.
- **allow_fetch_domain** — a carded standing grant reads one exact host
  without per-URL cards; redirects re-decide every hop; deny always
  wins. One-turn article summaries on granted domains.
- **Same-worktree crash resume (R8 closed)** — a crash that left a
  checkpoint keeps its worktree, says so in the crash push, and the
  carded `resume_run` continues in place from the cursor (or honestly
  replays from step 0 when the tree is gone).

## v73 (2026-07-21, F1–F11)

**The field answers back** (source: the v72 field test on a
frontier-class Queen + the operator's four-model morning — both reports
in docs/field-tests/; every remaining failure was infrastructure, not
model).

- **Reads fit their own replay cap** — list_schedules is compact by
  default with a `name=` detail step; oversized JSON tool results are
  chopped at a valid-JSON boundary with a marker that names how to
  narrow; a fixture audit pins the big reads under the cap.
- **The operator's clock** — the pinned prompt carries local time + UTC
  offset each turn; "what ran at 5:20 am" is answerable on any model.
- **The wedged chat heals** — a provider 4xx gets ONE halved retry; a
  success records the chat's provider ceiling and compaction holds the
  chat under it from then on. The shrink is on the transcript; the
  second-failure line teaches.
- **/resume joins the deck** — model-free crash recovery on the web
  deck, the REPL, and /help; the crash push names it.
- **list_repos sees workon dirs** — clones and workon-bound
  workspaces, labeled by source; deleted dirs drop out.
- **Final-pass guards** — an echoed internal instruction is replaced by
  the honest line; text-shaped tool-call JSON becomes a teaching line
  offering the model dial; neither executes anything.
- **Never card into a missing path** — dispatch_run and workon share
  one resolver: a missing directory is refused before any card, with
  one story.
- **The literal `Must include:` example** — shown in the dispatch
  manual; the document worker names prose-only acceptance honestly.
- **The react fate line** — "continuing in place from the saved
  round", never "cursor None".
- **R9 closed (Part II complete)** — the composition record is pinned:
  three workers, three worktrees, three approvals, main never
  advancing. Every 2026-survey refinement is now landed or closed.

## v74 (2026-07-21, F1–F5)

**The window and the index** (source: the operator's context-meter
reading — a brand-new chat at ~96% context used at its first message.
Measured: 54,079-char tool specs + ~6,000-char prompt against a
16,384-token default window = a 92% fixed floor before the operator
types a word).

- **The num_ctx dial reaches the settings UI** — the API had carried it
  since v56-F1; the settings card now has the field (empty = auto), the
  note names the budget math (chars ≈ tokens x 4, all protocols) and
  the wire truth (ollama-only).
- **Auto-size from the live model** — config save and
  set_assistant_model probe ollama's /api/show (architecture-prefixed
  `.context_length`), cache per model, and resolve override → detected
  (capped at 65,536 — the replay budget is a cost dial, and the
  operator's explicit override is uncapped) → default. Detection
  failure never breaks a save; `num_ctx_source` says which rule is in
  effect. A chat pinned to a bigger model budgets like one.
- **The tool index** — the 54KB of tool schemas stopped riding every
  round. Indexed delivery (default) sends a categorized one-line index
  (~11KB, generated from the registry at import time — it cannot
  drift), full schemas for the 11-tool core set plus this chat's
  described-active tools, and `describe_tools(names)` for on-demand
  schemas (persisted on `chats.active_tools_json`). Authority does not
  move: the executor dispatches on the name, an indexed-but-inactive
  read executes when called, mutations still card, deny space stays
  unreachable. The unknown-tool error teaches; `llm_tool_delivery:
  full` is the one-flip escape hatch. Fresh-chat floor: ~60KB → ~24KB
  (pinned ≤ 25KB); with the auto window, first-message load ~96% → ~9%.
- **The meter splits the floor from the conversation** — context_view
  returns the floor in parts (tool surface, prompt, digest) plus the
  window source; the composer ring renders floor (amber) vs
  conversation (green), and hovering opens a breakdown popover: % used,
  a filled/empty bar, exact token numbers, free space.
- **One "You can reach:" roof** — the F3 tool index, the v53-F7 skill
  index, and the registered MCP server ids fold into a single prompt
  section, each naming its detail verb (describe_tools / view_skill /
  list_mcp_tools). Live registries feed it: a new tool, skill, or
  server appears with zero prompt edits.
- **The local usage tally (F6, operator ask)** — ollama.com exposes NO
  account usage API (ollama/ollama#15663), so skep counts its own
  requests at the engine choke point (ollama's final-chunk token
  counts) into `llm_usage` (8-day retention) and serves rolling 5h/7d
  windows at /api/llm/usage. The context popover and the settings card
  show the tally, labeled as a local count with ollama.com/settings
  named as the authoritative meter.

## v75 (2026-07-21, F1–F8)

**The shell holds** (source: the 2026-07-21 design review + build spec,
corrected by the upgrade plan's 12 corrections and ground-truthed at
993e315; direction: Option A "Refine the Hive" + the command palette).

- **Foundation** — six additive tokens (clay `--accent-2`, card/raised
  elevation, fast/normal motion, `--radius-xl`); the rail groups into
  daily / manage / configure; shared `relativeTime`, `buildTabBar`
  (panels persist across switches, render-once), `buildFilterBar`,
  `buildSparkline`. Every pre-v75 token and pinned anchor survives.
- **codeChrome deleted** — the two always-disabled composer pills were
  a broken promise (I9); the real code-context status row stays. The
  structure pin moved with the change, re-pinned to absence (C4).
- **Settings → five tabs** — Assistant / Worker / Channels / Webhooks /
  Repos, lazy-rendered one tab at a time, with per-session tab memory
  and a Channels status summary that claims only `enabled` / `live` /
  `secret_configured` (I8). The v74 num_ctx dial rides Assistant
  unchanged.
- **Runs → cards** — filter tabs with live counts; run cards keep the
  `searchable` class, superseded dimming (v19-F8), verify badges, and
  the autonomy summary as a pill; the "All" view groups by state with
  the unclaimed remainder under Other — grouped, never dropped (I8).
- **Policies → four sections** — Execution defaults open; Security /
  Advanced / Auto-approval collapsed; every field carries a one-line
  help hint (I9); the shell-command editor is argv-safe (C8): one row
  per command as its exact JSON array, raw-JSON escape hatch, no
  whitespace splitting anywhere.
- **Run detail → timeline + tabs** — a visual timeline (canonical path,
  failure tail, unmodeled states APPENDED in arrival order — C6);
  Events / Commands / Policy / Transitions as persistent tabbed panels
  (the live SSE log keeps streaming under a hidden tab; the raw
  transitions log stays reachable, I8); a copy-ID button.
- **Templates & Skills → two tabs** — authored vs learned; template
  cards with a Use → `#/assign?template=` link (route regex tolerates
  the query; parsing ships v76-F4); the skill stepper renders ONLY the
  lifecycle skills.py produces (draft → tested → approved, rejected as
  the failure branch) — the spec's invented states refused (I8).
- **The ⌘K palette** — the topbar hint stops lying (C3/I9): a real
  overlay with one entry per page + run-id jump; every entry is a
  `location.hash` navigation, and a region test pins the palette free
  of `api(`/`streamSse(` calls — it can never mutate (I5/I6).

## v76 (2026-07-21, F1–F8)

**The morning view** (source: the second half of the 2026-07-21 design
round, corrected plan C1–C12; the review's core complaint for these
pages — "no narrative, no 'what happened while you were away'").

- **Home answers the morning question** — an activity feed (runs +
  approvals merge, approvals stamped `requested_at` — C5), a next-3
  schedule strip, a 20-run verify sparkline on the pass-rate tile,
  "Active schedules" replacing the total-runs tile, and a welcome-back
  banner that counts ONLY terminal runs since the last visit (I8) behind
  a 4h localStorage threshold.
- **The Queen tile** — model + live dot in the topbar; liveness rides
  the existing poll, the hover names only sourced facts (window,
  num_ctx_source, tool delivery, 5h request tally — C10); hidden until
  its data arrives.
- **Projects stop being write-only** — cards with phase badges (the
  four real phases) and a `#/projects/:id` detail page composing three
  existing endpoints client-side; the effective-policy JSON stays
  reachable on the page that owns it.
- **Assign guides** — 3 steps (repo+instructions open, template
  optional, advanced collapsed with field-help), execution mode visible
  beside Dispatch (the review's KEEP), a form-echo preview line, and
  `?template=` prefill closing the v75-F7 link contract (C9); unknown
  templates teach.
- **Approvals order themselves** — priority sort (high→medium→low,
  oldest first within a tier), type filters via approvalKind, a
  `requested_at` waiting clock (urgent past 1h; visibility, never
  authority), and an empty state that points at review work.
- **Schedules read as countdowns** — relative next-run (past =
  "overdue", absolute on hover), health banners only on actual failure
  (warn at 3 consecutive), inline disable at 5 through the EXISTING
  PATCH toggle, with the ticker's own auto-disable-at-5 named honestly.
- **Notes & Tasks grow up** — markdown-rendered notes with #tag pills
  and a filter, checkbox tasks grouped Overdue → Due today → Upcoming →
  No due date → Done(collapsed), and the explicit `run:<id>` token
  linking runs (no bare-hex guessing — I8).
- **Memory shows its shape** — class chips lockstep-pinned against the
  store's 7 MEMORY_CLASSES (the spec's invented classes refused),
  count+size, proposal age; the chat sidebar gains client-side search
  and localStorage pinning with a pinned-first section.

## v77 (2026-07-21, F1–F5)

**The terminal face: the CLI round** (source: design review §5 / build
spec §7, ground-truth-corrected — the CLI has carried the full command
deck since v48-F5; this round completes it and gives the REPL honest
eyes).

- **Confirm cards carry weight** — boxed rendering on a TTY with
  ANSI-safe width math; args never truncated; NO_COLOR governs color,
  not structure; piped output stays byte-identical (the v50-F1 script
  contract).
- **The prompt answers "how full am I, on which model?"** —
  `model: … · ctx: NN% · you ›`, threshold-colored (green <60, amber
  60–79, red ≥80), reading the same context_view as the web meter;
  readline-safe via \001…\002; cached per model turn; a failed refresh
  falls back to the bare glyph.
- **The deck completes** — /status (the banner, re-runnable), /model
  (effective model; with a name, cards set_assistant_model — scope
  chat by default), /exit, /replay; the web↔CLI parity pin re-pinned
  around an explicit CLI_ONLY set, each member documented by its web
  equivalent.
- **Tool calls read as threads** — `▸ name` + a deterministic result
  summary read off recorded JSON (✗ errors unmissable, ✓ counts, run
  ids), identical live and replayed; never model text.
- **Ids are handles** — task ids OSC 8-link to their run page (full
  ids — operators copy them into /land //steer //resume); resuming a
  chat prints one summary line naming what was withheld and the
  command that shows it, instead of twenty messages of scroll.

## v78 (2026-07-21, F1–F6)

**The gateways grow up** (source: design review §6 / build spec §8,
C7-scoped into its own server-side plan; two review proposals rejected
by name — channel-side landing authority in any spelling).

- **A volume dial per channel** — `notification_level`
  (all/approvals/none) enforced once at the outbound choke point;
  it can only silence delivery, never allow anything, and the chat
  row + web UI record everything at every level; confirm cards are
  in-turn replies and never consult it.
- **States wear their color everywhere** — one STATE_EMOJI map,
  prefixed at the source (`run_terminal_text`), inherited by the web
  transcript, all three channels, and the scheduler funnel.
- **Discord shows results with dignity** — terminal pushes carry a
  color-coded embed (house palette) beside the honest text line, with
  the supervisor's summary, the re-verify verdict only when the store
  holds one, and a link to `#/runs/<id>`; a vanished run degrades to
  plain text.
- **`/skep` without a model turn** — status and runs answer straight
  from the store; approve/deny are a third spelling of the existing
  button, through the same fail-closed gate (shell/policy/landing stay
  web-only); strangers get silence; registration is one idempotent
  bulk PUT whose 403 names the missing invite scope.
- **Telegram renders the Queen's markdown** — bold, code, and fences in
  MarkdownV2 with everything else escaped; a rejected parse resends the
  same message plain — formatting never costs a message.
- **Slack stops burying the channel** — pushes thread under the
  operator's latest message via the captured `thread_ts`; terminal runs
  arrive as rich blocks whose only button is a URL ("Open in web UI") —
  a link, not a verb.

## v79 (2026-07-22, F1–F5)

**The field-test five** (source: chat-store forensics 2026-07-21 —
11 chats, 1,088 messages, 106 runs; the five failures ranked by
operator pain, each traced to file:line before the plan was written).

- **Empty remotes stop eating days** — registering an empty GitHub
  repo now pushes the synthesized baseline (create-only), and carded
  `push_baseline` repairs repos already in the trap; the PR error
  teaches the in-skep path.
- **Approvals are a ledger the Queen can read** — the last 10 resolved
  verdicts ride `list_approvals` with who/when/where-landed, and runs
  expose their resume chain in both directions.
- **read_file follows the work** — an explicit `ref` or an automatic
  fall-back to the project's landing branch; the miss error names the
  branches instead of lying "not a file".
- **Tokens are counted where they're spent** — worker runs report real
  prompt/completion counts into task_usage; absent counts stay None.
- **Loops hit a wall** — an identical read already nudged this turn is
  refused without executing; the turn ends in an answer, not a poll.

## v80 (2026-07-22, F1–F2)

**The Streamable HTTP MCP runner** (source: I10 seed — the http
transport had been config-only since v17; registered remote servers
were dead rows).

- **Remote MCP servers are callable** — `runner_for_config` speaks
  the MCP Streamable HTTP transport: session-aware JSON-RPC POSTs,
  JSON or SSE responses, honest MCPErrors for dead URLs and legacy
  /sse-only servers; the policy/card path is untouched.
- **The tool description tells the truth** — "config-only in v1" is
  gone; the description names the endpoint shape instead.

## v81 (2026-07-22, F1–F15)

**The field-test fourteen (+1)** (source: I10 seed — the 2026-07-22
dogfooding day; 15 findings reconstructed with file:line anchors, one
was opt-in-by-design and needed no code).

- **Landing tells the truth and protects the right branch** — the
  default-branch guard reads the repo's real default, not the checkout;
  a failed apply denies its approval with the reason; runs pin their
  base commit so stale lands are flagged before approval and named in
  the failure; a checked-out target refuses with the remedy.
- **The Queen picks landing branches from a menu** — `skep/<task_id>`
  or the project's `auto_apply_branch`; invented names are rejected
  with the menu. PR surfaces read the persisted landing branch.
- **G10 alarms only on real risk** — a patch-less run reads "nothing
  to re-verify", never NOT CONFIRMED.
- **run_code deliverables survive** — script-written files are
  declared, delivered to `~/.skep/workspace/<slug>/`, and named in the
  tool result.
- **Surface gaps closed** — `reopen_task`, `list_notes` paging with
  honest totals, the dispatch deny lists registered repos, `/repos`
  and `list_repos` agree, `/skills` in both decks, `--oneshot --yes`
  for card-gated scripting.
- **Pending cards always appear** — the web UI reconciles cards after
  a dropped stream and on the poll; verified in a real browser.
- **Global auto_approve is inert** — per-project maintain is the only
  auto-apply ramp.

## v82 (2026-07-22, F1)

**The loopback guard** (source: the stranded Air branch
`local/v70-F4-context-meter` — the one salvageable idea after v74
superseded it).

- **A local daemon's window is the operator's call** — loopback
  ollama is never probed or auto-matched; num_ctx there is
  pre-allocated KV-cache RAM, and the conservative default stands
  until the operator dials it. Remote detection, the anthropic
  floor, and the explicit override are unchanged.

## v83 (2026-07-22, F1–F15)

**Hermes parity, governed** (source: the 2026-07-22 comparison — close
the day-to-day gap, keep every wall).

- **All 27 Hermes tools have a skep disposition** — direct read,
  granted lane, card, or governed equivalent; none direct-executes
  from the model's hand. read_url returns markdown with a granted-lane
  budget; run_code has a sandboxed 10s fast lane; run_shell/
  start_process/stop_process/quick_edit/delegate_analysis/remember/
  get_chat_context/setup_browser join the surface.
- **Prompt schedules** — "every morning summarize yesterday" as a
  read-only, store-reads-only Queen turn (ADR 0042 names the
  unattended-injection surface it closes).
- **The shelf has books** — 37 zero-grant seed skills load at startup
  (ADR 0043: operator copies win, deletes tombstone) and a shipped
  yt_transcript seed tool promotes through the forge's own trial+card.
- **The freeze** — Stage F runs against v83; the hotfix line is dated
  before the outcome.

## v84 (2026-07-22, F0–F8)

**The phase-2 shelf, review-corrected** (source: the v84 plan review —
seven amendments folded in before execution; the freeze point moved to
v84 on the record).

- **39 new seeds** (productivity, email, social, MLOps, ML models,
  creative) bring the shelf to 76 — every one zero-grant, parsing, and
  lockstep-checked against the tool surface.
- **Grant hygiene is law, not style** — a shelf-wide test pins that
  seeds only name multi-token read-verb prefix grants; mutation verbs
  never appear in a grant; curl is never a write path.
- **Outbound content has a mechanism (ADR 0044)** — posting/send
  prefixes are never-grantable at every persist lane; posts always run
  carded with the verbatim payload in the argv; pre-v84 grants swept.
- **The document toolchain is stated** — documents/ocr extras plus an
  honest per-library presence block in the planning prompt; the
  tesseract probe is functional (A4).
- **hermes-import** — memory/skills/sessions stage behind the existing
  gates with per-class batch review and visible [hermes-import]
  provenance in search; ~/.hermes is retirable.

## v85 (2026-07-23, F1–F7)

**The shelf points outward** (source: the Pi comparison — two adoptions,
operator-directed; supersedes v84's "v85 waits for Stage F" note).

- **Agent Skills standard** — real-world frontmatter parses (quotes,
  block scalars, wrapped lines); `skep skill shelf add ~/.claude/skills`
  registers external shelves synced at serve start under the seed rules
  (zero-grant, tombstones, operator wins), provenance `external`.
- **The pack ladder (ADR 0045)** — script-shipping packs are packages:
  they draft onto the v17 lifecycle (import-md `--allow-script`, or
  auto-draft on shelf sync) and reach the registry only through a
  parse-only trial plus a human action (`skep skill promote` /
  `promote_skill_pack` card). Suspension removes the registry skill
  (registered ⟺ active); rollback is terminal.
- **Grants become real** — activation snapshots the pack and rewrites
  script grants onto `.skep-skill/<id>/…`; dispatch materializes the
  snapshot into the run workspace, so the granted argv and the file
  agree on every sandbox backend.
- The fresh-chat floor pin re-measured 26KB → 26.5KB (two new tools).

## v86 (2026-07-23, F1–F2)

**Approval memory** (source: operator request — "approve = session,
approve always = always").

- **The session tier (ADR 0046)** — a plain approve on a shell-gate
  card now persists the command for the serve session
  (`session_allowed_shell_commands`, merged read-side at the one
  `_shell_allowlist_for` choke point; cleared and logged at startup).
  Remote-git, dangerous, and outbound-content classes never persist —
  they stay approve-once. "Approve & remember" remains the only path
  to permanence, and the durable union writers cannot absorb session
  entries.

## v87 (2026-07-23, F0–F8)

**The 2026-07-23 field test** (source: one day driving the
youtube-summary skill through three Queen models; plan
`plans/v87/README.md`). Two gate fixups rode ahead of the round: the
fastlane escape test aimed at a sandbox-allowed temp root, and
`reconcile` now reaps zombie children (`kill(0)` cannot tell a zombie
from a live process — the row lied "running" forever).

- **F1 resolver** — bare repo names resolve clone-slug → workon
  binding → host path; the daemon's CWD never participates (serve
  launched from the checkout turned "skep-workspace" into a
  nonexistent path while the binding sat unconsulted).
- **F2 gate cards in chat** — a pending gate plants an actionable
  approve_review card in the dispatching chat (source='gate'):
  Approve rides the /approve verb, Deny denies the review (v48-F3),
  any other surface's resolution supersedes it (v63-F2), the timeout
  sweep skips it, and it never locks the composer.
- **F3 channel health** — "never configured" stated in those words:
  `skep channel status`, configured/last_delivery/gateway fields on
  GET /api/channels, delivery-miss logging, gateway session
  breadcrumbs. (The field's "Discord doesn't work" was an unconfigured
  channel presenting as breakage.)
- **F4 verify before victory** — get_run carries `patch_digest`
  (per-file counts + first added lines, capped, drops stated) for
  completed runs; the unlanded-patch notification names the changed
  files; success-shaped prose about an untouched deliverable draws
  ONE verify nudge (I2, Queen-side).
- **F5 fabrication is a failure** — the worker prompt names invented
  content a FAILED run and requires verify to prove derivation;
  dispatch_run/POST /api/runs gain per-run `protocol='react'` for
  fetch-then-synthesize tasks (plan-mode plans before the data exists
  and can only fabricate); create_skill teaches the classification.
- **F6 env bootstrap** — allow_env_bootstrap grants uv venv/uv pip
  install/python3 -m venv/python3 -m pip install in one card through
  the standard guards; the worker prompt states the host's actual
  Python toolchain (bare `pip` does not exist on macOS).
- **F7 the wait names itself** — turn_status events ('thinking',
  'running <tool>') on the message stream; the web working-line adds
  a browser-side elapsed counter, the terminal prints a dim marker.
  await_runs kept its v71-F3 cap + honest partial unchanged.

## v88 (2026-07-25, F1–F5)

**A trace, not a field test** (source: an operator-directed walk of
dispatch → capability gate → verify → land, checked against
`docs/invariants.md` Part I; plan `plans/v88/README.md`). The
structural posture held — the git denies fire before the verify
fast-path and before any allowlist or grant, the same prefixes can
never be persisted as standing grants, `sudo`/`doas` is refused first
because every deny below keys on `argv[0]`, the Queen is bound to the
worker's list (v83-F9), and landing always takes a fresh non-default
branch. Two documented claims turned out stronger than their code, and
two chat-UI complaints from the same session rode along.

- **F1 one sidebar row height** — loose chat rows are direct children
  of the column-flex `.chat-sidebar` and shrank below content height
  once the list overflowed; pinned and terminal/discord-grouped rows
  sit a level deeper and did not. `flex: none; height: 32px`.
- **F2 the sidebar collapses** — a toggle in the existing
  `.chat-toolbar` flips `.sidebar-hidden` on `.chat-layout` (the same
  `display:none` the mobile breakpoint used), persisted in
  localStorage like the v76-F8 pins (I11).
- **F3 `await_runs` can say a run failed** — the failed-run coaching
  existed but was wired only into `get_run`, while `await_runs` (the
  tool the Queen blocks in) hand-rolled two branches covering no
  terminal failure. A dead run arrived as a bare `settled: true`, so
  the Queen reported nothing and retried nothing. Settled runs now
  carry `verification_details` and the shared per-run guidance; the
  duplicated pending_approval string is deleted (I8, I9). Automatic
  re-dispatch was deliberately not added — that is a policy decision,
  and the actual defect was that nothing said the run had died.
- **F4 the project pins what verification means** (I2) — G10 re-ran
  the command the *worker* nominated, so a worker verifying with
  `true` earned `confirmed=True` for a broken patch and, under a
  `require_reverified` rule, an automatic landing. A `verify_command`
  project-policy key now outranks the worker's nomination, resolved
  inside `run_task` so the resume and skill-test paths cannot silently
  downgrade G10, re-resolved on crash recovery, and the re-verification
  detail always names which command was re-run (I8). Unpinned projects
  keep the old fallback.
- **F5 I4 says what the code guarantees** — narrowed to remotes and
  branch-switching, which are absolute; staging and committing are
  denied through `shell.run` but grantable through the explicit-intent
  `git.stage`/`git.commit` capability path, and that is safe because
  the worktree is disposable and the patch diffs against the startup
  baseline — not because it is absolute. I1 is what makes a worker-side
  commit consequence-free.

## v89 (2026-07-25, F1)

**A race the v88 gate caught** (source: `test_parallel.py` failing 1 run
in 3 during the v88 gate, never in isolation; plan
`plans/v89/README.md`). Pre-existing, not a v88 regression — v88 touched
no worktree, threading or sweep code, though its extra store read in
`run_task` shifted thread timing enough to widen an already-live window.

- **F1 the orphan sweep and worktree creation are mutually exclusive** —
  `run_task` takes its keep-set snapshot before it mints a task id and
  before `_ACTIVE` registers the shield, so a parallel sibling's sweep
  could walk the directory, find a worktree whose `git worktree add` had
  printed "Preparing worktree" but not yet checked out, and delete it —
  killing the run with "fatal: this operation must be run in a work
  tree". `_ActiveWorktrees` locked each operation, but
  `snapshot → walk → remove` and `register → create` are *sequences*,
  and per-operation locking cannot exclude one sequence from another.
  A `TREE_LOCK` in `worktree.py` is now held across each whole sequence
  — including the keep-set snapshot, which reopens the window if taken
  outside it — at all three sweep sites, at the register+create pair,
  and in `reverify.py`'s creator.

No authority changes: this is a lock around existing filesystem
bookkeeping. Keep-set semantics are untouched, so preserved
pending-gate worktrees (I1's resume path) and salvaged crash
checkpoints (v72-F8) behave exactly as before.

## v90 (2026-07-25, F1–F5)

**Three operator asks and one decision** (plan `plans/v90/README.md`):
no way to run Claude Code as a worker; the approval card said the same
thing three ways and never named the risk; an approval already given
was asked for again. Plus the operator's answer to v88-F4's open
question — phase-targeted defaults rather than global.

- **F2 the card says one thing per line** — a server-side summary:
  `headline` (the argv, URL or branch verbatim), `purpose` (the tool
  description's first sentence), and `risk` — or no risk line at all,
  because an invented risk is as bad as a buried one. The risk text is
  drawn from the guard classes the policy engine actually consults
  (outbound content/ADR 0044, privilege escalation, ops-mutating) plus
  membership classes for publishing, landing, policy and destructive
  verbs. The model-facing description and the raw argument dump move
  behind a details disclosure.
- **F3 a repeat approval is honored once and shown** — confirming a
  card now writes a session-provenance rule onto the *same* learned
  list the always tier uses, so `resolve()` composes it unchanged and
  nothing can be learned into denied space. Repeats auto-resolve
  through the decision functions that already existed. Never-grantable
  classes (remote git, dangerous prefixes, outbound content) stay
  approve-once. A grant-covered action now leaves a receipt — the same
  headline and risk, no buttons — because silence was
  indistinguishable from nothing having happened (I8). Also fixed: a
  worker read approved network hosts from the current verdict only, so
  approving host A then host B dropped A and it was asked for again.
- **F4 the auto-landing lane requires a pinned verify_command** —
  maintain is the only lane that lands without a human, and it accepted
  any confirmed re-verification, including one earned by re-running
  `true`. It now requires the project to have said what verification
  means. An unpinned project is not blocked; its patch simply waits for
  a human. (The "missing key named on the record" half of this claim was
  not true until v106-F11 wrote the block reason to the audit trail —
  v90 shipped the enforcement, not the visibility.)
- **F1 CLI-agent engines become selectable** (ADR 0047) — the Claude
  Code, Codex and Aider adapters have been complete since v33 and
  unreachable because nothing mapped a name to them. A `coding_engine`
  project-policy key now chooses one, `skep doctor` reports which
  binaries are present, and the engine's API host is merged into the
  allowlist. A CLI engine requires a pinned `verify_command`, because
  its own verification is `git diff --check` — whitespace.
  **The boundary is stated, not assumed:** an external agent's commands
  do not pass skep's capability layer; the sandbox is what confines it.
  ADR 0047 tables which wall enforces what and names the cost — a CLI
  engine cannot commit because its worktree is disposable, and cannot
  reach a remote because the network pin excludes one, rather than
  because a deny fires. I1 is untouched and is what makes both
  consequence-free.

## v91 (2026-07-25, F1–F2)

**The pin nothing was setting** (operator ask; plan
`plans/v91/README.md`). v88-F4 gave a project the power to say what
verification means and v90 made that pin mandatory on the two lanes
where its absence is fatal — but no setup path ever wrote one, so every
ordinary run still re-ran the command the worker nominated for itself.
This is the alternative v90 deferred with a reason ("a confidently wrong
inferred command is worse than none, and it deserves its own round");
the option v90 *rejected* — refusing to confirm any worker-nominated
verification anywhere — stays rejected.

- **F1 setup pins a verify_command by default** — inferred from the
  repo's own declared entry point, reading the same explicit toolchain
  table that has seeded the shell allowlist since v23-F4, at the same
  already-confirmed moment. Conservative by construction, because a pin
  that cannot pass is indistinguishable from a broken patch: `make test`
  only when the target is declared, `uv run pytest`/`pytest` only when a
  test tree exists (pytest with no tests exits 5, which `reverify` reads
  as *failed*), `npm test` only for a real script (`npm init`'s
  placeholder exits 1 and is refused by name). Nothing detected means no
  pin and the pre-v91 fallback, unchanged. The pin now survives a phase
  change — phase defaults carry none, and the move *into* maintain is
  the move into the only lane that lands without a human. The three
  setup-preview surfaces (CLI, `cli_chat`, `app.js`) state which command
  G10 will re-run, including when the answer is the worker's own, since
  the weaker guarantee has to be legible at the moment of confirmation
  (I8). Existing projects are not rewritten: `skep doctor` names the
  ones still on the fallback, what it costs them, and how to fix it
  (I9).

- **F2 the card's purpose line stops speaking to the model** (field
  report: a `land_run` card whose what-is-this-for line read "PROPOSE
  landing a completed run's patch (requires user confirmation)").
  v90-F2 derives that line from the tool description's first sentence,
  and the mutating descriptions open with a wrapper written to steer
  the Queen, not to inform the human. `purpose()` now strips the
  wrapper at the presentation layer — descriptions stay untouched,
  they are load-bearing model prompts — and the sentence splitter no
  longer ends at "e.g."/"i.e.". The land_run card now reads "Landing a
  completed run's patch." over its risk line (I8).

## v92 (2026-07-26, F1–F3)

**The chat shows who is working, and since when** (field test
2026-07-26, chat `39bc00e2`; plan `plans/v92/README.md`). The operator
approved a gated run's shell request; the run was re-dispatched
server-side, completed three seconds later, and the 🟢 patch-ready call
to action was persisted — while the open chat rendered none of it. The
record was honest and the surface hid it (I8): no lane reopened the
status stream after the approval, the v43-F4 status line only moved on
heartbeat boundaries, and a completion born outside a live stream
waited for a manual reload.

- **F1 the worker loader** — the shared status line becomes one pulsing
  row per live run this chat owns: `worker <id>… · <phase> · <elapsed>`,
  a link to the run page, ticking every second in the browser while the
  server stays the authority (each SSE status event resyncs the clock
  from the run's own event timestamps, so the timer counts from
  dispatch). A live dispatch tool result seeds its row at 0s instantly;
  replay never seeds (a historical result may be long terminal — the
  stream resyncs any still-active run). Terminal events clear the row
  and now announce completions too, deduped across the v56-F7 replay
  window, and — never mid-stream, never over a draft — redraw the view
  so the stored call-to-action line appears without a reload
  (v81-F13-style). Route teardown closes the status stream and the
  ticker.

- **F2 the Queen loader carries its start time** — every `showWorking`
  phase stamps its start clock the moment it begins and ticks from the
  first second: "Running dispatch_run… · 11:17 · 5s". The v87-F7
  3-second threshold goes; the start stamp anchors phases too short to
  count. Phase vocabulary, `turn_status`, and the ticker structure are
  unchanged.

- **F3 every dispatch-capable lane reopens the status stream** — the
  incident's trigger: `watchStatus()` ran only at chat open and after a
  `deliver()` turn, so the `/approve` that re-dispatched the run left
  nobody subscribed. Card verdicts and every deck command now reopen the
  stream; idempotent, since `watchStatus` closes its predecessor and a
  chat with no runs streams nothing.

## v93 (2026-07-26, F1)

**An approval covers its bare-flag variants** (operator request; plan
`plans/v93/README.md`). A command was approved, ran, failed, and the
worker's retry — same command, same operands, different flags
(`pytest -x tests/foo.py` → `pytest -vv tests/foo.py`) — re-carded the
operator inside the session that just approved it, because approval
coverage was an exact token-prefix match everywhere it is consulted.

- **F1 bare-flag variants of an approved command are covered** — after
  the exact-prefix lanes miss, a second lane in the one shell-decision
  funnel (`ShellExecPlugin.decision`, I5) asks whether the candidate's
  *positional skeleton* — bare flags stripped, everything from a literal
  `--` on kept verbatim — is identical to an approved entry's. A bare
  flag is `-letters`/`--word` only: glued values
  (`--index-url=http://x`, `-o/tmp/x`, `-d@file`) keep their token, so
  no payload rides a "flag" past an approval, and a separated flag value
  reads as a positional and blocks the match. `git`, `find`, `sudo`,
  `doas` never match the lane (a bare flag alone flips those between
  read and destroy); every hard deny still precedes it (I4). Both grant
  tiers get the lane — resume grants and the merged allowlist carrying
  the v86-F1 session tier — the ledger keeps the verbatim approval
  (I13), and a variant match declares itself: reason
  `…shell_command_flag_variant` / `…shell_allowlist_flag_variant`,
  detail naming the covering command (I8).

## v94 (2026-07-26, F1–F7)

**The claude_code engine actually works end to end** (live field test,
operator-requested; plan `plans/v94/README.md`). Four dispatches
against a fresh uv sample repo surfaced the whole chain: the engine
could not authenticate (env scrub), could not edit (headless
permissions), was SIGKILLed mid-run (no heartbeats), ran on the naked
host in workspace mode, had no operator surface to select it, the
preview lied about the inferred verify pin, and the ONE run that
produced a perfect patch was rendered NOT CONFIRMED because `uv run
pytest` cannot run under the deny-all reverify profile. Acceptance
after the fixes: the same task, zero workaround flags — sandboxed
dispatch, patch produced at 58s, G10 `passed [confirmed]` on the
pinned command, landed on `skep/<task>` by human approval.

- **F1 the adapter grants Claude Code its hands** — `--permission-mode
  acceptEdits` joins the headless argv; `--print` cannot prompt, so
  every write was rejected and the engine could never patch. The
  sandbox is the wall, not Claude's prompts (ADR 0047).
- **F2 the adapter heartbeats while the agent thinks** — a timer
  thread emits contract HEARTBEAT events every 5s during the agent
  subprocess; the event stream takes a lock. The monitor's
  3×heartbeat kill rule is untouched — the adapter finally honors it.
- **F3 an engine declares the env it cannot run without** —
  `CodingEngine.env_vars`, merged at resolution like the API host
  (ADR 0047 §3): `claude_code` needs `USER`/`LOGNAME` for its macOS
  keychain lookup; the baseline is PATH+HOME. Identity names, never
  secrets; G2 intact.
- **F4 an external engine only ever runs inside the sandbox** — the
  resolver coerces execution mode to sandbox (the coerced mode is the
  shown mode, I8); dispatch backs it for every caller and refuses to
  run with no usable backend instead of falling back to the naked
  host (I9/I12).
- **F5 the operator can choose the engine** — `skep project
  setup/preview --engine NAME`, validated against the registry at
  setup time; previously only tests ever wrote `coding_engine`.
- **F6 the preview tells the truth about the pin it inferred** — the
  summary printer read a key the preview payload never carried and
  printed "none detected" over a freshly inferred pin; it now reads
  the project dict, correct on both paths.
- **F7 G10 can confirm a uv-pinned project** — re-verify primes the
  baseline env (before the patch applies, so patch code never gets
  the network), then runs the pinned command offline with a
  workspace-local uv cache (`UV_NO_SYNC`). The shared host cache
  stays unwritten; a failed prime reads "unavailable", never patch
  guilt.

## v95 (2026-07-27, F1–F4)

**Engine selection reaches the chat** (live field test,
operator-requested; plan `plans/v95/README.md`). The operator tried to
set up a project from chat with Claude Code as the engine; the Queen
sent `policy_overrides` as a JSON string and confirm died with a 500 at
`.items()`. Beyond the crash, "run THIS task with claude_code/codex"
had no chat-shaped path at all — the engine was a project-policy key
only. A gate casualty rode along: a v74 usage test carried a hardcoded
date that aged out of its own 7-day window six days after it was
written.

- **F1 the usage-window test stops carrying a time bomb** — the
  backdated row is now relative to `datetime.now(UTC)`; the suite no
  longer goes red by calendar (I10).
- **F2 object-typed tool args tolerate the JSON-string variant** —
  `_object_arg` decodes a stringified `policy_overrides` /
  `create_schedule.params` / `call_mcp_tool.arguments` and refuses
  garbage honestly naming the key (I9), a card error instead of an
  AttributeError 500 (I8). `_call_args` already handled the
  whole-blob-as-string variant; nested params now match.
- **F3 engine is a per-dispatch knob on dispatch_run** — threaded
  through `submit_run` → `resolve_run_policy` ABOVE the v90/v94 guard
  block: same single validation point (I5), unknown names fail closed
  naming the request (I9), external engines still require the pinned
  `verify_command` (I2), forced sandbox + host/env merges intact
  (I12). An explicit choice joins the explicit-overrides tuple and
  always cards (I6/I7). Fresh-chat floor re-measured 26.5KB → 27KB.
- **F4 setup_project takes engine first-class from chat** — the CLI's
  `--engine` (v94-F5) mirrored onto the chat tool: validated at setup
  naming the choices on a typo, folded into
  `policy_overrides.coding_engine` (same write path, I5); an explicit
  override beats the sugar.

## v96 (2026-07-27, F1–F5)

**The chat cockpit** (operator-requested design round; plan
`plans/v96/README.md`, co-designed in session with v97 — modular policy
groups — planned behind it). The chat page had no project awareness: the
composer's status row scraped tool results for a branch-looking string,
the server had no operator-visible chat→project binding, and push/PR
required deck incantations with task ids.

- **F1 effective_policy_view names engine, protocol, and the verify
  pin** — `coding_engine`, `worker_protocol`, `verify_command` join the
  one policy read every surface shares; unpinned verify renders an
  explicit `(worker-nominated fallback)` marker, never blank (I2/I8).
- **F2 a chat knows its project** — plan corrected during execution:
  the column already existed (`chats.project_id`, v56-F4, auto-bound on
  dispatch/workon); what was missing was every surface. Now:
  `PUT /api/chats/{id}/project` (operator/UI only — the Queen reads,
  never writes, I6; refusals name the known projects, I9), the chat GET
  returns a `chat_project_view` (name, phase, engine, repo; a deleted
  project renders unbound, I8), one `chat_project_line` in the pinned
  prompt so the Queen defaults repo/project args to the binding, and
  deck `/policy` `/state` default to the bound repo.
- **F3 the composer strip becomes real** — project selector +
  branch/policy/engine pills reading server truth (`effective-policy` +
  repo `state`), CSS-only popovers on the existing context-popover
  pattern; the transcript-scrape heuristic (projectChangeSignal) is
  deleted with its render sites; an unresolved policy renders AS
  unresolved (I8).
- **F4 Push and Open PR, one card each** — the verbs existed carded
  since v47/v57; `push_branch` + `open_pr` join `COMMAND_TOOL_NAMES`
  (the only gate that stood between them and the operator-command
  path), `open_pr` gains branch mode (`branch`+`repo` → push an
  existing local branch and PR it; selector refusal names all three
  modes, I9), and two strip buttons propose the cards — the card stays
  the confirmation (I6/I7); both verbs stay web-UI-only.
- **F5 push_branch pushes the checked-out branch** (live-verify find,
  I10: the verify-skill acceptance run WAS the reproduction) — v57-F7's
  guard ORed in `default_branch`, which actually returns the CURRENT
  checkout, so push_branch refused whatever branch you were on; dormant
  while only landing branches were pushed, fatal to the Push button.
  Both push-path guards now compare against `repo_default_branch` only
  — the check that means the I1 line. `delete_branch` keeps its clause
  (refusing to delete the checkout is correct).

Verified end-to-end against a throwaway home: strip renders bound
project/branch/policy/engine with popovers, Push card really lands the
branch on a bare origin, Open PR cards and denies honestly.

## v97 (2026-07-27, F1–F6, ADR 0048)

**Modular policy groups** (operator-requested, co-designed in the v96
session; plan `plans/v97/README.md`). Policy becomes modular: define a
convenience-grant bundle once (network hosts, shell prefixes, env vars,
budgets, engine), name it, attach it to any number of projects, edit it in
ONE place and every attached project follows on its next dispatch — with a
copy-on-write fork for "change it for THIS project only".

- **F1 groups exist** — `policy_groups` settings blob; write-time vetting
  with the same validators project policy passes (I5) incl.
  `dangerous_prefix_reason` (I4); trust-ramp keys ungroupable by
  construction, refused naming the groupable set (I9); builtins
  `python-bootstrap` / `node-dev` merged read-side (edit materializes a
  copy, delete reverts — builtins revert, never vanish).
- **F2 live composition** — one merge point (`run_policy_for_repo`):
  defaults → phase → groups (attach order) → project overlay. List keys
  union; project scalars beat any group. The v90/v94 engine guard runs
  AFTER composition (a grouped engine still needs the verify pin, still
  forces sandbox — I2/I12); a dangling attach fails the dispatch closed
  naming the group (peeks keep working via a breadcrumb); delete-while-
  attached refuses naming the projects.
- **F3 the five verbs** — `set_policy_group` (with `fork_from`/
  `repoint_project` — the whole copy-on-write fork is ONE carded action,
  validated entirely before the first write), `delete_policy_group`,
  `attach_policy_group`, `detach_policy_group`, `list_policy_groups`.
  Card risk lines say which shape is in play (in-place reaches every
  attached project; a fork leaves the source untouched, I8). Fresh-chat
  floor re-measured 27KB → 27.5KB (the ritual, not a surprise).
- **F4 setup suggests, never attaches** — `setup_project groups=` / CLI
  `--group` attach explicitly (same sugar shape as `engine=`); the
  toolchain sniff SUGGESTS builtins on preview/setup results (I6:
  suggested ≠ applied); a typo'd group refuses at setup naming the known
  set.
- **F5 the surfaces** — `effective_policy_view.policy_groups` carries each
  attached group WITH its grants ("why is this host allowed" has an
  answer, I8); `#/policies` grows the groups editor with the **"Save as
  new group"** toggle (context-sensitive default: pre-checked when opened
  from a project's view while the group serves >1 project); project detail
  links edit-in-context; the v96 strip's policy popover lists attached
  groups. Operator-direct routes incl. an atomic fork endpoint.
- **F6 group verbs join COMMAND_TOOL_NAMES** (acceptance find — the
  v96-F4 lesson relearned; the acceptance run IS the reproduction, I10).

Verified live end-to-end: carded attach to backend+frontend → both resolve
the group host; edit once → both follow; fork with repoint → frontend on
the fork, backend untouched; delete-while-attached refuses naming projects;
trust-ramp group refused at write. Not exercised live: a full sandbox
dispatch with python-bootstrap (composition + allowlist union is
unit-tested; v94-F7 offline re-verify untouched).

## v98 (2026-07-27, F1–F3)

**The ECC harvest, and the seed that lied** (operator-requested survey of
`github.com/affaan-m/ECC` — 281 external Agent Skills, MIT; plan
`plans/v98/README.md`). The survey's finding is mostly negative and that is
the useful part: skep's own 76-seed shelf (ADR 0043) already covers nine of
the twelve candidates in a better-adapted form, and verbatim import is
rejected on shape — 281 descriptions flood a small Queen's index, the bodies
run 200–800 lines against a house format of ~25, and they name ECC agents and
slash commands that do not exist here (the preset-hallucination class, I9).
Three things survived, and checking for overlap turned up a real defect.

- **F1 the defect** — `orchestrate-cli-coding-agent` told the Queen to send
  one brief to different backends via `batch_dispatch`; the batch task-item
  schema had no `engine`, so the closest reachable behavior was N runs on the
  same engine, silently. v95-F3/F4 threaded `engine=` into `submit_run` and
  `dispatch_run_decision`; `batch_dispatch` is the OTHER caller of both and
  was never updated — the v96-F4 / v97-F6 lesson in a third shape. Per-member
  `engine` now passes through both call sites with no new guard, because the
  member takes the same path a single dispatch takes: `resolve_engine` refuses
  an unknown name, an explicit engine cards the whole batch, and the v90/v94
  `verify_command` + sandbox block runs per member (I5, I2, I6/I7).
  `test_every_seed_names_only_tools_that_exist` could not have caught this —
  it cross-checks tool NAMES, not the arguments a seed instructs.
- **F2 compare-coding-engines** — the one ECC idea skep had no procedure for:
  four engines and a `coding_engine` key, and no evidence-based way to choose.
  Built on F1; scored on `reverification.confirmed` (the supervisor's re-run of
  the pinned command), never on `verification_outcome`, the worker's own claim —
  ranking engines on that measures confidence, not correctness (I2). Refuses to
  run without a pinned `verify_command`, since a CLI engine's built-in verify is
  `git diff --check` and every engine would "pass" (ADR 0047).
  `orchestrate-cli-coding-agent` gains the `engine:` argument and a pointer to
  its new sibling — the two answer different questions and each now says which.
- **F3 the two content gaps** — `security-audit` (repo-wide sweep anchored to
  real input paths, ranked by reachability, one dispatch per fix) and
  `production-readiness` (ship-or-block triage from local evidence only, I11;
  audits only boundaries that exist in the repo; refuses the uniformly-green
  report, I8). Distilled from ECC's `security-review` and `production-audit`
  into the house format, zero-grant, adapted-from credit in the description.

Shelf: 76 → 79 seeds. No new ADR — F1 is a bug inside ADR 0047's engine
surface, F2/F3 are shelf content under ADR 0043. Not exercised live: a real
three-engine bake-off (the per-member engine thread-through is unit-tested;
`claude_code`/`codex` need their binaries and a pinned project).

## v99 (2026-07-27, F1–F3)

**The index is a map, not a manual** (found while checking v98's acceptance;
plan `plans/v99/README.md`). The Queen's prompt showed **20 of 91 skills** and
the fresh-chat floor measured **27,345 of its 27,500 pin** — 155 chars of
headroom, about one schema property. Both are the same mistake: the prompt
spent its budget on *depth* (truncated prose about a few things) where it
should spend it on *coverage* (the existence of every thing). A name the model
never sees cannot be asked about; `describe_tools` / `view_skill` make depth
one call away, but nothing makes an unlisted name discoverable.

- **F1 the tool index** — 13,019 → 8,351 chars, all 112 tools kept. Summaries
  were 60% of the block: `PROPOSE ` opened 67 descriptions and a
  `(requires …confirmation…)` clause appeared in 64, so the 80-char cap
  truncated mid-boilerplate. Now four mechanical rules, no per-tool curation:
  `*` marks `MUTATING_TOOL_NAMES` membership with the legend stated once;
  core tools render name-only (their full schema is in the same request —
  zero loss, provably); a gloss whose tokens are a subset of the name's is
  dropped; gloss 80 → 44 chars and arg lists cap at 6 with `…`. The marker is
  *more* honest than the prose: six tools in that set never said PROPOSE, so
  the old index under-reported the confirmation path (I8). The legend
  deliberately says "cards UNLESS project policy auto-allows" — `read_file` is
  in the set and routinely auto-allowed.
- **F2 the skill index** — every template by name, no cap, no overflow line:
  1,645 chars for all 91 against 2,121 for 20. The cap traded coverage for
  depth and lost both; every v98 seed sat behind "… and 71 more". Comma
  packing is safe for skill names and was measured and REJECTED for tools,
  whose arg lists contain commas.
- **F3 the ratchet** — floor pin 27.5KB → 23KB, the first downward move after
  five upward ones (25 → 26 → 26.5 → 27 → 27.5), plus the reach block
  13.5KB → 9KB. Measured floor is now 22,677.

Net: the fresh-chat floor drops 4,668 chars (17%) while the model sees 91
skills instead of 20. No new ADR — this is v74-F3's progressive-disclosure
design carried through to its encoding. Not exercised live: how the small
Queen routes with names-only skills (the field test is whether `view_skill`
call rate rises).

## v100 (2026-07-28, F1–F10)

**Three sources, one round, and the field test found the rest** (plan
`plans/v100/README.md`). F1–F5 were planned: the four ECC ideas v98 cut for
size, and skep's own open R13. F6–F8 were reconstructed from the audit trail of
two dead `claude_code` runs. F9 and F10 were found by running v100's own
acceptance against the live operator home — the round auditing itself.

The field-test story is one story. Two runs on `skep-benchmarks` failed within
five seconds of each other at ~180s, reporting `agent exited 1`. Claude Code was
blamed for two days. The truth was three defects stacked:

- **F6 the proxy was cutting the wire.** `_TUNNEL_TIMEOUT = 120.0` was applied
  with `settimeout` to both ends of a CONNECT tunnel — but that bounds a single
  `recv`, not an idle connection. HTTP is request/response, so the
  client→upstream direction is silent for the whole of every response; 120s in,
  the forward pipe raised `TimeoutError`, set the shared `done`, and closed the
  tunnel **while the response was still streaming**, under `except OSError:
  pass` so nothing recorded it. An undocumented 120-second ceiling on any single
  model response. Now `_TUNNEL_POLL = 5.0`: a timeout means "nothing to relay
  yet", every other `OSError` still tears down, and the idle direction retires
  in ≤5s instead of ≤120s — better shutdown latency from the same edit. The
  plain-HTTP path had the same defect with a worse constant (10s, silently
  TRUNCATING the body) and is fixed with it; patching only the path the field
  test named would have left its sibling broken.
- **F7 the resolved allowlist was a list of characters.** `default_network` was
  stored double-encoded — a JSON *string* — so `list(policy["default_network"])`
  iterated it into 28 single characters. Every real host in the resolved list
  came from somewhere else, and the two the operator configured
  (`youtube.com`, `www.youtube.com`) were **never granted** since 2026-07-26.
  Two unguarded boundaries, one edit each: `update_policy` now validates both
  list fields (the REST route is typed, but the chat `set_policy` tool hands it
  raw args), and `policy_view` guards like its neighbours. Deliberately no
  `json.loads` on read — repairing malformed authorization data is how a
  validator becomes a parser (I5). Nothing widened: `domain_allowed` needs an
  exact match, so the characters never matched a host. The damage was to the
  truth of the record, which is the product.
- **F8 `agent exited 1` threw away the agent's own words.** `_run` captured
  stdout, stderr and both tails into the event log and the adapter dropped them
  at the one place the operator reads. `details` now carries a 200-char tail,
  stderr-first with a stdout fallback — `claude --print` reports API errors on
  stdout, which is exactly why this cost two days.

Then the acceptance went to run itself and found two more:

- **F9** No surface but chat could re-dispatch on a named engine (`skep run` has
  no `--engine`; `POST /api/runs` never passed the field `submit_run` has taken
  since v90), and moving the engine onto the project dropped the
  `verify_command` pin — which **no CLI or REST surface could set**, while
  `policy_resolver.py:543` refuses to run an external engine without one. A
  refusal naming a way forward the operator did not have. `--verify-command` on
  `project setup`/`preview`, `engine` on the REST dispatch; every wall stays in
  the resolver.
- **F10** `project setup` on an existing project **re-installed** it: bindings
  wiped and re-added, policy rebuilt from phase defaults that cover four keys.
  Every other key an operator ever set was silently destroyed by a re-run that
  changed one flag. v24-F4 wrote the right rule for seeded templates and it
  never reached the project's own record. Now: stored → phase defaults →
  explicit overrides, and bindings replace only the kinds supplied.

**Acceptance, live.** Run `019fa8de` — the brief that died twice at ~180s — ran
**555.6 seconds** on `claude_code` and exited 0, re-verified against the pinned
`python3 verify_plan.py`, and auto-landed on `skep/maintain`. A dispatched run's
`task.json` now carries seven real hosts and zero characters. A forced failure
reports `agent exited 127: [Errno 2] No such file or directory: 'aider'`.

The planned half:

- **F1–F4 the shelf**, 79 → 83 seeds: `audit-an-agent-stack` (audit the
  assembled payload and one reconstructed run, not the templates),
  `write-an-adr`, `council` (three lenses briefed to disagree, one verdict), and
  `postmortem-a-run` — which is F6–F8 written as a procedure. Skill index
  1,422 → 1,485 chars against its 2,400 bound; the fresh-chat floor stays inside
  v99-F3's 23KB pin.
- **F5 closes R13.** A pack declaring `self_test:` now has that command RUN, in
  a sandboxed no-network script run, extracted at the same
  `.skep-skill/<pack_id>/` path a real run uses, printing the forge's own
  evidence line so `trial_verdict` reads it unchanged. A pack declaring nothing
  still promotes on the syntax smoke and the evidence says `level: "syntax"` —
  the gap closes either by running the check or by saying plainly that none ran.
  The v36-F3 shadow-permissions guard caught the first attempt building its own
  `Permissions`; the trial now requests and the resolver decides.

No new ADR — ADR 0045's ceiling bullet is amended (R13 is its own named upgrade
path), F1–F4 are shelf content under ADR 0043, and F6–F10 are bug fixes to
existing decisions. Not exercised live: a real self-test pack promotion end to
end (the harness is executed for real in tests; the dispatch seam is stubbed).

## v101 (2026-07-29, F1–F14; F15/F16 landed in v106)

The caste roster becomes a registry (ADR 0049): the contract owns the
names, `castes.py` owns routing and description, a test pins the two sets
equal, and every surface reads the registry. The verifier and reviewer
castes become real workers; the store records `worker_kind` and the
RESOLVED `coding_engine` on every run (I8). The UI gains its type/spacing
scale, accessibility floor, two breakpoints and the `.chip` primitive,
all linted; `GET /api/workers` and the Assign panel expose the whole
roster; the Queen's dispatch enums generate from it. Setup writes the
slug binding it always implied, and an un-runnable inferred pin is
refused by the same host probes that seeded it (F14).

**Part E (F15 tool-call pairing, F16 the close_pr card) entered the plan
mid-round and did NOT ship with v101** — recorded complete by mistake,
found by the 2026-07-29 audit, landed as v106-F4/F5. The plan's own words
for F15: "the one fix here that should not be dropped."

## v103 (2026-07-29, F1–F5)

The git surface round. Mid-turn steering is queued with a receipt instead
of discarded (and the queue state's declaration order is pinned — a TDZ
stopped the chat rendering entirely once). `merge_branch` lands
supervisor-side, carded, refusing the default branch, conflict-aborting
in a temp worktree. **F3 closed a live hole:** `git merge`, `rebase`,
`cherry-pick`, `revert` and `reset --hard` were never denied to workers —
one broad allowlist entry would have let a worker merge another branch
into its worktree, and the patch (diffing against the baseline) would
land that work under the wrong approval. Denied with the rest, before
the verify fast-path and every grant lane; the Queen is bound by the same
predicates; stored grants are swept, not grandfathered.

## v104 (2026-07-29, F0–F5, ADR 0050)

One verb, three faces. The surface-parity gate
(`tests/supervisor/test_surface_parity.py`) turns "someone will notice
the next chat-only verb in a field test" into "the next gap fails a
gate" — on first run it reported 35 of 74 mutating verbs faceless, not
the estimated fifteen. `skep branch`, `skep pr`, and `skep repo refresh`
close the git family's operator half; two REST routes give the web UI
branch create/merge; every remaining chat-only verb sits in `CHAT_ONLY`
with a written reason. ADR 0050 makes it a rule: a new mutating verb
ships with its operator face or its recorded exemption.

## v105 (2026-07-29, F1)

The conversation continues when the work does: a completed run's owning
chat gets one unattended, read-only continuation turn — fact-seeded,
once per run, mutations card instead of executing (the pinned test
proves a scripted dispatch_run becomes a CARD). `continue_chat_after_run`
opts out.

## v106 (2026-07-29, F1–F12)

The audit round — seeded not by a field test but by a nine-agent
plan-vs-code audit of v85–v105 plus a read-only reconstruction of the
live home's week. The code was faithful; the felt breakage was
operational. F1 gives per-run toolchain state a writable home inside the
sandbox wall (`CLAUDE_CONFIG_DIR`, `npm_config_cache` →
`<workspace>/.toolchain/`) — Claude Code's Bash tool and npm both died
against a read-only $HOME. F2 heartbeats through long shell commands
(three workers killed at exactly 3×10s mid-npm-install). F3 stops
"could not re-verify" covering for "ran and FAILED", puts the
unconfirmed verdict on the pending approval card, and makes an
`unavailable` outcome ride the ready-to-land line. F4/F5 land v101's
Part E. F6 re-clocks card timeouts to operator absence. F7 gives
run_code a 600s ceiling. F8 teaches doctor to name umbrella and dead
registrations. F9 adds yarn's registry to node-dev and stops the UI
404-polling diffs that don't exist. F10 writes the four tests the plans
named and nobody wrote — its CLI-reference drift gate flagged eight
undocumented command groups on first run. F11 implements v90's unkept
visibility clauses (grant tier + time on the receipt, blocked
auto-apply reasons on the audit trail, the ADR 0046 amendment). F12 is
this file catching up.

## v107 — the kept worktree (2026-08-03)

The first public-tree plan round, seeded entirely by the 2026-08-03
dogfood arc (the day the v106 port shipped as 1.0.2 and skep started
fixing skep with Claude Code as its own worker). F1: failed and
unconfirmed-completed runs keep their worktree — the keep answer for a
completed run is deferred until re-verification writes the confirmed
bit; a failed run's retry resumes into its warm tree (no checkpoint
needed — the tree IS the value); a 24h TTL sweep on the ticker collects
what nobody resumes. F2: diagnose_run — one bounded, always-carded
command inside a kept worktree, sandboxed like re-verification, with a
REST face (POST /api/runs/{id}/diagnose); the Queen finally gets "re-run
the failing test, show me" without a terminal. F3: TMPDIR moves inside
the wall (workspace .toolchain/tmp for workers, a worktree-local dir for
re-verify) — the "~184 tests blocked by network isolation" was a
misdiagnosis, bwrap already brings loopback up; the nested-bwrap /tmp
mask was the whole story, and with it fixed the skep suite passes inside
the sandbox, making G10 confirmable for skep-shaped repos. Plus the
re-run now gets the run's own wall-clock budget (the flat 300s cap timed
out healthy 10-minute suites), and the plugin scratch copy learned to
exclude bookkeeping dirs. Recorded seed: macOS seatbelt's bare
(deny network*) blocks loopback binds — unverified from a Linux host.

## v108 — the provider shelf (2026-08-05)

Hermes parity as data: the operator's archived assistant reached ~28
model providers; skep's registry (v14) reached four — and recon showed
the registry itself was a facade (no surface could write a profile,
allowed_network_hosts was stored but read by nothing, and every
inference path collapsed onto the single llm-secret). F1 fixes the six
truths the plumbing was lying about: the probe bridge gains anthropic
(profiles could never probe healthy), phantom `gemini` leaves the
vocabulary, the profile host list actually reaches the v19-F2 egress
merge, the reviewer caste stops starving (the merge gate reads
needs_provider), registry api_key_env gets the v48-F2 name guard, and
the protocol vocabulary derives from ONE Literal
(test_protocol_vocabulary pins every surface). F2 gives the registry
its operator face — add/use/remove as one actions.py verb each with
CLI, REST, and carded chat forms (ADR 0050); `use` writes through to
the saved assistant config so activation is something the Queen
actually speaks. F5/F6 add the two missing wire protocols: the OpenAI
Responses API and AWS Bedrock Converse with a hand-rolled, test-vector-
pinned SigV4 (no boto3) plus a binary eventstream decoder. F3 ships the
preset catalog (~30 rows mined from the operator's own ~/.hermes:
OpenRouter, DeepSeek, GLM, Kimi, MiniMax, Copilot, Bedrock, Google via
its OpenAI-compatible endpoint, …) — every registration prints its
egress truth, provenance lands as source=preset:<id>, azure demands its
per-resource URL instead of guessing. F4 ends the one-secret era:
per-profile 0600 key files (env NAME → own file → legacy) honored by
chat, workers, probes, and /api/llm/models, with stdin-only CLI entry
and a write-only key route. F7 automates the one subscription auth that
needs no borrowed identity — the GitHub Copilot token exchange (your
own GitHub token becomes a short-lived in-memory bearer). F8 adds
`skep provider login`: RFC 8628 device flow with the OPERATOR'S client
id — skep ships none, ever (ADR 0051). F9 is the face polish: a preset
picker in both LLM forms, docs, and this entry. The chat floor pin
moved 24KB → 24.5KB for the four registry verbs + two protocol enums.

## v109 — one question asked once (2026-08-05)

Seeded by the 2026-08-03 blog-post session read straight out of the
store: two byte-equivalent land_run cards confirmed 67 s apart, three
approval surfaces for one landing with no shared words, three fresh
dispatches for one fix-chain while the v107 kept-tree machinery sat
unused — and, found in the same rows, `cd <repo> && git checkout … &&
sed -i …` executed from chat with exit 0. F1: guards judge SEGMENTS,
not lines — command lines split at shell operators, `bash -c` payloads
and `env` prefixes unwrap, and the deny fires on any segment, on both
lanes (the same shape also closed the worker's wrapped-git and
verify-fast-path holes); what cannot be statically read fails closed on
the persist/worker lanes and cards on the queen lane. F2: proposal-time
dedup — an identical pending proposal hands the model the pending card
with the protocol spelled out; a changed proposal for the same subject
supersedes honestly; gate mirrors and operator cards are never
superseded by a model proposal. F3: landing approvals are titled
`land "<brief>" → <branch>`, review cards headline the reason instead
of a UUID, and land_run's result says done-means-done. F4/F5: a
per-project uv/npm cache mounted through the sandbox wall (re-verify
primes against the same cache; grandchild processes finally inherit
TMPDIR/UV_CACHE_DIR). F6: dispatch surfaces hint at kept worktrees
before a fresh dispatch redoes the work. F7/F8: approve-and-remember
reaches network hosts (project `default_network`, allow-host faces),
and the ledger closes its loop — the Nth identical approval carries a
nudge, GET /api/ledger/suggestions derives the standing offers on read,
remembering through any door marks every matching ledger row. F9: the
Policies workspace shows every tier, learned/session rules become
listable and revocable (carded from chat, with an honest narrowing risk
line), and RSoP provenance answers "who decided each key" on project
detail. F10: the catastrophic floor — rm on roots, mkfs, dd onto
devices, shutdown, fork bombs — refused everywhere with one laugh and
one honest line, never allowlistable, learnable, or grandfathered, on
the same only-ever-grows footing as the git guards. The fresh-chat
floor re-measured to 24.5KB (the round paid byte-for-byte first: F7
folded its tool into allow_command_review, F6 trimmed). Also: the main
branch arrived with a failing release-checklist pin from the README
front-page rewrite — repaired first, per the no-pre-existing-failures
rule.

## v110 — the fleet sync verb (2026-08-05)

A requested-capability round, not a field failure: the operator's
machines converge through an apiary-style sync script (publish +
converge — commit, rebase, push, then pull and re-run installers), and
the apiary README's growth path had already named the missing face,
"a `skep sync` verb wrapping what bootstrap.sh does, once the shape
settles." The shape that respects the invariants: the command is PINNED
from the terminal only (`skep sync --set`, the settings table), and the
model can propose running it, never choose it — a chat tool that
accepted a command argument would have been a shadow run_shell without
the Queen's git guard, the exact lane I4 forbids. F1: `sync_fleet` in
serve/actions.py runs the pin supervisor-side on diagnose_run's bounded
shape (/bin/sh -c, PATH/HOME/USER/LOGNAME/SSH_AUTH_SOCK env, tail-capped
capture, timeout as exit_code=-1), records `fleet_sync_state` (I8),
refuses unpinned naming the fix (I9); faces: `skep sync`
(--set/--show/--clear/--timeout, docs/cli-reference.md) and read-only
GET /api/sync — deliberately no POST, running goes through the carded
path or the operator's own argv. F2: the chat face — a zero-argument
proposal carded as a publishing risk beside push_branch, deck-proposable,
web-UI-only (absent from CHANNEL_CONFIRMABLE_ACTIONS), /sync in both
decks in lockstep with card notes stating the exact pinned command and
the last outcome; an args-cannot-steer test pins that a model-authored
{"command": …} still runs the pin verbatim. Floor cost: one 58-char
index line; the ≤24500 pin holds.
