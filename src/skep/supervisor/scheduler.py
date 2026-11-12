"""Stage E: recurring Queen-scheduled tasks (the "nightly" in U1).

skep is not a daemon. It persists schedule definitions and dispatches the ones
that are due when ``skep tick`` is invoked; an external cron / launchd supplies
the wakeup — the Unix way, and exactly the decision record's "a cron job checks my
GitHub projects." One tick dispatches every due schedule through the *same*
``run_task`` spine as a manual run, so scheduled work inherits the whole boundary:
contract, sandbox, network allowlist (D1), re-verification (G10), and — once
rules are configured — auto-approval (D3).
"""

from __future__ import annotations

import contextlib
import logging
import os
import select
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from skep.worker_contract import Permissions

from .autonomy import AutonomyDecision, project_policy_dispatch_decision
from .config import SupervisorConfig
from .contracts_io import DEFAULT_BUDGET
from .dispatch import run_task
from .ingest import IngestOutcome
from .nodes import OPS_INSPECT_CAPABILITIES, OPS_MUTATING_CAPABILITIES
from .policy_resolver import PolicyResolutionError, resolve_run_policy, run_policy_for_repo
from .providers import ProviderHealth, ProviderProfile, check_provider_health
from .spawner import build_worker_env
from .store import SCHEDULE_SUCCESS_STATES, RunStore, ScheduleRecord
from .templates import WorkflowTemplate, instantiate

logger = logging.getLogger("skep.scheduler")

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
# ES B110: the longest interval a schedule may have. `99999999999999d` was
# accepted and overflowed datetime at tick time; a year is a sane ceiling.
MAX_INTERVAL_SECONDS = 366 * 86400

# v44-F4: the agent-less cron lane (Hermes `--no-agent --script`).
SCRIPT_TIMEOUT_SECONDS = 120
SCRIPT_OUTPUT_CAP = 4000


# ES B111: the ONLY variables a script inherits. Never serve.env's keys,
# never a *KEY*/*SECRET*/*TOKEN* — the output reaches a chat and the provider.
SCRIPT_ENV_BASELINE = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TERM",
    "TMPDIR",
)
# ES B117: bytes kept from the pipe — enough for SCRIPT_OUTPUT_CAP chars of
# any UTF-8; the rest is read and dropped so the script never blocks on it.
_SCRIPT_READ_CAP = SCRIPT_OUTPUT_CAP * 4
# ES B120: process groups of the scripts running now, for kill_running_scripts.
_RUNNING_GROUPS: set[int] = set()
_RUNNING_LOCK = threading.Lock()


def script_env() -> dict[str, str]:
    """The environment a script schedule runs with — the scrubbed baseline
    present in this process, nothing else. The script card lists these
    names (P4-2), so the card and the run read one function."""
    return build_worker_env((), baseline=SCRIPT_ENV_BASELINE)


def kill_running_scripts() -> None:
    """ES B120: SIGKILL every running script's whole process group — serve
    shutdown calls this so a restart never leaves a script running with no
    supervisor."""
    with _RUNNING_LOCK:
        groups = list(_RUNNING_GROUPS)
    for group in groups:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(group, signal.SIGKILL)


