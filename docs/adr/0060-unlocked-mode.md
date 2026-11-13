# ADR 0060 — unlocked mode on policy v3

**Status:** proposed (round ES-F140..ES-F149, branch `es-unlocked-v3`).
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
| Worker escalations (shell, capability, network host) | answered themselves through the click path, actor `auto:unlocked`, once (never the session tier) |
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
  request;
- it turns an **ask that is only a default** into `allow`, labelled
  `unlocked:<project>/zero-ask`: `default/unmatched`, or a catch-all `*` ask
  (ADR 0059 D2: "a `*` ask is a default, not a ceiling");
- it never touches a deny, a named ask, the floor (checked before any lane:
  the git floor and the secret floor), the baseline guard (a zero-state or
  tampered home still asks), or a line skep cannot read (`sh -c '…'`, `$(…)`).

Everything after it still applies: a role's denies and asks (including its
catch-all ask, which only a human grant answers, P3-2), the delegation cap (a
delegated run's chain may turn the allow into a deferred ask a human answers,
D3), and taint (D5). HIGH is a card property and `vet_write` a write-time
check; neither is reached by a worker gate.

### Worker gates

On a finished run in `pending_approval` (the serve dispatcher's done
callback), if the run's project is unlocked, the run is in sandbox mode, and
every request the gate names is allowed by `decide(…, sandboxed=True)` with
the run's own role and chain: the gate resolves through `resume_past_gate`,
actor `auto:unlocked`, **once** (no session grant written, B39). Each request
is a line in the decision log (I13) with actor `auto:unlocked`. The gates it
understands: `shell.run` (each listed command) and `network.fetch` /
`network.read` (the host, decided as `network.connect`). Anything else — a
git capability, a gate with no command — stays a human card.

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

1. Should unlocked also answer a delegating role's catch-all ask (a builder
   chat's dispatch)? Today it does not: P3-2 says only a human's grant
   answers a role's `*` ask, and the operator's HIGH card turning unlocked on
   is a decision about the project, not about the chat's role. The effect: a
   gate on a run dispatched from a builder chat still cards when the builder
   role would itself ask; runs dispatched from the CLI, REST, a `/command` or
   a schedule (no chain) are fully zero-ask.
2. The network-host trade from August stands: a worker can reach any host no
   rule names, each a logged verdict, with credentials stripped by the env
   allowlist (G2) and nothing landing outside G10 + `skep/*`.
