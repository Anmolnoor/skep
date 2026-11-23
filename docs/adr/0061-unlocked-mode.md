# ADR 0061 — unlocked mode on policy v3

**Status:** accepted (ES-F140–ES-F147); amended by D42 (ES-F311): unlocked
never answers a network host — see "Amendment: D42" at the end; and by D46
and D47 (2026-11-21): unlocked answers a role's catch-all ask, and
onboarding offers the mode — see the amendments after it; and on
2026-11-23 (phase-01 1.1): a CLI `skep run` answers the same gates — see
"Delta 2026-11-23" at the end.
**Replaces:** the August `unlocked` branch's ADR (numbered 0052 there; the
design archive keeps it as accepted ADR 9999). **Builds on:** ADR 0059
(policy v3), ADR 0048 (policy groups), ADR 0047 (engines), ADR 0044
(outbound content). **Invariants:** I1, I2, I4, I5, I6, I8, I12, I13.

## What the August branch promised

One boolean project-policy key, `unlocked`, the top rung of the per-project
trust ramp (I6: never a global toggle). Its justification was the sandbox:
it removed exactly the asks the walls contain.

| Ask | Under unlocked (August) |
|---|---|
| Dispatch cards (dispatch_run, run_code, quick_edit, batch members) | ran in-turn, reason `dispatch.auto_allowed.unlocked` |
| Worker escalations (shell, capability, network host) | answered themselves through the click path, actor `auto:unlocked`, once (never the session tier). The network-host half never bought egress to any host and is withdrawn (D42, below): with egress, only a host the run lists in `network_ask` escalates to a card, any other is refused; unlocked answers none |
| Patch landing | zero clicks: unlocked implied `auto_apply_verified_patch` (G10 with the pinned verify, `skep/*` only) |
| Outbound content (open_pr, merge_pr, pushes, messenger sends) | still carded |
| Policy and trust changes | still carded |
| The Queen's host shell (run_shell, start_process) | unchanged: it runs on the real host, so the sandbox argument does not apply |
| Hard git denies, sudo, catastrophic floor | still denied |
| Explicit run overrides (engine, network, budgets) | still carded |

On and off: `skep project setup --unlocked / --no-unlocked`, chat
`setup_project unlocked=`, REST `policy_overrides`. Saving it on refused
without a pinned `verify_command` (I2) and a usable sandbox (I12); every
unlocked run was forced into sandbox mode. Visibility: `skep doctor`, an RSoP
provenance label, a UI pill. A set_project_phase re-derive dropped it.

The last commit on that branch (an unreviewed WIP rescued from another
machine) added `SKEP_UNLOCKED`, a process-wide environment switch that made
every project unlocked, opened run egress to `*`, and logged "would have"
verdicts. That is the global don't-ask toggle I6 forbids, and it labelled
itself "never production". It is dropped.

## What still makes sense under policy v3

ADR 0059's first rule is one `decide()`. The August code had four private
answers (a branch in the dispatch posture, a resolver override, a done
callback that resumed any gate, a sandbox coercion). On v3:

- **Dispatch in-turn** stays a posture, not a `decide()` verdict: `dispatch`
  is no policy scope, and the dispatch gate is already the project's posture
  (ADR 0059 P3-6). Unlocked composes the posture keys it implies
  (`auto_dispatch_allowed`, `default_execution_mode=sandbox`,
  `auto_apply_verified_patch`) in the resolver, labelled `unlocked` in RSoP.
  Taint still asks a tainted chat's dispatch (`taint/untrusted-action`) and a
  HIGH card (explicit engine, `network '*'`) is never auto-allowed.
- **Worker escalations** become a `decide()` question. A gate answers itself
  only when `decide()` allows the request it asks about; the allow comes from
  one new step inside `decide()`, the unlocked layer.
- **Landing** is unchanged v90-F4 machinery: G10 with the pinned verify,
  `skep/*` only, `main` never moves (I1, I2).
- **The Queen's host shell** stays out, now by construction: the Queen
  decides with no repo, so the unlocked layer (a run-lane step) never sees
  her requests.
- **The phase drop** holds: `set_project_phase` re-derives policy from the
  phase defaults and keeps only the verify pin. A repeated `project setup`
  carries every stored key since v100-F10, so it keeps `unlocked` too.