def run_schedule_script(
    command: str, *, timeout_seconds: float = SCRIPT_TIMEOUT_SECONDS, stdin: str | None = None
) -> tuple[str, bool]:
    """Run a script schedule's command via ``sh -c``; (output, succeeded).

    ``stdin`` carries a chained schedule's last output (v53-F5) — data into
    an operator-vetted command, never a new command.

    SUPERVISOR-SIDE, with the operator's own filesystem standing — as trusted
    as the operator's crontab, which is why creation is operator-gated (see
    the tick branch). Like cron, it gets a minimal env (SCRIPT_ENV_BASELINE),
    not the daemon's. It runs in its own session, so the timeout kills the
    whole process group, not just ``sh``. Output is stdout+stderr, read as it
    streams, trimmed and capped, so a chatty script can flood neither a chat
    transcript nor the daemon's memory.
    """
    try:
        proc = subprocess.Popen(
            ["/bin/sh", "-c", command],
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=script_env(),
            start_new_session=True,
        )
    except OSError as exc:
        return (f"script failed to start: {exc}", False)
    with _RUNNING_LOCK:
        _RUNNING_GROUPS.add(proc.pid)
    kept = bytearray()
    truncated = timed_out = False
    try:
        if stdin is not None and proc.stdin is not None:
            # A chained output is already capped (≤ SCRIPT_OUTPUT_CAP), far
            # below a pipe buffer, so this write cannot block.
            with contextlib.suppress(BrokenPipeError):
                proc.stdin.write(stdin.encode())
            with contextlib.suppress(BrokenPipeError):
                proc.stdin.close()
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:  # select retries EINTR itself, so this is the deadline
                timed_out = True
                break
            chunk = os.read(fd, 65536)
            if not chunk:
                break  # EOF: every writer in the group closed the pipe
            room = _SCRIPT_READ_CAP - len(kept)
            if len(chunk) > room:
                truncated = True
            kept += chunk[: max(room, 0)]
        if not timed_out:
            try:
                proc.wait(timeout=max(deadline - time.monotonic(), 0))
            except subprocess.TimeoutExpired:
                timed_out = True
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            if timed_out or proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()
        with _RUNNING_LOCK:
            _RUNNING_GROUPS.discard(proc.pid)
    combined = kept.decode("utf-8", errors="replace").strip()
    if truncated or len(combined) > SCRIPT_OUTPUT_CAP:
        combined = combined[:SCRIPT_OUTPUT_CAP] + "\n… (truncated)"
    if timed_out:
        note = f"script timed out after {timeout_seconds:g}s"
        return (f"{combined}\n{note}" if combined else note, False)
    if proc.returncode != 0:
        # A failure always says something, even a mute one.
        return (f"{combined or '(no output)'}\n[exit {proc.returncode}]", False)
    # v51-F6: empty success returns "" so the tick can stay silent (watchdog).
    return (combined, True)


SCRIPT_RUNNING_STATE = "script_running"


def reconcile_interrupted_scripts(store: RunStore) -> list[str]:
    """ES B120: at serve start, a schedule still marked running was cut off
    by the last shutdown. Record that as a health failure; its slot already
    advanced, so it fires at the next one, not at once."""
    interrupted: list[str] = []
    for schedule in store.list_schedules():
        if schedule.last_state != SCRIPT_RUNNING_STATE:
            continue
        state = "script_interrupted: serve stopped mid-script; next run at its next slot"
        store.mark_schedule_ran(
            schedule.name,
            ran_at=schedule.last_run_at or now_ts(),
            next_run_at=schedule.next_run_at,
            task_id=None,
            state=state,
        )
        store.record_schedule_outcome(schedule.name, task_id=None, state=state)
        interrupted.append(schedule.name)
    return interrupted


def parse_interval(spec: str) -> int:
    """``'30s' / '5m' / '2h' / '1d'`` (or a bare integer of seconds) → seconds (> 0)."""
    text = spec.strip().lower()
    if not text:
        raise ValueError("empty interval")
    if text[-1] in _UNITS:
        value, factor = text[:-1], _UNITS[text[-1]]
    else:
        value, factor = text, 1
    try:
        amount = int(value)
    except ValueError as exc:
        raise ValueError(f"invalid interval {spec!r}: use e.g. 30s, 5m, 2h, 1d") from exc
    if amount <= 0:
        raise ValueError(f"interval must be positive, got {spec!r}")
    if amount * factor > MAX_INTERVAL_SECONDS:
        raise ValueError(f"interval must be at most 366d, got {spec!r}")
    return amount * factor


def _parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _fmt_ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def now_ts() -> str:
    return _fmt_ts(datetime.now(UTC))


def _start_slot(start_at: str | None) -> str | None:
    """ES B116: ``start_at`` → the store's ``…Z`` form, or ValueError.

    ``next_run_at`` is compared as a STRING, so a verbatim ``tomorrow`` sorted
    after every timestamp (never fired) and ``-08:00`` sorted 8 h early. A
    naive time is UTC — the documented posture."""
    if start_at is None or start_at == "":
        return None
    try:
        moment = datetime.fromisoformat(start_at.strip())
    except ValueError as exc:
        raise ValueError(
            f"invalid start_at {start_at!r}: use an ISO time, e.g. 2026-07-16T09:00:00Z"
        ) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return _fmt_ts(moment.astimezone(UTC))


