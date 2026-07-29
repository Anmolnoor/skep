# ADR 0003 — Store carve-out boundary (Q6-A)

Date: 2026-06-11 · Status: accepted

## Context

An earlier supervisor prototype had useful durable-state concepts but too much
surface area to import wholesale. The new supervisor needs only the
bridge-touched concepts. Importing the full prototype would inherit unrelated
risks; rewriting everything risks re-learning solved problems.

## Decision

Carve out by **copy-and-upgrade, never import**: the earlier implementation is a
read-only donor of concepts, and every carved module is reborn at Skep's quality
bar (hermetic tests, strict typing, single-writer discipline). The supervisor
talks to workers only through `task.json` / NDJSON / `result.json`.

## What was carved (donor concept → new module)

| Donor concept | New module (skep/supervisor/) | Upgrade applied |
|---|---|---|
| Run/task store + state transitions (`honeycomb.py`, `data_plane/repositories/sqlite_durable_state.py`) | `store.py` (runs, append-only `run_transitions`) | SQLite WAL, single writer (G4), strict typing, hermetic tests |
| HITL approval queue (`approvals` table + review flow) | `store.py` (approvals: FIFO, verdict + actor + timestamp, final once resolved) | resolution finality enforced; wired to `pending_approval` terminal state |
| Honeycomb append-only event/audit writer (`.honeycomb/events/<trace_id>.jsonl`) | `store.py` events table + `ingest.py` audit copies | dedupe on `event_id`, order on `seq` (spec §4); artifacts stored by sha256 with hash verification |
| Personal-mode status (`personal_mode.py`, queen_api status) | `cli_cmds.cmd_status_personal` | CLI-first (G6); reads only the run store |
| UUIDv7/trace identity conventions | `ids.py` | supervisor-minted, one namespace end-to-end (Q7) |

Nothing else crossed the boundary. The pre-existing `skep` shell
(setup/doctor/start/dashboard) predates this project and is brought to the bar
only as later stages touch it.

## Consequences

- The supervisor has zero dependency on the earlier prototype; AGPL questions
  stay within this repo (see ADR 0004).
- The slice can grow: future carve-outs follow the same copy-and-upgrade rule
  and get a row in the table above.
