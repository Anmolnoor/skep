# ADR 0017 — `skep serve`: the API daemon, the ticker, and mutable config (v5)

Date: 2026-06-12 · Status: accepted

## Context

Through v4 skep is complete but headless: operable only from a CLI, on a host
with the full toolchain. v5 needed a browser to drive it and a container to
host it — and both share one spine: a long-running process exposing the core
over HTTP. The decisive audit fact: the CLI was already a thin argparse wrapper
over importable core functions (`dispatch.run_task`, `RunStore`,
`scheduler.run_due`, `apply.apply_patch_on_branch`, …), so an API layer could
sit beside the CLI without moving any logic. Options considered in the v5 RFC:
a new FastAPI daemon (chosen), retrofitting the pre-v2 personal-mode
`server.py` (would entangle eras), or a two-service API+frontend split
(orchestration cost with no payoff for a single operator).

## Decision

**One FastAPI daemon (`serve/` package), handlers thin enough to read in one
breath: validate → call the core → JSON.** The CLI stays; both surfaces call
the same functions. Zero contract change — the worker boundary does not move.

### 1. Async dispatch, because `run_task` is synchronous end-to-end

`POST /api/runs` never calls `run_task()` inline — that would hang the request
for the whole run. The dispatcher submits to a small thread pool (the
scheduler's own pattern) and answers **202 + task id**. The id exists before
the run does any work via the one additive hook v5 adds to existing code:
`run_task(on_run_created=…)`, fired right after `create_run`, a no-op when
unset.

### 2. SSE from the audit trail (and its one subtlety)

`GET /api/runs/{id}/events?stream=1` is Server-Sent Events, not WebSockets:
the flow is one-way and SSE auto-reconnects over plain HTTP. Subtlety: events
reach SQLite only at ingest (end of run); during the run they exist in the
worktree's NDJSON stream. The endpoint tails the live file while the run is
active and falls back to the store after ingest — same events, same seq order,
deduplicated by `read_event_log`. The stream closes itself on a terminal
state; ingest writes events *before* the terminal transition, so a terminal
read implies every event has been emitted.

### 3. The in-process ticker replaces cron

Cron does not translate into a container. A daemon-owned thread calls the same
`run_due` on an interval re-read from settings every cycle (a policy edit
re-paces it without a restart), killable on shutdown, never concurrent with
itself (one sequential loop). `skep tick` remains for CLI/cron users.

### 4. Mutable, persisted settings — the frozen config stays frozen

The UI must edit policy at runtime, but the core relies on `SupervisorConfig`
immutability. Resolution: a `settings` key/value table on the single-writer
store, and a `ConfigHolder` whose write path is *load settings → build a new
frozen instance over the startup base → swap the reference*. Every dispatch
reads `holder.current`, so an edit applies to the next run, never a running
one. `PUT /api/policy` edits auto-approval, the worker command, default
network/env allowlists, and the ticker interval.

### 5. Auth: one token, header for machines, cookie for the browser

First boot mints a random token, persists it under the supervisor home (0600,
rides the data volume), and prints it to the boot log. Middleware gates every
`/api/*` route with a constant-time comparison. The cookie path is not a
convenience: the browser's `EventSource` cannot set headers, so SSE
authenticates by cookie while curl/CLI use `X-Skep-Token` or `Bearer`. Static
UI assets are public; they contain no secrets.

### 6. Repos by URL

`POST /api/repos {url}` clones into `SKEP_HOME/repos/<slug>`; runs and
schedules then name repos by slug. This is what makes "zero local
dependencies" true — host paths remain a documented dev-only route.

## Consequences

- The same gates guard both surfaces: G10 re-verification, the human approval
  gate (Q5 patch-as-approval, Q8 resume), and the D1 allowlist are enforced in
  the core the handlers call — the UI cannot reach around them.
- Approving a suspended run over HTTP dispatches the resume on the background
  pool and records the link in the same approval row the CLI would use.
- The skill test gate (`POST /api/skills/{name}/test`) runs a real worker and
  blocks its request; FastAPI's sync-handler threadpool keeps the daemon
  responsive. Acceptable for a single operator; revisit if it ever isn't.
- Proven offline by `make serve`: assign → SSE-stream → approve → patch on a
  branch → policy round-trip, first-party worker, zero secrets.