# v14 Step 2: after this many consecutive failed ticks a schedule is auto-disabled
# (with a recorded reason) so a persistently broken config stops hot-looping.
MAX_SCHEDULE_CONSECUTIVE_FAILURES = 5


def next_run_after(reference: str, interval_seconds: int) -> str:
    """Next fire time = reference + interval (scheduled from the tick, so no catch-up storm)."""
    return _fmt_ts(_parse_ts(reference) + timedelta(seconds=interval_seconds))


def next_fire_times(
    start_at: str | None, interval_seconds: int, *, once: bool = False, count: int = 3
) -> list[str]:
    """The next ``count`` fire times of a schedule created now, by the
    scheduler's own rules: the first is ``start_at`` (normalised) or the next
    tick, each later one ``next_run_after`` the previous. A tick that runs
    late pushes the rest later by as much (no catch-up storm). ``once`` fires
    once. A ``start_at`` already past is due at once: the next tick fires it.
    ValueError on a bad ``start_at``."""
    now = now_ts()
    start = _start_slot(start_at)
    times = [start if start is not None and start > now else now]
    while not once and len(times) < count:
        times.append(next_run_after(times[-1], interval_seconds))
    return times


def make_schedule(
    *,
    name: str,
    repo: Path | str,
    instructions: str,
    interval_seconds: int,
    worker_kind: str = "coding",
    ref: str | None = None,
    network: Sequence[str] = (),
    env_allowlist: Sequence[str] = (),
    start_at: str | None = None,
    enabled: bool = True,
    chat_id: str | None = None,
    once: bool = False,
    chain: str | None = None,
) -> ScheduleRecord:
    created = now_ts()
    start = _start_slot(start_at)
    return ScheduleRecord(
        name=name,
        repo=str(repo),
        ref=ref,
        worker_kind=worker_kind,
        instructions=instructions,
        network=list(network),
        env_allowlist=list(env_allowlist),
        interval_seconds=interval_seconds,
        enabled=enabled,
        created_at=created,
        last_run_at=None,
        next_run_at=start or created,  # default: eligible on the next tick
        last_task_id=None,
        last_state=None,
        chat_id=chat_id,
        once=once,
        chain=chain,
    )


def make_template_schedule(
    *,
    name: str,
    template: WorkflowTemplate,
    params: dict[str, str],
    repo: Path | str,
    interval_seconds: int,
    ref: str | None = None,
    start_at: str | None = None,
    enabled: bool = True,
    chain: str | None = None,
) -> ScheduleRecord:
    """A schedule bound to a template (v3.5): "run template X with these params".

    The template is instantiated once here to (a) validate the params eagerly and
    (b) snapshot the filled instructions / caste / network / env for display. The
    binding is *live*: ``run_due`` re-instantiates the template at each tick, so
    later template edits (and the template's budget) take effect. ``repo`` is the
    schedule's target and overrides any repo pinned in the template.
    """
    instance = instantiate(template, params, repo=str(repo), ref=ref)
    created = now_ts()
    start = _start_slot(start_at)
    return ScheduleRecord(
        name=name,
        repo=instance.repo,
        ref=instance.ref,
        worker_kind=instance.worker_kind,
        instructions=instance.instructions,  # display snapshot; tick re-resolves live
        network=list(instance.permissions.network),
        env_allowlist=list(instance.permissions.env_allowlist),
        interval_seconds=interval_seconds,
        enabled=enabled,
        created_at=created,
        last_run_at=None,
        next_run_at=start or created,
        last_task_id=None,
        last_state=None,
        template_name=template.name,
        params=dict(params),
        chain=chain,
    )


@dataclass(frozen=True)
class TickResult:
    name: str
    task_id: str | None
    state: str


DispatchFn = Callable[..., IngestOutcome]


def _schedule_dispatch_decision(
    *, project_bound: bool, policy: dict[str, object] | None = None
) -> AutonomyDecision:
    if not project_bound:
        return AutonomyDecision(
            verdict="allow",
            reason="dispatch.allow.schedule_tick_resolved",
        )
    if policy is None:
        return AutonomyDecision(
            verdict="require_approval",
            reason="dispatch.require_approval.policy_resolution_failed",
        )
    return project_policy_dispatch_decision(
        policy=policy,
        requested_execution_mode=None,
        explicit_run_overrides=False,
    )


