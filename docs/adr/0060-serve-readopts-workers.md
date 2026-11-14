# ADR 0060 — serve re-adopts a live worker after a restart

**Status:** accepted (edge sweep decision D2, round R4; ES-F130–ES-F136;
Linux bubblewrap ES-F210–ES-F212).
**Related:** ADR 0006 (supervisor re-verification), ADR 0008 (suspend and
resume), ADR 0011 (network proxy), ADR 0017 (the serve daemon). Invariants
touched: I1, I2, I8, I12.

## Question

Restarting `skep serve` killed every run in flight. On startup
`recover_interrupted_runs` killed any worker still running (ES-F5, B10) and
marked the run `worker_crashed`; the worktree survived only when a version-2
checkpoint existed. React-style runs write one every round; plan-style runs
only at approval gates, so a plan run lost its worktree and its work (field
test finding L18). The operator chose (D2): **serve re-adopts the running
worker** and supervises it to completion as if it had never restarted —
with one deliberate exception (D35, section 3): a re-adopted run never
auto-applies; it lands by a human approval.

## Decision

### 1. How a worker outlives its parent

The spawner starts every worker with `start_new_session=True`: the worker
leads its own session and process group, so the terminal's SIGHUP and a
signal to serve's group do not reach it. Its stdout and stderr go to
`audit/<task>/worker.log`, not a pipe, so serve's exit does not SIGPIPE it.
The worker is reparented to init (launchd on macOS, systemd or pid 1 on
Linux), which reaps it when it exits.

- **macOS (Seatbelt).** `sandbox-exec` execs the worker in place: same pid,
  same start time, no signal handling of its own. A sandboxed worker
  survives serve's exit. Its only egress is `localhost:<proxy port>`, which
  is baked into its Seatbelt profile and its environment.
- **Linux (bubblewrap).** bwrap keeps `--die-with-parent` (ES-F2, B50) in
  every invocation; what changes is who its parent is (ES-F210–ES-F212).
  Dropping the flag would be unsafe: bwrap runs the sandbox in a session of
  its own (`--new-session`) under a pid namespace whose init is bwrap's
  child, so `kill_session` on bwrap's group would kill only the outer bwrap
  and leave the sandbox running, reparented to init, with nobody to reap or
  bound it (measured on bwrap 0.13). With re-adoption on, the spawner starts
  a **keeper** in bwrap's place: a `python -c` one-liner that runs bwrap as
  its child, waits for it and exits with its status. The keeper is what
  serve spawns (`start_new_session`), so its pid and start time are what the
  run records and what a new serve verifies; pids inside the namespace are
  never recorded. Serve's death no longer reaches bwrap. Killing the
  keeper's session (a breach, `kill_session`) kills the keeper and bwrap
  together, bwrap's death still takes the sandbox down, and if the keeper
  alone dies bwrap dies with it — the keeper lives exactly as long as the
  sandbox. The keeper also bounds a sandbox no serve comes back for: it
  kills bwrap at the run's wall clock + grace + 60 s, a minute after the
  monitor would have, so a live monitor always acts first. With
  `--no-readopt` there is no keeper: bwrap is serve's child and dies with
  it, as before this ADR.

  The proxy bridge: bwrap used to bind the host proxy's socket *file* into
  the sandbox, which pins the inode of a listener that dies with serve; a
  re-adopting serve's proxy listens on a new inode at the same path. bwrap
  now binds the socket's private directory (it holds only that socket)
  read-only, and the shim connects through it on every connection, so it
  reaches whichever proxy listens there now. The run records the socket
  path; the new serve removes the stale socket file and listens on the same
  path and the same port. The netns is untouched by a restart (the shim
  inside keeps listening on the sandbox loopback), and the R5 socket masks
  are unchanged.
- **podman** never carried `--die-with-parent`: the container runs under
  conmon, outside serve's process tree, and the podman client serve spawns
  leads its own session, so a podman worker already outlives serve and is
  adopted like a workspace-mode one. No test covers podman across a restart.

### 2. What serve records, and how it knows the worker is the same one

At spawn the run's `running` transition now reads
`pid <N> started <lstart> [proxy port <P>] [proxy socket <path>]` (the
socket only on Linux, section 1). `<lstart>` is the process start
time from `ps -o lstart=` in the C locale and UTC — the same helper the CLI
landing claim already uses (W27, `store._process_started`); no second one.
A pid is only a number and is reused; the pair (pid, start time) is not.
Serve also records itself as the run's supervisor (`runs.supervisor_owner`,
the W27 `{"pid", "started"}` owner of the dispatching process).

