# ADR 0010 — Storage gate: SQLite-WAL single writer (G4, v3 entry)

Date: 2026-06-11 · Status: accepted

## Context

Decision G4 made storage a **v3-entry gate**: before anything parallel ships,
choose between *SQLite-WAL with a single writer process* and *the roadmapped
Postgres move* — with the hard rule that **v3 may not start on concurrent JSONL
appends.** v3 introduces parallel dispatch (Stage F: more than one worker at a
time), so the durable store will, for the first time, take writes from several
threads at once. The gate has to be answered before that lands.

Two facts about the existing store framed the choice:

- It already keeps durable state in **SQLite (WAL mode)**, not JSONL appends. The
  per-worktree NDJSON event stream is the worker's transport; the supervisor
  ingests it into the `events` table (deduplicated on `event_id`). So the "no
  concurrent JSONL appends" rule was already satisfied — there is no shared
  append-only file in the write path.
- The store was written single-connection, single-thread (`v1`: one worker at a
  time). That connection was not safe to share across the threads parallel
  dispatch creates.

## Decision

**SQLite-WAL, single writer.** Not Postgres.

- *Rationale.* This is a personal supervisor on one machine. SQLite-WAL gives
  ACID transactions, non-blocking readers, and a single-file audit store with
  zero operational surface (no server, no port, no backup daemon). Postgres buys
  multi-host concurrency and write throughput this workload does not need; it
  would add a deployment dependency for no gain at v3's scale. Postgres stays on
  the roadmap for the day a hosted/multi-host offering exists (revisit with G1's
  licensing note), not before.

- *Implementation.* `RunStore` now holds one connection shared across dispatch
  threads (`check_same_thread=False`) and serialized by a single re-entrant lock
  (`_locked` wraps every public method). That lock **is** the "single writer
  process" the gate names: at most one statement/transaction touches the
  connection at a time, held only for the brief duration of each write, never
  across a worker's multi-second runtime. `PRAGMA busy_timeout=5000` makes a
  *cross-process* writer (e.g. a `skep status`/`skep tick` running beside a
  `skep run`, each with its own connection) wait its turn instead of raising
  `SQLITE_BUSY`. WAL keeps those other-process readers non-blocking.

## Consequences

- Parallel dispatch (Stage F) can share one store across a thread pool safely.
  Proven by `test_single_writer_survives_parallel_dispatch`: 12 threads issuing
  ~490 interleaved writes land with no corruption, no cross-task event leakage,
  correct per-task `seq` ordering, and no threading/`BUSY` errors.
  `test_two_connections_can_both_write` proves the cross-process WAL path.
- The lock serializes writes, so the store is not a throughput engine — correct
  for a personal fleet (a handful of workers), the wrong choice for a hosted
  multi-tenant service. That boundary is the Postgres trigger, recorded here so
  the next person knows what would move the line.
- No schema change, no contract change. The gate was a concurrency-safety
  decision about the *writer*, not the data model.
