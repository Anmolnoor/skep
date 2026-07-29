# agent-task-contract — Spec Draft v0.1
Date: 2026-06-10
Status: Draft (implements decisions Q2/Q3/Q4/Q5/Q7/Q8/G5 from the 2026-06-10 decision record)
License: Apache-2.0 (per G1)
Consumers: the skep supervisor and its `coding_worker` (worker). Both run this package's
compatibility tests + golden fixtures in CI.
Design rule: **small and stable.** Anything not needed by v1–v2 is reserved, not specified.
---
## 1. Versioning (G5)
- `contract_version`: semver string, present in **every** envelope and every event. v0.1 at draft.
- Supervisor declares a supported range (e.g. `>=0.1,<0.2`). On dispatch, if the worker's
  reported `contract_version` is outside the range, the supervisor **rejects dispatch** with a
  doctor-style error: expected range, worker version, remediation hint.
- Workers reject task envelopes with a major version they don't support, emitting a single
  `task.rejected` terminal event.
- Schema changes: additive optional fields = minor bump. Field removal/rename/semantic change =
  major bump. Golden fixtures are regenerated on every bump and both repos' CI must pass them.
## 2. Identity (Q7)
- Supervisor mints `trace_id` and `task_id` (UUIDv7 recommended — time-sortable).
- Worker stamps both on every NDJSON event and on its internal session/step records.
- One namespace end-to-end. No worker-minted correlation IDs.
## 3. Task envelope — `CodingWorkerTask` (Q4, Q5, G2)
```python
class Permissions(BaseModel):
    read: list[str]              # paths, workspace-relative or absolute; v1: ["workspace"]
    write: list[str]             # v1: ["workspace"]
    network: bool = False        # v1: always False
    env_allowlist: list[str]     # exact env var names the worker process receives (G2)
class Budget(BaseModel):
    wall_clock_seconds: int      # supervisor backstop deadline derives from this
    max_iterations: int          # bounded replan ceiling
    max_actions: int
    max_provider_calls: int
class CodingWorkerTask(BaseModel):
    contract_version: str
    task_id: str
    trace_id: str
    worker_kind: Literal["coding"]
    workspace: str               # path to the supervisor-created git worktree (Q5)
    instructions: str
    permissions: Permissions
    budget: Budget
    resume_of: str | None = None # task_id of a prior task this re-runs (Q8 v1 stop-and-rerun)
    approval_verdict: ApprovalVerdict | None = None  # reserved; populated on v2 resume
```
Notes:
- The supervisor creates the worktree, writes `task.json` into it, and spawns the worker with
  `env` built strictly from `env_allowlist` (never inherited). This is a v1 acceptance criterion.
- `resume_of` gives v1's stop-and-rerun a first-class audit link; v2 reuses the same fields for
  true resume with zero schema change.
Forward-compatibility commitments (D1/D2 from the decision record — design constraints today,
fields tomorrow):
- **`permissions.network` will evolve from `bool` to a domain allowlist** (`list[str]`, where
  `false` ≡ `[]`). Planned minor bump (target v0.2). Consequence now: Q1's sandbox enforcement
  must be designed around *allowlists*, not an on/off switch, even though v1 only ever passes
  "deny all." First consumer: the dependency/audit bot (U1) needing package registries + GitHub
  API.
- **`worker_kind` will widen from `Literal["coding"]` to an open caste registry** —
  `coding`, `audit`, `document`, `curator`, and future castes. The envelope, event stream,
  budgets, states, and evidence rules are caste-independent by construction; only the
  capability set varies per caste. Validators must treat unknown `worker_kind` values as
  "reject dispatch" (supervisor) / "reject task" (worker), same doctor-style error as version
  skew — never silent acceptance.
## 4. Event stream (Q3, Q8)
NDJSON, one event per line, append-only, written by the worker to a path the supervisor knows
(`<workspace>/.events/<task_id>.ndjson` by default).
Common envelope on every event:
```python
class Event(BaseModel):
    contract_version: str
    event_id: str        # UUID, unique per event
    seq: int             # monotonic per task, starts at 1, no gaps required but never repeats
    task_id: str
    trace_id: str
    ts: str              # RFC 3339 UTC
    type: EventType
    payload: dict        # type-specific, schema per type below
```
Idempotency rule (Q8): consumers deduplicate on `event_id` and order on `seq`. Re-delivery and
re-reads are always safe. A consumer that has seen `seq=n` ignores any event with `seq<=n` whose
`event_id` it has already recorded.
### Event types (v0.1 core set)
| type | payload (required keys) | emitted by |
|---|---|---|
| `task.start` | `worker_version`, `manifest_fingerprint` | worker |
| `heartbeat` | `phase` | worker, every N sec (default N=10) |
| `plan.created` | `steps: list[str]` | worker |
| `command.start` | `command`, `purpose` | worker |
| `command.result` | `command`, `exit_code`, `duration_ms`, `stdout_tail`, `stderr_tail` (redacted) | worker |
| `file.changed` | `path`, `change: created\|modified\|deleted` | worker |
| `verify.result` | `outcome` (see §6), `details` | worker |
| `approval.requested` | `action`, `reason` | worker (v1: immediately followed by terminal `task.pending_approval`) |
| `task.terminal` | `status` (see §5), `summary` | worker, or **synthesized by supervisor** on timeout/crash |
Supervisor-synthesized terminal events (worker dead or hung) use the same schema, with
`payload.synthesized: true`. A crashed worker cannot self-report; the supervisor backstop is
part of the contract, not an implementation detail.
### Death path (Q3)
- Worker emits `heartbeat` every N seconds (N in worker config, default 10; not a contract field).
- Supervisor enforces a wall-clock deadline derived from `budget.wall_clock_seconds` (+ grace).
- On breach: kill process tree → teardown → synthesize `task.terminal` with `status=worker_timeout`.
- Missing heartbeats for 3×N with a live process = hung: same treatment, `status=worker_timeout`,
  `payload.reason="heartbeat_lost"`.