def ops_schedule_is_conservative(capability: str, *, dry_run: bool) -> bool:
    """v15 Step 4: an ops schedule may run unattended only when it is safe —
    a read-only inspection, or an explicit dry-run of a mutating capability.
    A non-dry-run mutating ops schedule (or a network probe) is never eligible
    to run without a human — it must be an approved, per-node action."""
    if capability in OPS_INSPECT_CAPABILITIES:
        return True
    if capability in OPS_MUTATING_CAPABILITIES:
        return dry_run
    return False


def run_provider_health_checks(
    store: RunStore,
    *,
    list_models: Callable[[ProviderProfile], list[str]],
    now: str | None = None,
) -> list[ProviderHealth]:
    """Probe every registered provider and record its health (v14 Step 4).

    Schedulable: call this from a cron/tick. Reaches only each provider's
    explicit endpoint host — no hidden network widening."""
    moment = now or now_ts()
    results: list[ProviderHealth] = []
    for profile in store.list_provider_profiles():
        health = check_provider_health(profile, list_models=list_models, now=moment)
        store.record_provider_health(health)
        results.append(health)
    return results


MAX_CHAIN_DEPTH = 3


def validate_chain(store: RunStore, *, name: str, chain: str | None) -> None:
    """v53-F5: a chain must name existing schedules, stay acyclic, and stop
    at depth 3 — deeper chains are a sign the user should write one script."""
    if chain is None:
        return
    seen = {name}
    current: str | None = chain
    depth = 0
    while current is not None:
        depth += 1
        if current in seen:
            raise ValueError(f"schedule chain contains a cycle at {current!r}")
        if depth > MAX_CHAIN_DEPTH:
            raise ValueError(
                f"schedule chains are capped at {MAX_CHAIN_DEPTH} levels — "
                "write one script that does everything instead"
            )
        record = store.get_schedule(current)
        if record is None:
            raise ValueError(f"chain names an unknown schedule {current!r}")
        seen.add(current)
        current = record.chain


def _chained_context(store: RunStore, schedule: ScheduleRecord) -> str | None:
    """The chained source's last stored output, when there is one."""
    if not schedule.chain:
        return None
    source = store.get_schedule(schedule.chain)
    if source is None or not source.last_output:
        return None
    return source.last_output


def _chain_prefixed(schedule: ScheduleRecord, context: str, instructions: str) -> str:
    """Labeled as CONTEXT, never as new instructions (the memory-injection
    posture, applied to ticks)."""
    return (
        f"[Context from schedule {schedule.chain!r}]:\n{context}\n\n"
        f"[Your instructions]:\n{instructions}"
    )


def _deliver_tick_text(
    store: RunStore,
    schedule: ScheduleRecord,
    text: str,
    notify: Callable[[str, str, str], None] | None,
    kind: str = "info",
    *,
    script_output: bool = False,
    context: str | None = None,
) -> None:
    """Deliver a repo-less tick's text: bound chat (+ best-effort outbound
    push, v44-F2) when it still exists, otherwise an inert note. The chat row
    is the durable copy; a push failure never breaks the tick. ``kind``
    (v78-F1) threads the delivery classification through to the push choke
    point, so a schedule-dispatched run's pending gate still reaches an
    'approvals'-level channel.

    ADR 0059 D5 (B26): a script's output (``script_output``, or a chained
    source's ``context``) is outside content — a user row from
    ``schedule:<name>``, replayed wrapped, setting the chat's ``untrusted``
    bit; never the Queen's own words. The push carries the same text."""
    prefix = "" if context is None else f"[Context from schedule {schedule.chain!r}]:\n{context}"
    pushed = f"{prefix}\n\n{text}" if prefix else text
    if schedule.chat_id and store.get_chat(schedule.chat_id) is not None:
        if prefix:
            store.add_chat_message(
                schedule.chat_id, role="user", content=prefix,
                provenance=f"schedule:{schedule.chain}",
            )  # fmt: skip
        if script_output:
            store.add_chat_message(
                schedule.chat_id, role="user", content=text,
                provenance=f"schedule:{schedule.name}",
            )  # fmt: skip
        else:
            store.add_chat_message(schedule.chat_id, role="assistant", content=text)
        if notify is not None:
            try:
                notify(schedule.chat_id, pushed, kind)
            except Exception:
                logger.warning("schedule %r outbound push failed", schedule.name, exc_info=True)
    else:
        store.create_note(pushed, actor=f"schedule:{schedule.name}")


