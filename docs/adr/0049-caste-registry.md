# 0049 — the caste roster is a registry (v101)

## Status

Accepted (v101-F1).

## Question

The question is not "may we add castes" — it is **where the roster lives**.

The contract has declared seven worker kinds since v17/v51
(`KNOWN_WORKER_KINDS`). The table that turns one of those names into a
process was a dict literal inside `build_config` (`cli_cmds.py:163-172`)
listing five. Every other surface kept its own copy:

| where | list |
|---|---|
| `worker_contract/task.py:9-17` | coding, audit, curator, document, researcher, script, **verifier** |
| `cli_cmds.py:163-172` | audit, curator, document, researcher, script (+ coding = default) |
| `app.js:3334` (Assign) | coding, audit |
| `app.js:3972` (template form) | coding, audit |
| `tools.py:788` / `tools.py:1560` | coding, audit / coding, audit, document |

Five lists, already diverged. The cost is not untidiness: `config.command_for`
falls back to the *coding* worker for any unregistered name (`config.py:57-59`),
so `verifier` — declared in the contract, routable nowhere — meant a verifier
dispatch silently ran a coding worker under a verifier's name for eighty-odd
versions. `researcher`, `script` and `curator` were routable but reachable from
no operator surface except the CLI.

This is the v42 / v51-F3 lesson, which `engines.py:1-8` states in its own
opening — *code that exists but is never registered behaves exactly as if it
does not exist* — and which was then not applied to castes.

## Decision

**The contract owns the names; the supervisor owns routing and description.**

- `KNOWN_WORKER_KINDS` stays authoritative for which caste names exist.
  `castes.py` is the supervisor-side registry: one `Caste` per name carrying
  its argv, a one-line operator-facing `summary`, and three honest facts —
  `lands`, `needs_provider`, `needs_network`.
- A test pins the two sets equal, so declaring a caste without registering it
  fails the gates instead of failing a field run. Nothing imports across the
  line — the pin is a test, not an import cycle.
- Every operator surface — CLI, REST, UI, chat tool schema — reads the
  registry. None carries its own copy.
- `resolve_caste` refuses an unknown name naming the known set, the
  `resolve_engine` shape. A silent fallback to the coding worker is the exact
  defect this ADR ends (I9).
- `coding` holds an EMPTY argv and stays out of the routing table, deferring to
  `config.command_for`. That is what `SKEP_WORKER_CMD`, `--worker-cmd` and the
  test fake worker override, and a registry must not quietly take it away —
  the `BUILTIN_ENGINE` precedent (`engines.py:47-51`).

**The registry grants nothing.** It maps a name to an argv and a description.
Permissions, budgets, execution mode and engine still resolve exactly once, in
`resolve_run_policy` (I5). A caste describes work, not authority; it is not a
policy tier and must never become one.

## Consequences

- One place to add a caste, and adding it to the contract without registering
  it is now a gate failure rather than a silent misroute.
- Every surface's caste list moves in lockstep with the roster, which is what
  F9–F12 build on. The Queen's tool enums stop being a hand-kept subset.
- **A registry entry can still point at nothing.** The test checks the module
  is importable, which is why `verifier` is registered as a declared hole
  (`UNIMPLEMENTED_CASTES`) rather than as an entry — F2 writes the worker and
  empties the constant. A named hole is honest; a routing entry to a missing
  module would be the same defect wearing a registry (I8).
- **The schedules `caste` enum is deliberately NOT wired** (`tools.py:1653`).
  It mixes worker castes with supervisor-side schedule kinds and carries a real
  name collision: a schedule of kind `script` runs a shell command on the
  *supervisor host*, which is not the `script` worker caste. Feeding the
  registry into that enum would silently redefine an existing verb. Recorded
  here so the next reader does not "fix" it by accident.
- The three fact flags are descriptive, not enforcement. `needs_network` does
  not grant network; the run's allowlist still comes from policy. If a later
  round wants them to drive resolution, that is a new decision — and it would
  need to argue why a caste may influence authority, which this ADR says it may
  not.

## Rejected

- **A caste plugin directory** (operator-dropped castes). That is the
  plugin/skill-pack system wearing a second name, and it would need its own
  trust ladder (ADR 0045's whole argument). YAGNI until a field test asks.
- **Deriving the roster by scanning `skep/workers/`.** Implicit registration is
  what `SKEP_WORKER_CMD` already covers for the one caste where it is wanted;
  everywhere else, a caste appearing because someone added a file is exactly
  the silent behaviour this ADR exists to end.