- Process exit without a terminal event = crash: synthesize `status=worker_crashed` with exit code.
- Orphan cleanup (stale worktrees, zombie processes, dangling `.events` files) runs at supervisor
  startup and after every terminal event.
## 5. Task states (Q8)
```
created → dispatched → running → ┬→ completed
                                 ├→ failed
                                 ├→ pending_approval   (suspended; v1 reaches it only as a
                                 │                      terminal stop — resume = new task with
                                 │                      resume_of; v2: true suspend/resume)
                                 ├→ worker_timeout
                                 ├→ worker_crashed
                                 └→ rejected            (version skew or invalid envelope)
```
The enum is frozen at v0.1. v2 changes *transitions* (pending_approval becomes resumable), never
the states themselves.
## 6. Verification taxonomy (kept from the predecessor prototype, unchanged)
`outcome ∈ { passed, failed, unavailable, not_attempted }`
- `passed` / `failed`: a verification command ran; exit code decided it.
- `unavailable`: verification impossible in this environment (missing tool, no test target).
- `not_attempted`: worker terminated before the verify step.
A result may claim `status=completed` **only** with `verification.outcome=passed` or an explicit
per-task waiver flag (not in v0.1; tasks without verifiable outcomes should say so in
instructions and expect `unavailable`).
## 7. Result envelope — `CodingWorkerResult` (Q5)
```python
class CommandRecord(BaseModel):
    command: str
    exit_code: int
    purpose: str
class Verification(BaseModel):
    outcome: Literal["passed", "failed", "unavailable", "not_attempted"]
    details: str
class Artifact(BaseModel):
    kind: Literal["event_log", "patch", "file"]
    path: str
    sha256: str
class Usage(BaseModel):              # reserved at v0.1 (G8); populated from v2
    provider_calls: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
class CodingWorkerResult(BaseModel):
    contract_version: str
    task_id: str
    trace_id: str
    status: TaskState                # terminal states only
    summary: str
    changed_files: list[str]
    commands: list[CommandRecord]
    verification: Verification
    artifacts: list[Artifact]        # MUST include the event_log and (if changes) the patch
    usage: Usage | None = None
    risk_flags: list[str] = []
```
The patch artifact is the only mutation that leaves the worktree (Q5). **Applying the patch is
the approval action**, performed supervisor-side. The worker never commits, never pushes; the
worktree is torn down after artifact collection.
## 8. Golden fixtures (Q2)
The package ships, and both repos' CI must pass:
1. `fixtures/task_minimal.json` — smallest valid task envelope.
2. `fixtures/events_happy.ndjson` — full happy-path stream, start→plan→commands→verify→terminal.
3. `fixtures/events_timeout.ndjson` — heartbeats stop, supervisor-synthesized terminal.
4. `fixtures/events_crash.ndjson` — stream ends mid-run, synthesized `worker_crashed`.
5. `fixtures/events_pending_approval.ndjson` — `approval.requested` → terminal `pending_approval`.
6. `fixtures/result_completed.json`, `fixtures/result_failed.json`.
Compatibility test = parse every fixture with the current models; round-trip serialize; byte-stable
field set per version. A fixture that fails to parse blocks release of either repo.
## 9. Out of scope at v0.1 — reserved, deliberately unspecified
Named reservations (each maps to a north-star use case in the decision record):
| Reserved item | Future shape | Target | Serves |
|---|---|---|---|
| Network allowlist | `permissions.network: list[str]` of domains; sandbox enforces per-domain | v0.2 field, v2 enforcement | U1, U2 |
| Worker castes | `worker_kind` open registry: `audit`, `document`, `curator`, … | v0.2 | U1, U2, U4 |
| Workflow templates | Parameterized recorded task definitions, Queen-side; instantiate into ordinary task envelopes — **no new envelope type** | v3.5 (user-authored), v4 (learned/promoted) | U2 |
| Auto-approval rules | Queen-side declarative policy granting autonomy (auto-apply patch when conditions hold); audit-recorded with the rule that fired; zero contract change | v2 design, v3 active | U1 |
| Memory proposals | `memory_proposals` block in result envelope (curator caste output) | v3 | U4 |
| Usage enforcement | `Usage` recorded from v2 (G8); per-day/repo accumulation Queen-side; enforcement later | v2+ | U1 |
| Secret references | Short-lived scoped credentials resolved at spawn (v3 form of G2) | v3 | all |
| Live-stream transport | v1 reads the NDJSON file post-exit; v2 adds tailing — no schema change | v2 | all |
| Multi-worker batching | Parallel dispatch semantics; gated on G4 (storage under concurrency) | v3 | U1 |
Rule for all of the above: when a reserved item lands, it lands as an **additive minor bump**
with regenerated golden fixtures. If it can't land additively, it was specified wrong here —
re-open the design before touching the major version.
---
*Acceptance: this draft satisfies the §6 bar of the planning doc ("contract spec draft exists
reflecting Q2/Q3/Q4/Q5/Q7/Q8/G5"). Next: scaffold the package, generate JSON Schema, write the
six fixtures, wire both CIs.*