def compose_digest(store: RunStore) -> str:
    """v47-F6: the operator's "what needs me" summary, read-only over the store.

    Compact by design — a messenger message, not a report: pending approvals
    (the queue that blocks landings), recent run states, disabled/overdue-ish
    schedule health by last state, and memory proposals awaiting review."""
    lines = ["skep digest"]
    approvals = store.pending_approvals()
    if approvals:
        lines.append(f"approvals waiting: {len(approvals)}")
        for approval in approvals[:5]:
            lines.append(f"  - {approval.review_id[:13]}… {approval.action} ({approval.reason})")
    else:
        lines.append("approvals waiting: none")
    runs = store.recent_runs(20)
    if runs:
        by_state: dict[str, int] = {}
        for run in runs:
            by_state[run.state] = by_state.get(run.state, 0) + 1
        counts = ", ".join(f"{state} {count}" for state, count in sorted(by_state.items()))
        lines.append(f"recent runs: {counts}")
    schedules = store.list_schedules()
    disabled = [s.name for s in schedules if not s.enabled]
    failing = [
        s.name
        for s in schedules
        if s.enabled and s.last_state is not None and s.last_state not in SCHEDULE_SUCCESS_STATES
    ]
    if disabled:
        lines.append(f"schedules disabled: {', '.join(disabled[:5])}")
    if failing:
        lines.append(f"schedules last failed: {', '.join(failing[:5])}")
    proposals = store.list_memory_proposals(state="pending_review")
    if proposals:
        lines.append(f"memory proposals to review: {len(proposals)}")
    # v72-F4: the morning briefing remembers the week — last few active
    # observations (the fluid lane; they expire on their own).
    observations = [
        item for item in store.list_memory_items() if item.memory_class == "observation"
    ][:5]
    if observations:
        lines.append("recent observations:")
        for item in observations:
            lines.append(f"  - {item.content[:80]}")
    # v53-F1: the curator SURFACES the aging skill queue — it never acts.
    from .skill_curator import stale_drafts

    stale = stale_drafts(store)
    if stale:
        names = ", ".join(candidate.name for candidate in stale[:5])
        lines.append(f"skill drafts waiting >30d: {names}")
    return "\n".join(lines)


# v83-F5 (ADR 0042): the serve daemon's hook for 'prompt' schedules — one
# read-only Queen turn in the schedule's bound chat; (reply_text, ok). None
# (the CLI tick) fails the tick honestly: there is no chat engine here.
PromptTurnFn = Callable[[ScheduleRecord, "str | None"], tuple[str, bool]]