## Decision

### The unlocked layer inside decide()

`decide(req, …, sandboxed=False)`. `sandboxed` is a fact about the request,
like `read_only`: the request comes from a worker running inside the sandbox
walls. Only the worker-gate auto-answer passes it, and only for a run whose
execution mode is `sandbox` on a home whose sandbox is on. REST, the CLI, the
Claude Code hook and ACP never pass it: the hook and ACP govern agents on the
real host.

After the run lane answers and before the role, the chain and taint:

- it applies only on the run lane (`stack.run`), for a project whose
  effective policy has `unlocked: true` (`Stack.unlocked`), to a sandboxed
  `shell.run` request (`UNLOCKED_REQUESTS`; `network.connect` was the
  second until D42 removed it); nothing else is ever the layer's (a write
  outside the workspace, a network host keep their verdict);
- it turns an **ask that is only a default** into `allow`, labelled
  `unlocked:<project>/zero-ask`: `default/unmatched`, or a catch-all `*` ask
  (ADR 0059 D2: "a `*` ask is a default, not a ceiling");
- it never touches a deny, a named ask, the floor (checked before any lane:
  the git floor and the secret floor), the baseline guard (a zero-state or
  tampered home still asks), a line skep cannot read (`sh -c '…'`, `$(…)`)
  or a background line (`… &`);
- the operator's standing denies and named asks bind too. The run lane is
  compiled from knobs and holds allows only; a baseline `deny curl` or a
  named `ask make deploy` lives on the operator lane (setup document,
  overlay, locked baseline — D2: a template's denies and named asks are a
  ceiling). Where one names any part of the line, the step declines and the
  run lane's ask stands, for a human to answer;
- the gate's answer resumes the run with the command as an approved argv
  **prefix** (the worker contract's matcher), so the step also declines a
  command whose grant would cover a line a deny or named ask names: `make`
  (it would also run `make deploy`) and `env` or `timeout 5` (they would
  launch `curl`).

These rules read command words, as every shell rule does. A line that hides
what it runs from a word matcher — an interpreter given code (`python3 -c
'…'`, a script the worker wrote), or a spelling the rule does not cover
(`make -C . deploy` for a named `make deploy`) — reads as unmatched and is
answered. On an unlocked project a shell deny or named ask is a stop for the
words it names, not a boundary; the sandbox is the boundary. The git,
secret and catastrophic floors still hold on every line, because the worker's
own capability layer re-checks them before any grant.

Everything after it still applies: a role's denies and asks (including its
catch-all ask, which only a human grant answers, P3-2), the delegation cap (a
delegated run's chain may turn the allow into a deferred ask a human answers,
D3), and taint (D5). The worker-gate auto-answer decides with no chat, so on
that path taint is the dispatch's: a tainted chat's dispatch already asks
(`taint/untrusted-action`). HIGH is a card property and `vet_write` a
write-time check; neither is reached by a worker gate.

### Worker gates

On a finished run in `pending_approval` (the serve dispatcher's done
callback), if the run's project is unlocked, the run is in sandbox mode, and
every request the gate names is allowed by `decide(…, sandboxed=True)` with
the run's own role and chain: the gate resolves through `resume_past_gate`,
actor `auto:unlocked`, **once** (no session grant written, B39). Each request
is a line in the decision log (I13) with actor `auto:unlocked`. The gates it
understands: `shell.run` (each listed command). A `network.fetch` /
`network.read` gate is decided as `network.connect` and, since D42, always
stays a human card, as does anything else — a git capability, a gate with no
command. Serve's done callback and a foreground CLI `skep run` both run the
auto-answer (Delta 2026-11-23); REST, the hook and ACP never do.

### Switching it on and off

- The key: `unlocked` (bool), project layer only, on the trust ramp, never
  groupable and never global (the registry says so; I6).
- Preconditions when it is saved on: a pinned `verify_command` (I2), a
  usable sandbox (I12), and a home whose baseline guard binds nothing (an
  approved, untampered baseline or a legacy home: on a zero-state home the
  layer could answer nothing, and a mode that silently does nothing is a lie,
  I8).
