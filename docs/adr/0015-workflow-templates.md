# ADR 0015 — Workflow templates: a filled template is just a normal task (v3.5)

Date: 2026-06-11 · Status: accepted

## Context

The decision record (U2 + the v3.5 row) calls for **workflow templates,
user-authored** — "the v4 skill registry's simplest form: user-authored
templates, no learning loop, no promotion pipeline; it can land earlier (v3.5)."
The recurring U1 audit bot in v3 already re-types the same instructions, caste,
network scope and budget at every `schedule add` / `run`. A template makes that
recipe a named, parameterized, reusable thing — the precursor to v4's
draft→tested→approved pipeline for *generated* workflows.

The open question was where templates sit relative to the contract. The hard
constraint, set by the prompt and by the whole architecture: **a template must
not change the contract.** If instantiating a template needed a schema bump, the
boundary would have leaked Queen-side concerns into the worker protocol.

## Decision

**A workflow template is a Queen-side recipe; instantiating it produces the exact
arguments a completely normal `CodingWorkerTask` is minted from.** Nothing about
the contract, the worker, the events, or the evidence changes — a filled-in
template *is* a regular task.

- *Model* (`templates.py`). `WorkflowTemplate` = name, description, an instruction
  template, declared parameters, and the v3 knobs already on a task: caste
  (`worker_kind`), optional target repo/ref, network allowlist, env allowlist, and
  budget. `instantiate(template, params, repo=…, ref=…)` validates the params,
  substitutes them into the instructions, and returns a `TemplateInstance` — the
  literal arguments `run_task` already takes. The "mints a completely normal task"
  claim is a test, not a comment: the instance round-trips through `mint_task` and
  the contract validator unchanged.
- *Parameters.* Required unless they declare a default. Substitution uses
  **`{{name}}`** double-brace placeholders, deliberately *not* `$name` or
  `{name}` — instructions handed to a coding worker routinely contain shell
  snippets (`$VAR`) and braces, and a template syntax that collided with them
  would be a footgun. Authoring-time validation rejects an instruction that
  references an undeclared parameter, a duplicate parameter, or an unknown caste.
- *Storage* (`store.py`). A `templates` table alongside `schedules`, same
  single-writer store (G4), same INSERT-OR-REPLACE-by-name grain. The simplest
  storage consistent with v3 — no new database, no new process.
- *Authoring two ways.* By file (`--from audit.toml` / `.json`, parsed with stdlib
  `tomllib`/`json`, validated) for full fidelity including parameter descriptions;
  or by CLI flags (`skep template add NAME --instructions … --param p`) for the
  quick case. `list` / `show` / `remove` round out CRUD.
- *Schedules bind to templates.* `skep schedule add JOB REPO --template T --param
  k=v` stores a **live reference** (`template_name` + `params`) plus a display
  snapshot. `skep tick` re-instantiates the *current* template each time, so a
  later edit — and the template's own budget — take effect. The schedule then
  dispatches through the unchanged `run_due` → `run_task` spine, inheriting the
  whole boundary (sandbox, D1, G10, D3). A bound template that has been deleted is
  a recorded `dispatch_error` that still advances the schedule (the existing
  resilience rule).

## Consequences

- **Zero contract change, verified.** No `agent-task-contract` bump, no
  golden-fixture regen; the contract stays on v0.2. Templates are entirely
  Queen-side — exactly the layering U2/D-deltas promised.
- The v3 U1 nightly bot is now expressible as a recipe: author the audit template
  once, then *both* `skep run --template dep-audit` and `skep schedule add …
  --template dep-audit` mint normal audit tasks. Proven end-to-end through the real
  CLI in `make templates` (author once → run on demand AND bind to a schedule;
  both complete and are G10-confirmed).
- **Explicitly not built (it's v4, not v3.5):** no learning loop, no
  draft→tested→approved promotion pipeline, no auto-generation of templates. v3.5
  is the human-authored floor those build on.
- **Honest limits.** Parameters substitute into the instruction template only —
  not into caste/network/budget, which are fixed recipe knobs (a parameterized
  network scope can arrive additively if a use case needs it). The schedule's
  display snapshot (instructions/caste/network shown in `schedule list`) can drift
  from a later-edited template; the *run* always uses the live template, and the
  snapshot is labelled as provenance, not truth. A storage migration (`_migrate`)
  adds the two new schedule columns to an existing v3 database in place, since
  `CREATE TABLE IF NOT EXISTS` never alters a live table.