def run_due(
    *,
    store: RunStore,
    config: SupervisorConfig,
    now: str | None = None,
    dispatch: DispatchFn = run_task,
    notify: Callable[[str, str, str], None] | None = None,
    prompt_turn: PromptTurnFn | None = None,
) -> list[TickResult]:
    """Dispatch every schedule whose next_run_at has arrived; advance each one.

    A schedule that raises is recorded and skipped — one broken schedule never
    aborts the rest of the tick. Each due schedule shares the single-writer store
    (G4), so the whole tick is one writer even while dispatching several repos.
    """
    moment = now or now_ts()
    results: list[TickResult] = []
    for schedule in store.due_schedules(moment):
        task_id: str | None
        try:
            # ES B110: computed BEFORE the side effect, inside the try — a row
            # whose interval no clock can hold (stored before the cap) raised
            # here after its note had posted, outside the try, and killed the
            # whole tick every second.
            next_at = next_run_after(moment, schedule.interval_seconds)
        except (OverflowError, ValueError) as exc:
            state = f"dispatch_error: interval {schedule.interval_seconds}s is unusable ({exc})"
            store.set_schedule_enabled(schedule.name, enabled=False)
            store.set_schedule_disabled_reason(schedule.name, f"auto-disabled: {state}")
            store.record_schedule_outcome(schedule.name, task_id=None, state=state)
            results.append(TickResult(name=schedule.name, task_id=None, state=state))
            continue
        try:
            blocked_state: str | None = None
            # v53-F5: a chained schedule reads its source's last output as
            # labeled CONTEXT — never as new instructions.
            chained = _chained_context(store, schedule)
            if schedule.worker_kind == "note":
                # A 'note' schedule posts its text at tick time — into the chat
                # that created it (v43-F6) when one is bound and still exists,
                # otherwise as an inert note (the twin of chat add_note). No
                # repo, no worker, no policy surface; "note_posted" rides the
                # blocked_state no-dispatch tail and counts as a health success.
                _deliver_tick_text(store, schedule, schedule.instructions, notify, context=chained)
                store.record_schedule_output(schedule.name, schedule.instructions)
                blocked_state = "note_posted"
            elif schedule.worker_kind == "script":
                # v44-F4: the agent-less cron lane. The command runs
                # supervisor-side with operator standing — creation is
                # operator-gated (chat proposals always card; the token-authed
                # API IS the operator) and workers never see this lane. Output
                # delivers exactly like a note tick: bound chat (+ outbound
                # push) or inert note. A non-zero exit / timeout is a health
                # failure, so a persistently broken script auto-disables.
                # ponytail: scripts run sequentially inside the tick (120s cap
                # each); parallelize if a slow monitor ever starves the tick.
                # ES B120: claim the slot BEFORE the side effect — a serve that
                # dies mid-script must not re-run it at once on restart
                # (at-most-once; reconcile_interrupted_scripts records it).
                store.mark_schedule_ran(
                    schedule.name,
                    ran_at=moment,
                    next_run_at=next_at,
                    task_id=None,
                    state=SCRIPT_RUNNING_STATE,
                )
                output, script_ok = run_schedule_script(schedule.instructions, stdin=chained)
                store.record_schedule_output(schedule.name, output)
                if script_ok and not output:
                    # v51-F6: the watchdog pattern — a healthy check with
                    # nothing to say stays silent. The health row still
                    # records the tick, so "did it run?" keeps its answer.
                    blocked_state = "script_ran"
                else:
                    report = f"[{schedule.name}] {output}" if not script_ok else output
                    _deliver_tick_text(store, schedule, report, notify, script_output=True)
                    blocked_state = "script_ran" if script_ok else "script_failed"
            elif schedule.worker_kind == "prompt":
                # v83-F5 (ADR 0042): a Queen turn at tick time. Read-only by
                # construction (mutations refuse, never card — nobody is
                # watching to confirm, I6) and store-reads-only (no web
                # egress unattended). The engine writes the chat transcript
                # itself; the tick records the output and pushes outbound.
                if prompt_turn is None:
                    blocked_state = (
                        "prompt_failed: prompt schedules run inside the serve "
                        "daemon (the CLI tick has no chat engine)"
                    )
                else:
                    output, prompt_ok = prompt_turn(schedule, chained)
                    store.record_schedule_output(schedule.name, output)
                    if prompt_ok:
                        blocked_state = "prompt_posted"
                        if notify is not None and schedule.chat_id:
                            try:
                                notify(schedule.chat_id, output, "info")
                            except Exception:
                                logger.warning(
                                    "schedule %r outbound push failed",
                                    schedule.name,
                                    exc_info=True,
                                )
                    else:
                        blocked_state = f"prompt_failed: {output[:200]}"
            elif schedule.worker_kind == "digest":
                # v47-F6: the "what needs me" summary, composed from the store
                # at tick time and delivered exactly like a note tick. No repo,
                # no worker, read-only over the store.
                digest_text = compose_digest(store)
                store.record_schedule_output(schedule.name, digest_text)
                _deliver_tick_text(store, schedule, digest_text, notify, context=chained)
                blocked_state = "digest_posted"
            elif schedule.template_name is not None:
                # Template-bound: re-instantiate the live template at tick time, so
                # template edits + the template's budget apply (v3.5).
                template = store.get_template(schedule.template_name)
                if template is None:
                    raise ValueError(f"bound template {schedule.template_name!r} not found")
                instance = instantiate(
                    template, schedule.params, repo=schedule.repo, ref=schedule.ref
                )
                repo = Path(instance.repo).expanduser().resolve()
                template_bound = (
                    store.project_for_binding("template_name", schedule.template_name) is not None
                    or store.project_for_binding("repo_path", str(repo)) is not None
                    # v24-F2: a project bound by repo slug governs ticks too.
                    or store.project_for_binding("repo_slug", repo.name) is not None
                )
                if template_bound:
                    policy = run_policy_for_repo(
                        store,
                        config,
                        repo,
                        binding_candidates=[("template_name", schedule.template_name)],
                    )
                    try:
                        resolved = resolve_run_policy(
                            store=store,
                            config=config,
                            repo=repo,
                            caste=instance.worker_kind,
                            network=list(instance.permissions.network) or None,
                            env_allowlist=list(instance.permissions.env_allowlist),
                            wall_clock_seconds=instance.budget.wall_clock_seconds,
                            max_iterations=instance.budget.max_iterations,
                            max_actions=instance.budget.max_actions,
                            max_provider_calls=instance.budget.max_provider_calls,
                            execution_mode=None,
                            binding_candidates=[("template_name", schedule.template_name)],
                        )
                    except PolicyResolutionError as exc:
                        dispatch_decision = project_policy_dispatch_decision(
                            policy=policy,
                            requested_execution_mode=None,
                            explicit_run_overrides=False,
                            policy_resolution_error=exc,
                        )
                        blocked_state = f"policy_blocked: {dispatch_decision.reason}"
                    else:
                        dispatch_decision = _schedule_dispatch_decision(
                            project_bound=True,
                            policy=resolved.policy,
                        )
                        if not dispatch_decision.allows_execution():
                            blocked_state = f"policy_blocked: {dispatch_decision.reason}"
                        permissions = resolved.permissions
                        budget = resolved.budget
                        execution_mode = resolved.execution_mode
                        project_context = resolved.project_context
                        raw_auto_apply = resolved.policy.get("auto_apply_verified_patch")
                        auto_apply_verified_patch = (
                            raw_auto_apply if isinstance(raw_auto_apply, bool) else None
                        )
                        dispatch_decision = dispatch_decision.with_project_context(
                            project_context
                        ).with_network_audit(resolved.network_requested, resolved.network_resolved)
                else:
                    dispatch_decision = _schedule_dispatch_decision(project_bound=False)
                    # v24-F2 → ADR 0059 D3: run_task gives an unbound tick
                    # its provider_hosts, as it does every face.
                    permissions = instance.permissions
                    budget = instance.budget
                    execution_mode = "sandbox"
                    project_context = None
                    auto_apply_verified_patch = None
                if blocked_state is None:
                    outcome = dispatch(
                        repo,
                        instance.instructions
                        if chained is None
                        else _chain_prefixed(schedule, chained, instance.instructions),
                        config=config,
                        worker_kind=instance.worker_kind,
                        permissions=permissions,
                        budget=budget,
                        auto_apply_verified_patch=auto_apply_verified_patch,
                        project_context=project_context,
                        dispatch_decision=dispatch_decision,
                        ref=instance.ref,
                        store=store,
                        execution_mode=execution_mode,
                    )
            else:
                repo = Path(schedule.repo).expanduser().resolve()
                if (
                    store.project_for_binding("repo_path", str(repo)) is not None
                    # v24-F2: a project bound by repo slug governs ticks too.
                    or store.project_for_binding("repo_slug", repo.name) is not None
                ):
                    policy = run_policy_for_repo(store, config, repo)
                    try:
                        resolved = resolve_run_policy(
                            store=store,
                            config=config,
                            repo=repo,
                            caste=schedule.worker_kind,
                            network=list(schedule.network) or None,
                            env_allowlist=list(schedule.env_allowlist),
                            wall_clock_seconds=None,
                            max_iterations=None,
                            max_actions=None,
                            max_provider_calls=None,
                            execution_mode=None,
                            binding_candidates=[("repo_slug", repo.name)],
                        )
                    except PolicyResolutionError as exc:
                        dispatch_decision = project_policy_dispatch_decision(
                            policy=policy,
                            requested_execution_mode=None,
                            explicit_run_overrides=False,
                            policy_resolution_error=exc,
                        )
                        blocked_state = f"policy_blocked: {dispatch_decision.reason}"
                    else:
                        dispatch_decision = _schedule_dispatch_decision(
                            project_bound=True,
                            policy=resolved.policy,
                        )
                        if not dispatch_decision.allows_execution():
                            blocked_state = f"policy_blocked: {dispatch_decision.reason}"
                        permissions = resolved.permissions
                        budget = resolved.budget
                        execution_mode = resolved.execution_mode
                        project_context = resolved.project_context
                        raw_auto_apply = resolved.policy.get("auto_apply_verified_patch")
                        auto_apply_verified_patch = (
                            raw_auto_apply if isinstance(raw_auto_apply, bool) else None
                        )
                        dispatch_decision = dispatch_decision.with_project_context(
                            project_context
                        ).with_network_audit(resolved.network_requested, resolved.network_resolved)
                else:
                    dispatch_decision = _schedule_dispatch_decision(project_bound=False)
                    permissions = Permissions(
                        read=["workspace"],
                        write=["workspace"],
                        network=list(schedule.network),
                        env_allowlist=list(schedule.env_allowlist),
                    )
                    budget = DEFAULT_BUDGET
                    execution_mode = "sandbox"
                    project_context = None
                    auto_apply_verified_patch = None
                if blocked_state is None:
                    outcome = dispatch(
                        repo,
                        schedule.instructions
                        if chained is None
                        else _chain_prefixed(schedule, chained, schedule.instructions),
                        config=config,
                        worker_kind=schedule.worker_kind,
                        permissions=permissions,
                        budget=budget,
                        auto_apply_verified_patch=auto_apply_verified_patch,
                        project_context=project_context,
                        dispatch_decision=dispatch_decision,
                        ref=schedule.ref,
                        store=store,
                        execution_mode=execution_mode,
                    )
            if blocked_state is None:
                task_id, state = outcome.record.task_id, outcome.record.state
            else:
                task_id, state = None, blocked_state
        except Exception as exc:  # a broken schedule must not abort the rest of the tick
            task_id, state = None, f"dispatch_error: {exc}"
        # ES B110: the bookkeeping stays inside this schedule's own try too —
        # one schedule's failure must never end the tick for the rest.
        try:
            store.mark_schedule_ran(
                schedule.name,
                ran_at=moment,
                next_run_at=next_at,
                task_id=task_id,
                state=state,
            )
            # v14: record the tick outcome for schedule health (success rate,
            # consecutive failures, last failure reason).
            store.record_schedule_outcome(schedule.name, task_id=task_id, state=state)
            # v72-F3 (R5): a scheduled worker run's terminal state pushes through
            # the SAME vocabulary as chat-dispatched runs — the scheduler bypasses
            # the RunPool notify funnel, so a failing nightly run was silent.
            if task_id is not None:
                from .serve.run_status import run_terminal_text

                notice = run_terminal_text(store, task_id, audit_dir=config.audit_dir)
                if notice is not None:
                    terminal_text, terminal_kind = notice
                    _deliver_tick_text(
                        store,
                        schedule,
                        f"[{schedule.name}] {terminal_text}",
                        notify,
                        kind=terminal_kind,
                    )
            # v14 Step 2: a schedule that keeps failing (e.g. a persistent policy
            # block) is auto-disabled rather than retried every interval forever —
            # it advances and records health, but stops hot-looping on a broken
            # config until an operator fixes and re-enables it.
            health = store.schedule_health(schedule.name)
            if (
                health is not None
                and health.consecutive_failures >= MAX_SCHEDULE_CONSECUTIVE_FAILURES
            ):
                store.set_schedule_enabled(schedule.name, enabled=False)
                reason = (
                    f"auto-disabled after {health.consecutive_failures} "
                    f"consecutive failures: {state}"
                )
                store.set_schedule_disabled_reason(schedule.name, reason)
                # v72-F3 (R5): the auto-disable reaches the operator where they
                # are, with the call to action — digest-only was "waiting to be
                # asked".
                _deliver_tick_text(
                    store,
                    schedule,
                    f"schedule {schedule.name!r} {reason} — fix the cause, then "
                    "set_schedule_enabled to resume",
                    notify,
                )
            if schedule.once:
                # v44-F2: one-shot means ONE fire, success or not — retrying a
                # failed one-shot every interval forever would be worse than the
                # failure. The disabled row + reason is the record of what happened.
                store.set_schedule_enabled(schedule.name, enabled=False)
                store.set_schedule_disabled_reason(schedule.name, f"one-shot: fired ({state})")
        except Exception:
            logger.exception("schedule %r: recording its tick failed", schedule.name)
        results.append(TickResult(name=schedule.name, task_id=task_id, state=state))
    return results