- Faces (ADR 0050): `skep project setup --unlocked / --no-unlocked`, REST
  `POST /api/projects` `policy_overrides`, chat `setup_project unlocked=`.
- Turning it on from chat is a **HIGH** card (web UI or CLI only, never a
  messenger). Turning it off narrows and is an ordinary card.
- `copy_project_policy` never copies it: each project climbs the ramp on its
  own card.

### Visibility

`skep doctor` names every unlocked project; RSoP labels the keys it implied
`unlocked`; the dispatch receipt says `dispatch.auto_allowed.unlocked`; a
worker gate's resolution reads `auto:unlocked`; every auto verdict is a
`decide()` row naming `unlocked:<project>/zero-ask`.

## Changed or dropped from August, and why

| August | Now | Why |
|---|---|---|
| Every escalation answered | only default asks a sandboxed worker raises | v3: named asks and denies bind; the floor is first |
| Answered on any home | needs an approved baseline | the zero-state guard asks before any lane |
| Answered regardless of the dispatching chat | a delegated run's chain still caps | D3: a delegator's ask is a human's |
| Ticker sweep for recovered and scheduler gates | done callback only | a recovered run's gate stays a card: fails toward asking |
| `run_task` / spawner refusals | not needed | main already refuses a sandbox run with no usable sandbox (ES-F1, B47) |
| UI pill | not built | doctor, RSoP, receipts and the decision log carry it; the pill is a UI round |
| `SKEP_UNLOCKED`, `unlocked-report` | dropped | a global toggle (I6) |

## Open for the operator

1. ~~Should unlocked also answer a delegating role's catch-all ask (a builder
   chat's dispatch)? Today it does not: P3-2 says only a human's grant
   answers a role's `*` ask, and the operator's HIGH card turning unlocked on
   is a decision about the project, not about the chat's role. The effect: a
   gate on a run dispatched from a builder chat still cards when the builder
   role would itself ask; runs dispatched from the CLI, REST, a `/command` or
   a schedule (no chain) are fully zero-ask.~~ Decided by D46 (below): it
   does.
2. ~~The network-host trade from August stands: a worker can reach any host
   no rule names.~~ Decided by D42 (below): it does not.

## Amendment: D42 — unlocked never widens the network (ES-F311)

The 2026-11-14 field test (L4) found the host half three ways at once: the
worker refused an unnamed host flatly whenever the run had egress
(`capability.deny.network_host_not_allowed`), a run with no egress raised a
host gate that the serve's auto-answer approved, and doctor said hosts no
rule names were not asked about. Operator decision D42, security first: a
model reaching hosts no rule names is the main exfiltration path, so
unlocked mode never widens the network to them, on any path.

- `network.connect` leaves `UNLOCKED_REQUESTS`: `decide(…, sandboxed=True)`
  keeps a host's ask, so a host gate stays a human card on serve exactly as
  it does on a CLI run. A host no rule names is refused (a run with egress)
  or asked (a run without), never auto-approved.
- Unlocked still answers default command asks, on serve-dispatched runs and
  (Delta 2026-11-23) a foreground CLI `skep run`.
- The D34 listing of hosts the mode auto-approved (doctor, a run's
  `unlocked_hosts`) can only find rows written before D42, and doctor says
  they are historical.

## Amendment: D46 — unlocked answers a role's catch-all ask (open item 1)

Council round 2 (2026-11-21) read the operator's own store: about ten cards
per chat, more than 80% confirmed, and a builder chat's dispatches still
carding on an unlocked project because of open item 1. Operator decision
D46: the operator's explicit unlock of a project answers a role's catch-all
`*` ask the way a human's grant does (ADR 0059 decision #1: a catch-all ask
is a default, not a ceiling).

- In `decide()`, an allow from the unlocked step counts as granted for
  `catch_all_asks_only`: the run's own role and every delegator in the chain
  (each decides the same request, still `sandboxed`) let it stand when all
  they say about the line is a catch-all ask (`shell-learnable`,
  `shell-gated`).
- A role's deny or named ask still binds, as do the operator's (`_unlocked`),
  the floor, the baseline guard and taint. D42 is untouched: a network host
  is never the step's.