On startup a run is re-adoptable only when **all** hold: the recorded start
time exists (rows written before this change have none and are killed as
before), the process with that pid has that start time (`owner_alive`), and
its command line still names the task (the ES-F5 check, kept). A live
process with the recorded pid but another start time is a stranger: it is
neither killed nor adopted, and the run takes the dead-worker path.

### 3. Supervising a process that is not our child

There is no `waitpid` for a non-child. `watch_worker` takes an
`AdoptedWorker` in place of the `Popen`: liveness is the owner check
(polled at most twice a second, it runs `ps`), and the exit code is
unknown, so the verdict carries `exit_code=None`, as recovery already did.
Everything else is the existing loop: the event log under
`.events/<task>.ndjson` is the heartbeat, the result envelope under
`results/<task>/result.json` is the claim, and a breach kills the session
with the existing `kill_session`. After the worker exits, the run takes the
same ingest → G10 re-verification → approval path recovery already uses
(I1, I2), and its finish reaches the dispatching chat as a live run's does:
the one-line notice and the Queen's continuation turn (v105-F1).

**Landing is the exception (D35).** Auto-apply rules are not replayed on
this path, as for every recovered run: an adopted run lands only by a human
approval, even on a project whose maintain lane or unlocked mode would have
auto-applied it. The reason: after a restart the chain of trust is
*reconstructed, not witnessed*. The new serve rebuilt supervision from the
record — pid and start time, the event log, the result envelope — and nobody
watched the gap between the old serve's death and the adoption; G10 still
re-verifies, but the only lane that lands without a human (I1) is the one
place a reconstructed chain is not enough, so a human verdict lands it
(I1, I13). The run's audit trail says so: an `auto_apply_blocked` transition
names the restart and the `skep review <task> --approve` that lands it.

The filtering proxy lived in the old serve. Before watching, the new serve
starts a proxy with the run's own allowlist and asks (from `task.json` in
the audit dir) **on the recorded port**. If that port cannot be bound, the
worker is killed and the run crashes honestly: its profile still points at
that port, and whatever holds it now is not skep's filter (I12).

The supervision runs on a daemon thread, so serve's startup is not held for
the length of a run; if serve dies again the worker survives again and the
next serve adopts it again.

### 4. Budgets across the restart

The wall-clock deadline counts from the worker's own start time (`lstart`,
one-second resolution), not from adoption: a restart never extends a
budget. A worker already past its budget plus grace is killed on adoption.
The heartbeat window starts fresh at adoption (the event file's growth is
measured from then); a worker silent for 3× the heartbeat after adoption is
killed as before.

### 5. Two serves on one home

There is no cross-process serve lock. Recovery now skips any run whose
recorded supervisor is still alive — a second serve no longer kills the
workers of a first one that is still running (it used to). Recovery takes
each run, live worker or dead, with a compare-and-set on
`runs.supervisor_owner` in one SQLite `UPDATE`: of two serves starting
together, exactly one wins; the loser leaves the run alone. A run is named
with its supervisor when its row is created, so a serve starting while a
dispatch is still setting the run up leaves it alone too.

### 6. Kill switch

`skep serve --no-readopt` (or `SKEP_READOPT_WORKERS=0` in the environment
or in `serve.env`) restores the old behaviour: a live worker is killed with
its session and the run is recovered as before. On Linux it also drops the
keeper (section 1), so a sandboxed worker dies with the serve that spawned
it; the variable (not the flag, which is serve's) does the same for runs a
`skep run` or `skep team` process spawns. The skip of runs whose
supervisor is still alive (5) stays on either way: killing a live sibling's
worker was never intended.

## Consequences

- A serve restart no longer costs a plan run its work, sandboxed or not, on
  macOS and on Linux (section 1).
- A pid is never trusted alone; a reused pid is left alone (I8, I12).
- A worker runs without a filtering proxy between serve's death and the next
  serve's start; its egress is refused for that window, as before.
- If no serve comes back, nothing enforces the budget of a macOS worker, a
  workspace-mode worker or a podman one; it runs until it ends on its own. A
  Linux bubblewrap worker is killed by its keeper a minute past its wall
  clock + grace.
- A service manager that stops a whole cgroup on restart (systemd's default
  `KillMode=control-group`) kills the workers with serve, and there is
  nothing left to re-adopt; skep ships no unit file.
- Rows written before this change carry no start time and are killed, as
  before. No migration rewrites them.