- Only sandboxed runs reach the step: serve-dispatched ones and, since the
  Delta 2026-11-23, a foreground CLI `skep run`; REST, the hook and ACP
  never pass `sandboxed`.

I6 holds: the mode is still the per-project ramp the operator climbs with
an explicit action, and no model ever answers a card.

## Amendment: D47 — onboarding offers the mode, and defaults to `builder`

The same council found that pressing Enter through `skep onboard` locked
the `assistant` template (plan decision #4, least privilege), which denies
code edits: a new user's first coding task could not run. Operator decision
D47:

- `skep onboard`, the web `/setup` page and `POST /api/onboard/template`
  default to `builder`. `builder` still asks on every command it does not
  name; the sandbox, not the template, is what contains a worker.
- After step ④, onboarding asks once, in plain words, whether skep may work
  in the work directory without asking. A typed `y` (or the web's Turn on
  button, `POST /api/onboard/unlocked {"on": true, "path": <the path GET
  /api/onboard/unlocked returned>}`) turns `unlocked` on for that one
  project through project setup, under the same save-time guard (pinned
  non-vacuous `verify_command`, usable sandbox, approved baseline). The
  answer names the folder it was shown: a work directory or binding changed
  since is refused. Enter means no; each folder's answer is kept in
  `onboard.json` and not asked again for that folder.
- Nothing is offered before step ④ is locked, nor for a project that binds
  anything but the folder shown (another path or any `repo_slug`: the
  unlock is the project's); `skep project setup --unlocked` decides for all
  of it. Step ② re-pointing a project id (a folder's basename) at a folder
  it does not already bind drops the project's unlock.
- `--yes` never answers it: it takes defaults, and working without asking is
  not one. `skep onboard --unlocked` is the explicit flag.
- It is not offered when step ③ chose no sandbox or the guard would refuse;
  the reason is printed.

This is I6 as written: the operator climbs the per-project ramp explicitly,
once, at setup. Nothing turns it on for every project or by default.

## Delta 2026-11-23 — CLI `skep run` parity (phase-01 1.1)

"A CLI `skep run` on an unlocked project stops at every gate" (Worker gates,
D46) no longer holds. The decision is now one shared helper,
`unlocked_gate_allowed` (`serve/actions.py`): the same preconditions (run in
`pending_approval`, execution mode `sandbox` on a home whose sandbox is on,
project unlocked, a `shell.run` gate only), the same `decide(…,
sandboxed=True)` under the run's role and chain, one decision-log row per
request with actor `auto:unlocked` and the run's `task_id`. Serve's done
callback resumes an allowed gate through `resume_past_gate`, as before; a
CLI `skep run` resumes it in-process through the same resume `skep review
--approve` takes, then asks the helper again for the successor's gate. One
decision path, no second allow list (I5).

- **The cap.** One CLI `skep run` answers at most `MAX_UNLOCKED_RESUMES` (10,
  `cli_cmds.py`) gates in a row; a run still gating after that stops at its
  card and says so (`answered 10 gates in a row (the cap); this one waits
  for you`). Serve has no such loop: each resume is a new dispatch.
- **What still cards, on both faces:** any request `decide` does not allow —
  the floor (`git push`, `sudo`, `env git push`, … with zero operator shell
  rules too; the worker's own shell floor denies them first, upstream), an
  opaque shell `_widens` keeps asking (`sh -c "…"`, `$(…)`, `&`), the
  operator's or a role's deny or named ask, the baseline guard; every host
  gate (D42); any gate that is not `shell.run`; a locked project; a
  workspace-mode run or an unsandboxed home. `skep review --approve` and a
  serve approve still resume only what a human answered.
- **Review round 1.** Every request a batch gate names is decided and logged,
  the ones after a refusal too, and the gate answers only if all allow (I13).
  A CLI run that chose narrower grants than its project's policy (`--minimal`,
  start minimal, an unbound template) is never auto-answered: the step reloads
  the project's policy, which those runs opted out of (I5, I6). A resume
  refused before it starts (no auth env, engine absent, no `task.json`) prints
  the reason and leaves the gate a card, unclaimed, as serve does. The resumed
  successor keeps the audited run's pinned verify, caste, protocol and engine.
