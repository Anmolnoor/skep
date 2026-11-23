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
import re
import select
import shlex
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from skep.worker_contract import Budget, Permissions

from .autonomy import AutonomyDecision, project_policy_dispatch_decision
from .castes import CASTES, RETIRED_CASTES
from .codex_login import run_auth_source
from .config import SupervisorConfig
from .contracts_io import DEFAULT_BUDGET
from .dispatch import run_task
from .engines import (
    CODING_ENGINES,
    CodingEngine,
    external_agent_for_command,
    resolve_engine,
)
from .ingest import IngestOutcome
from .nodes import OPS_INSPECT_CAPABILITIES, OPS_MUTATING_CAPABILITIES
from .policy import known_vacuous_verify
from .policy_resolver import (
    PolicyResolutionError,
    managed_repos_root,
    resolve_run_policy,
    run_policy_for_repo,
)
from .policy_v3 import log_decision
from .projects import _require_auto_apply_branch
from .providers import ProviderHealth, ProviderProfile, check_provider_health
from .sandbox import (
    _SEATBELT_SYSTEM_ROOTS,
    _SYSTEM_ROOTS,
    DENY_ALL_NETWORK,
    STRICT_READS_KEY,
    SandboxUnavailableError,
    read_allowlist_for,
    strict_read_roots,
)
from .sandbox import availability as sandbox_availability
from .spawner import build_worker_env, resolve_sandbox_backend
from .store import (
    PROVIDER_BLOCKED_STATE,
    SCHEDULE_SUCCESS_STATES,
    UNKNOWN_TAINT_ORIGIN,
    ChatTaint,
    RunStore,
    ScheduleRecord,
)
from .templates import WorkflowTemplate, instantiate

if TYPE_CHECKING:
    from .serve.settings import ConfigHolder

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


def next_run_after(reference: str, interval_seconds: int, *, now: str | None = None) -> str:
    """Next fire time = reference + interval. v122-F5: with ``now``, the slot
    is advanced in whole intervals until it is after ``now`` — a late or
    missed tick keeps the schedule's time of day and fires once, no catch-up
    storm."""
    if interval_seconds <= 0:  # parse_interval never makes one; a hand-edited row can
        raise ValueError(f"interval must be positive, got {interval_seconds}s")
    step = timedelta(seconds=interval_seconds)
    slot = _parse_ts(reference) + step
    if now is not None:
        late = _parse_ts(now) - slot
        if late >= timedelta(0):
            slot += step * (late // step + 1)
    return _fmt_ts(slot)


def next_fire_times(
    start_at: str | None, interval_seconds: int, *, once: bool = False, count: int = 3
) -> list[str]:
    """The next ``count`` fire times of a schedule created now, by the
    scheduler's own rules: the first is ``start_at`` (normalised) or the next
    tick, each later one the next slot on the schedule's own clock (v122-F5:
    a late tick does not move the rest). ``once`` fires once. A ``start_at``
    already past is due at once: the next tick fires it, then the schedule
    is back on its slot. ValueError on a bad ``start_at``."""
    now = now_ts()
    start = _start_slot(start_at)
    slot = start if start is not None else now
    times = [max(slot, now)]
    while not once and len(times) < count:
        slot = next_run_after(slot, interval_seconds, now=times[-1])
        times.append(slot)
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
    role: str | None = None,
) -> ScheduleRecord:
    if worker_kind in RETIRED_CASTES:  # v127-F11: refused at creation, every face
        raise ValueError(f"caste {worker_kind!r} is retired: {RETIRED_CASTES[worker_kind]}")
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
        role=role,
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
    role: str | None = None,
    once: bool = False,
    chat_id: str | None = None,
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
        role=role,
        once=once,  # v126-F11
        chat_id=chat_id,
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


def _with_chat_taint(taint: ChatTaint | None, decision: AutonomyDecision) -> AutonomyDecision:
    """v126-F11: a tick of a schedule a chat created carries that chat's
    CURRENT taint bits (``constraints.chat_taint``), as a live dispatch from
    the chat does (ADR 0059 D5) — so the run card shows them on every tick,
    not only on the card that created the schedule. A chat that is gone
    counts as both bits (get_chat_taint fails closed; v126-F11 review).
    ``taint``: the tick's one snapshot (None: no chat), the same one the
    untrusted refusal read (Phase 04 review R1)."""
    if taint is None:
        return decision
    bits = taint.bits()
    if not bits:
        return decision
    return replace(decision, constraints={**(decision.constraints or {}), "chat_taint": bits})


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
    worker: str | None = None,
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
    bit; never the Queen's own words. The push carries the same text.
    v123-F3 review (W21c): a scheduled run's terminal notice quotes its
    worker (``worker``: the task id) — a user row from ``worker:<task_id>``,
    exactly as ``run_status.notify_run_terminal`` writes it."""
    prefix = "" if context is None else f"[Context from schedule {schedule.chain!r}]:\n{context}"
    pushed = f"{prefix}\n\n{text}" if prefix else text
    if schedule.chat_id and store.get_chat(schedule.chat_id) is not None:
        if prefix:
            store.add_chat_message(
                schedule.chat_id, role="user", content=prefix,
                provenance=f"schedule:{schedule.chain}",
            )  # fmt: skip
        if worker is not None:
            store.add_chat_message(
                schedule.chat_id, role="user", content=text, provenance=f"worker:{worker}"
            )
        elif script_output:
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
        # W21 review: a script's output (or a chain's) is outside text, so the
        # note carries ``untrusted`` to every chat that lists it; v121-F3: and
        # ``private``, like every ``schedule:`` row
        outside = script_output or context is not None or worker is not None
        store.create_note(
            pushed,
            actor=f"schedule:{schedule.name}",
            taint=("untrusted", "private") if outside else (),
        )


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
        from .serve.chat import wrap_untrusted

        lines.append("recent observations:")
        for item in observations:
            text = item.content[:80]
            if item.provenance is not None:  # D15: a worker's words, outside content
                text = wrap_untrusted(item.provenance, text)
            lines.append(f"  - {text}")
    # v53-F1: the curator SURFACES the aging skill queue — it never acts.
    from .skill_curator import stale_drafts

    stale = stale_drafts(store)
    if stale:
        names = ", ".join(candidate.name for candidate in stale[:5])
        lines.append(f"skill drafts waiting >30d: {names}")
    return "\n".join(lines)


def _sources_first(due: list[ScheduleRecord]) -> list[ScheduleRecord]:
    """L14: a chained schedule due in the same tick as its source runs after
    it, so it reads this tick's output — not the last one. Stable otherwise
    (next_run_at order); chains are acyclic and capped (``validate_chain``)."""
    by_name = {schedule.name: schedule for schedule in due}

    def depth(schedule: ScheduleRecord) -> int:
        level, current = 0, schedule.chain
        while current in by_name and level <= MAX_CHAIN_DEPTH:
            level, current = level + 1, by_name[current].chain
        return level

    return sorted(due, key=depth)


# v83-F5 (ADR 0042): the serve daemon's hook for 'prompt' schedules — one
# read-only Queen turn in the schedule's bound chat; (reply_text, ok). None
# (the CLI tick) fails the tick honestly: there is no chat engine here.
PromptTurnFn = Callable[[ScheduleRecord, "str | None"], tuple[str, bool]]


def _held_to_role(
    store: RunStore,
    config: SupervisorConfig,
    holder: ConfigHolder | None,
    schedule: ScheduleRecord,
    repo: Path,
    permissions: Permissions,
    decision: AutonomyDecision,
    *,
    engine: str | None,
    caste: str,
    rows: list[dict[str, Any]],
) -> tuple[Permissions, AutonomyDecision, str | None]:
    """v123-F1 (DD-7, ADR 0055 D4): a schedule with a role is a delegator,
    ``schedule:<name>@<role>``. Every shell prefix and host its worker gets is
    decided under it by the chat's own narrowing (``delegate_permissions``,
    I5), so the role only ever tightens what the project allows. Headless:
    asks are dropped, never carded. A role that no longer resolves, or a
    narrowing that refuses, is the third value — ``policy_blocked: <why>`` —
    and the tick dispatches nothing: it never runs without its role. Phase
    02 (I13): its dispatch rows name ``engine`` and ``caste`` and are held
    in ``rows``, logged with the run's ``task_id`` once the tick has one."""
    if schedule.role is None:
        return permissions, decision, None
    from fastapi import HTTPException

    from .roles import Delegator, resolve_role
    from .serve.actions import delegate_permissions, delegated_decision
    from .serve.settings import ConfigHolder

    live = holder or ConfigHolder(config, store)
    try:
        role = resolve_role(schedule.role, live.current.home)
    except ValueError as exc:
        return (
            permissions,
            decision,
            (
                f"policy_blocked: schedule {schedule.name!r} runs as role {schedule.role!r}, "
                f"which no longer resolves ({exc}); re-create the schedule with a role that "
                "exists, or with none"
            ),
        )
    hop = Delegator(f"schedule:{schedule.name}@{role.name}", role)
    try:
        narrowed, delegation, note = delegate_permissions(
            live, store, repo, permissions, role=None, delegation_chain=[hop], deferred_asks=False,
            engine=engine, caste=caste, held=rows,
        )  # fmt: skip
    except HTTPException as exc:
        return permissions, decision, f"policy_blocked: {exc.detail}"
    return narrowed, delegated_decision(decision, delegation, note), None


# The tick kinds run_due serves itself, before any template or caste.
_TICK_LANES = frozenset({"note", "script", "prompt", "digest"})

# v127-F4: the text the builtin worker writes when its provider refuses the
# key (``llm_plan._chat_chunks``: "provider request failed: <OllamaError>",
# an OllamaError reads "401 from <url>"). A text match is the signal until a
# run carries a structured provider-failure field.
_PROVIDER_AUTH_FAILURE = re.compile(r"provider request failed: (40[13]\b[^\n]*)")


def _live_kind_and_repo(store: RunStore, schedule: ScheduleRecord) -> tuple[str, str] | None:
    """A template-bound tick runs the template as it is now, so its caste and
    repo come from the live template, not the schedule row's snapshot. None:
    the template is gone (the tick itself reports that)."""
    if schedule.template_name is None or schedule.worker_kind in _TICK_LANES:
        return schedule.worker_kind, schedule.repo
    template = store.get_template(schedule.template_name)
    if template is None:
        return None
    instance = instantiate(template, schedule.params, repo=schedule.repo, ref=schedule.ref)
    return instance.worker_kind, instance.repo


def _uses_worker_provider(
    store: RunStore, config: SupervisorConfig, schedule: ScheduleRecord
) -> bool:
    """v127-F4: does this schedule's run call skep's worker provider? Its
    caste must need one, and its worker must be skep's own: a worker command
    running an external agent's module (--worker-cmd / SKEP_WORKER_CMD, the
    resolver's ``wrapped``) or a coding run on an external engine
    authenticates on its own (ADR 0047) — skep's dead key is not its key."""
    live = _live_kind_and_repo(store, schedule)
    if live is None:
        return False
    worker_kind, repo = live
    caste = CASTES.get(worker_kind)
    if caste is None or not caste.needs_provider:
        return False
    if external_agent_for_command(config.command_for(worker_kind)) is not None:
        return False
    if worker_kind != "coding":
        return True
    resolved_repo = Path(repo).expanduser().resolve()
    policy = run_policy_for_repo(
        store,
        config,
        resolved_repo,
        binding_candidates=_bound_candidates(config, resolved_repo, schedule),
    )
    return not resolve_engine(str(policy.get("coding_engine") or "")).external


def _slug_candidates(config: SupervisorConfig, repo: Path) -> list[tuple[str, str]]:
    """v132-F5 (I5, I6): a ``repo_slug`` names a managed clone
    (``<home>/repos/<slug>``) — the policy resolver's rule. A folder elsewhere
    that shares its basename is not that project."""
    return [("repo_slug", repo.name)] if repo.parent == managed_repos_root(config) else []


def _bound_candidates(
    config: SupervisorConfig, repo: Path, schedule: ScheduleRecord
) -> list[tuple[str, str]]:
    """The binding candidates a tick resolves its project by — the same list
    for binding detection, provider preflight and policy resolution
    (sol-loop R1 S2): the schedule's template, then a managed clone's slug."""
    template = [("template_name", schedule.template_name)] if schedule.template_name else []
    return [*template, *_slug_candidates(config, repo)]


def _bound_by_slug(store: RunStore, config: SupervisorConfig, repo: Path) -> bool:
    return any(
        store.project_for_binding(kind, value) is not None
        for kind, value in _slug_candidates(config, repo)
    )


def _provider_blocked(reason: str) -> str:
    return (
        f"{PROVIDER_BLOCKED_STATE}: skipped without a run — {reason}. Fix the worker "
        "provider's key or settings (`skep doctor` shows the coding worker check); the schedule "
        "resumes on its next tick, and this skip never counts toward auto-disable"
    )


# Phase 04 4.2: a schedule an external engine's gates closed on. Disabled, not
# re-tried each tick; `skep doctor` lists it with the fix (status.py).
PAUSED_STATE = "paused"


def _refusal_row(
    schedule: ScheduleRecord,
    repo: Path,
    *,
    rule: str,
    note: str,
    caste: str,
    engine: str | None,
    **extra: Any,
) -> dict[str, Any]:
    """Phase 04 4.2 (I8, I13): a tick refused before any run, as one
    ``decisions.jsonl`` row with Phase 02's blame fields — no ``task_id``,
    because no run was minted. It names a repo, so replay never re-decides it."""
    row: dict[str, Any] = {
        "at": now_ts(),
        "actor": f"schedule:{schedule.name}",
        "repo": str(repo),
        "scope": "schedule",
        "action": "dispatch",
        "value": schedule.name,
        "source": "guard",
        "verdict": "deny",
        "rule_id": f"schedule.{rule}",
        "decided_by": f"schedule/{rule}",
        "layer": "guard",
        "note": note,
        "schedule": schedule.name,
        "caste": caste,
        **extra,
    }
    if engine and caste == "coding":
        row["engine"] = engine
    if schedule.role:
        row["role"] = schedule.role
    if schedule.chat_id:
        row["chat"] = schedule.chat_id
    return row


class _TickEngine(NamedTuple):
    """The external agent a coding launch runs, with the policy it runs under."""

    name: str
    engine: CodingEngine
    policy: dict[str, Any]
    repo: Path


def _launch_engine(
    config: SupervisorConfig, caste: str, coding_engine: str | None, policy: dict[str, Any],
    repo: Path,
) -> _TickEngine | None:  # fmt: skip
    """Phase 04 4.2 (D36): the external agent a coding launch runs — its
    engine, else the agent a --worker-cmd runs — with the policy it runs
    under; None for skep's own worker (or no coding launch). Review R2 (F3):
    an agent no entry declares (an unregistered ACP adapter, the shell
    worker) carries its own command, never builtin's empty requirements."""
    if caste != "coding":
        return None
    engine = resolve_engine(coding_engine or None)
    if engine.external:
        return _TickEngine(engine.name, engine, policy, repo)
    command = config.command_for("coding")
    wrapped = external_agent_for_command(command)
    if wrapped is None:
        return None
    declared = CODING_ENGINES.get(wrapped) or CodingEngine(name=wrapped, argv=tuple(command))
    return _TickEngine(wrapped, declared, policy, repo)


def _tick_engine(
    store: RunStore, config: SupervisorConfig, schedule: ScheduleRecord
) -> _TickEngine | None:
    """The tick's engine as the policy reads BEFORE resolution — only the
    pre-check, so a gate the resolver would refuse first (a vacuous pin)
    still pauses with its gate named. Review R2 (F2): what dispatches is
    gated again on its own resolved launch (_external_gates)."""
    live = _live_kind_and_repo(store, schedule)
    if live is None or live[0] != "coding":
        return None
    repo = Path(live[1]).expanduser().resolve()
    candidates = _bound_candidates(config, repo, schedule)
    policy = run_policy_for_repo(store, config, repo, binding_candidates=candidates)
    # The dispatch branches' own test: only a bound tick passes the policy's
    # engine; an unbound one dispatches builtin (or what --worker-cmd runs).
    bound = (
        any(store.project_for_binding(kind, value) is not None for kind, value in candidates)
        or store.project_for_binding("repo_path", str(repo)) is not None
    )
    engine = str(policy.get("coding_engine") or "") if bound else None
    return _launch_engine(config, live[0], engine, policy, repo)


def _no_auth(tick: _TickEngine, seconds: int) -> str | None:
    """Gate engine_auth's env half: the engine's auth env, or the opted-in
    plan login held for ``seconds`` (run_task decides the same way)."""
    engine = tick.engine
    if not engine.auth_env or any(os.environ.get(var) for var in engine.auth_env):
        return None
    if run_auth_source(engine.name, tick.policy, min_seconds=seconds) is not None:
        return None
    return (
        f"NO AUTH ENV — set one of {'/'.join(engine.auth_env)} in the environment the daemon "
        "inherits (<home>/serve.env, then restart it)"
        + (f", or an opted-in plan login valid for the tick's whole {seconds}s" if seconds else "")
    )


def _paused(
    schedule: ScheduleRecord, tick: _TickEngine, closed: list[tuple[str, str]]
) -> tuple[str, list[dict[str, Any]]] | None:
    """The ``paused:`` state and one decisions row per closed gate, or None."""
    if not closed:
        return None
    state = (
        f"{PAUSED_STATE}: an unattended run on the external engine {tick.name!r} needs all five "
        f"gates and {', '.join(gate for gate, _ in closed)} failed — "
        + "; ".join(f"gate {gate}: {why}" for gate, why in closed)
        + ". Fix it, then re-enable the schedule (chat: set_schedule_enabled; "
        f"PATCH /api/schedules/{schedule.name})"
    )
    rows = [
        _refusal_row(schedule, tick.repo, rule=gate, note=why, caste="coding", engine=tick.name)
        for gate, why in closed
    ]
    return state, rows


def _external_engine_pause(
    config: SupervisorConfig, schedule: ScheduleRecord, tick: _TickEngine
) -> tuple[str, list[dict[str, Any]]] | None:
    """Phase 04 4.2 (I1, I2, I4, I12): an external coding agent's own commands
    never pass the capability layer (ADR 0047), so it runs unattended only
    when ALL five gates hold — sandbox, strict_reads, verify_pin, engine_auth,
    landing. Judged twice: as the pre-check, before the tick spends anything
    (or the resolver refuses a vacuous pin first), and on the resolved launch
    itself (_external_gates, review R2), beside what only that launch knows —
    its egress, its budget (_launch_pause). Returns the ``paused:`` state and one
    decisions row per closed gate, or None. run_task's own refusals stay the
    backstop for every other face."""
    engine, policy = tick.engine, tick.policy
    setup = f"skep project setup {shlex.quote(str(tick.repo))}"
    closed: list[tuple[str, str]] = []
    mode = policy.get("default_execution_mode")
    # A strict-read run launches the backend that can confine reads whatever
    # its network (spawner.resolve_sandbox_backend), so that is the one probed.
    probe = sandbox_availability(
        resolve_sandbox_backend(config, DENY_ALL_NETWORK, strict_reads=True)
    )
    if mode != "sandbox":
        closed.append(
            ("sandbox", f"the project's default_execution_mode is {mode or 'unset'}, not "
             f"sandbox — `{setup} --execution-mode sandbox`")
        )  # fmt: skip
    elif not config.sandbox:
        closed.append(
            ("sandbox", "the sandbox is switched off in this supervisor's config, so nothing "
             "would confine the agent — run the daemon with its sandbox on")
        )  # fmt: skip
    elif not probe.usable:
        closed.append(
            ("sandbox", f"no usable sandbox here ({probe.reason}: {probe.detail}) — "
             "`skep doctor` names the fix")
        )  # fmt: skip
    if policy.get(STRICT_READS_KEY) is not True:
        closed.append(
            ("strict_reads", "strict_read_confinement is off, so the agent could read your "
             f"whole home — `{setup} --strict-reads`")
        )  # fmt: skip
    vacuous = known_vacuous_verify(str(policy.get("verify_command") or ""))
    if vacuous is not None:
        closed.append(
            ("verify_pin", f"{vacuous}, so G10 and its V1 test-tamper guard would confirm "
             f'nothing — `{setup} --verify-command "<command>"`')
        )  # fmt: skip
    # Review R1: the binary as the launch finds it — the worker's PATH, which
    # leaves skep's own venv off (spawner.build_worker_env).
    worker_path = build_worker_env((), baseline=config.env_baseline).get("PATH", "")
    found = shutil.which(engine.binary, path=worker_path) if engine.binary else None
    allowlist = read_allowlist_for(policy)
    if engine.name not in CODING_ENGINES:
        closed.append(
            ("engine_auth", f"the worker command `{shlex.join(engine.argv)}` runs {tick.name!r}, "
             "which no engine entry declares, so the binary and auth env it needs cannot be "
             "verified — run a registered engine (`skep doctor` lists each) or attend its runs")
        )  # fmt: skip
    elif engine.binary is not None and found is None:
        closed.append(
            ("engine_auth", f"{engine.binary!r} is not on the worker's PATH (skep's own venv "
             "is left off it) — install it there (`skep doctor` lists each engine)")
        )  # fmt: skip
    elif (
        found is not None
        and allowlist is not None
        and (outside := _outside_strict_wall(config, Path(found), allowlist, probe.backend))
    ):
        closed.append(("engine_auth", outside))
    elif (no_auth := _no_auth(tick, 0)) is not None:
        closed.append(("engine_auth", no_auth))
    branch = policy.get("auto_apply_branch")
    if branch is not None:
        try:
            _require_auto_apply_branch(branch)
        except ValueError as exc:
            closed.append(("landing", f"{exc} — main never moves unattended (I1)"))
    return _paused(schedule, tick, closed)


def _outside_strict_wall(
    config: SupervisorConfig, found: Path, extra: tuple[Path, ...], backend: str | None
) -> str | None:
    """Review R2 (F5, I9): a strict launch binds only the system dirs, the
    detected toolchains and the project's extra_read_paths — a binary on the
    worker's PATH anywhere else (or linked from there) could never start.
    Why not, else None. ponytail: the binary itself, not its interpreter."""
    system = _SEATBELT_SYSTEM_ROOTS if backend == "seatbelt" else _SYSTEM_ROOTS
    try:
        roots = [*map(Path, system), *strict_read_roots(config.home.parent, extra=extra)]
    except SandboxUnavailableError as exc:
        return str(exc)
    roots += [root.resolve() for root in roots]
    if all(any(path.is_relative_to(root) for root in roots) for path in (found, found.resolve())):
        return None
    return (
        f"{str(found)!r} (really {found.resolve()}) is outside every root the strict read wall "
        "binds (the system dirs, the detected toolchains, the project's extra_read_paths), so "
        "the launch could not start it — install it under one, or add its directory to the "
        "project's extra_read_paths"
    )


def _launch_pause(
    schedule: ScheduleRecord, tick: _TickEngine, *, permissions: Permissions, budget: Budget
) -> tuple[str, list[dict[str, Any]]] | None:
    """Phase 04 review R1: the two gates only the resolved run can answer,
    on the final dispatch path. Gate sandbox on the launch's own profile: a
    ``*`` egress takes no filtering proxy (dispatch), so its files would be
    walled and its network not — gate (a), because the spec's sandbox gate
    is the launched profile, and this one does not confine. Gate engine_auth
    on the tick's own budget: a template's may outlast the project default."""
    closed: list[tuple[str, str]] = []
    if "*" in permissions.network:
        closed.append(
            ("sandbox", "the run's egress allows every host (`*`), which no filtering proxy "
             "confines — pin the hosts it needs in the schedule's, template's or project's "
             "network instead")
        )  # fmt: skip
    if (no_auth := _no_auth(tick, budget.wall_clock_seconds)) is not None:
        closed.append(("engine_auth", no_auth))
    return _paused(schedule, tick, closed)


def _foreign_untrusted_row(store: RunStore, schedule: ScheduleRecord, origin: int) -> int | None:
    """Review R1 (ADR 0059 D5): the row that makes ``schedule``'s chat
    untrusted for IT — None when the bit's only sources are finish notices of
    the schedule's own runs (``worker:<task_id>``, which run_due posts): its
    own output never refuses its next tick, though the Queen still sees the
    bit. Review R2 (F4): the store records every row that contributes the
    bit, even once it is on, so only those count — words, a private-only
    read or a denied tool set none. Unknown taint, a setter not in this chat
    (inherited) or one never recorded is the answer as is — fail closed.

    ponytail: reads the whole transcript, only for an untrusted external
    tick; a tool row whose bit lands (serve/taint.py) after this read is not
    seen."""
    chat = schedule.chat_id
    if not chat or origin == UNKNOWN_TAINT_ORIGIN:
        return origin
    since = [message for message in store.chat_messages(chat) if message.id >= origin]
    contributed = store.chat_untrusted_rows(chat)
    if not since or since[0].id != origin or origin not in contributed:
        return origin
    for message in since:
        kind, _, task = message.provenance.partition(":")
        if message.id not in contributed or (
            kind == "worker" and store.schedule_for_task(task) == schedule.name
        ):
            continue
        return message.id
    return None


def _untrusted_refusal(
    store: RunStore,
    schedule: ScheduleRecord,
    repo: Path,
    *,
    engine: str,
    taint: ChatTaint | None,
    rows: list[dict[str, Any]],
) -> str | None:
    """Phase 04 4.2 (ADR 0059 D5, I6): a chat that read outside content makes
    every dispatch for it ask, and an unattended tick has nobody to ask — so a
    schedule whose chat carries ``untrusted`` refuses here, on the final
    dispatch path (v126-F11 only recorded the bits). A chat that is gone
    counts as both bits (get_chat_taint fails closed). ``private`` alone
    never stops a run: a mail chat's schedule keeps working. Only for an
    EXTERNAL engine (master plan trap 6; the caller passes the tick's
    effective one): a builtin tick still dispatches with the bits attached
    (v126-F11). ``taint`` is the tick's one snapshot (None: no chat)."""
    if taint is None or taint.untrusted is None:
        return None
    origin = _foreign_untrusted_row(store, schedule, taint.untrusted)
    if origin is None:
        return None
    chat = schedule.chat_id
    if origin == UNKNOWN_TAINT_ORIGIN:
        why = (
            f"its chat {chat} is gone or unreadable, so its taint is unknown (counts as untrusted)"
        )
        fix = "re-create the schedule from a chat you trust, or from the CLI"
    else:
        why = f"its chat {chat} carries the untrusted bit (set by message {origin})"
        fix = (
            f"read what set it, then `skep trust {chat} --bit untrusted` (/trust in the chat), "
            "or re-create the schedule from a clean chat"
        )
    note = (
        f"schedule {schedule.name!r} refused: {why}, and an unattended run never acts for a "
        f"chat that read outside content (ADR 0059 D5) — {fix}"
    )
    rows.append(
        _refusal_row(
            schedule, repo, rule="untrusted_taint", note=note, caste="coding", engine=engine,
            taint=taint.bits(),
        )
    )  # fmt: skip
    return f"policy_blocked: {note}"


def _external_gates(
    store: RunStore,
    config: SupervisorConfig,
    schedule: ScheduleRecord,
    repo: Path,
    *,
    caste: str,
    coding_engine: str,
    policy: dict[str, Any],
    permissions: Permissions,
    budget: Budget,
    rows: list[dict[str, Any]],
) -> tuple[str | None, ChatTaint | None]:
    """Phase 04 4.2, last before dispatch. Review R2 (F2): the resolved
    launch — the caste, engine and policy dispatch is handed — is the one
    gated: its five gates and its profile's two (a closed one pauses), then
    the untrusted-taint refusal. Review R2 (F1): the chat's taint is read
    here, last, and that one snapshot is returned for the run's metadata.
    State None: dispatch (a builtin launch is held to neither gate, master
    plan trap 6)."""
    tick = _launch_engine(config, caste, coding_engine, policy, repo)
    if tick is not None:
        pause = _external_engine_pause(config, schedule, tick) or _launch_pause(
            schedule, tick, permissions=permissions, budget=budget
        )
        if pause is not None:
            state, refusals = pause
            rows.extend(refusals)
            return state, None
    taint = store.get_chat_taint(schedule.chat_id) if schedule.chat_id else None
    if tick is None:
        return None, taint
    refusal = _untrusted_refusal(store, schedule, repo, engine=tick.name, taint=taint, rows=rows)
    return refusal, taint


def run_due(
    *,
    store: RunStore,
    config: SupervisorConfig,
    now: str | None = None,
    dispatch: DispatchFn = run_task,
    notify: Callable[[str, str, str], None] | None = None,
    prompt_turn: PromptTurnFn | None = None,
    holder: ConfigHolder | None = None,
) -> list[TickResult]:
    """Dispatch every schedule whose next_run_at has arrived; advance each one.

    A schedule that raises is recorded and skipped — one broken schedule never
    aborts the rest of the tick. Each due schedule shares the single-writer store
    (G4), so the whole tick is one writer even while dispatching several repos.
    ``holder`` is the live config a schedule's role is decided against (v123-F1);
    none builds one from ``config``.
    """
    moment = now or now_ts()
    results: list[TickResult] = []
    # v127-F4: one worker-provider probe per tick (a real completion, so only
    # when a provider-needing schedule is due); a dead key — or the first run
    # this tick that died on one — skips the rest without a run row.
    provider_block: str | None = None
    provider_probed = False
    for schedule in _sources_first(store.due_schedules(moment)):
        task_id: str | None
        held: tuple[dict[str, Any], Path] | None = None  # v123 review: the receipt owed (I13)
        rows: list[dict[str, Any]] = []  # 2.1: the role's dispatch rows, logged with the id
        try:
            # ES B110: computed BEFORE the side effect, inside the try — a row
            # whose interval no clock can hold (stored before the cap) raised
            # here after its note had posted, outside the try, and killed the
            # whole tick every second.
            next_at = next_run_after(schedule.next_run_at, schedule.interval_seconds, now=moment)
        except (OverflowError, ValueError) as exc:
            state = f"dispatch_error: interval {schedule.interval_seconds}s is unusable ({exc})"
            store.set_schedule_enabled(schedule.name, enabled=False)
            store.set_schedule_disabled_reason(schedule.name, f"auto-disabled: {state}")
            store.record_schedule_outcome(schedule.name, task_id=None, state=state)
            results.append(TickResult(name=schedule.name, task_id=None, state=state))
            continue
        try:
            blocked_state: str | None = None
            # v132-F5 (I2, I8): a bound run is the project's run — its engine
            # and verify pins reach dispatch (empty = run_task's builtin).
            verify_command = coding_engine = ""
            auto_apply_branch: str | None = None
            planning_protocol = "plan"
            # Phase 04 4.2: the gates' pre-check, on the policy as read now;
            # review R2: what dispatches is gated on its resolved launch.
            external = _tick_engine(store, config, schedule)
            if _uses_worker_provider(store, config, schedule):
                if provider_block is None and not provider_probed:
                    provider_probed = True
                    from .. import status

                    # v127-F4 review: only a refused key or a config error no
                    # run gets past skips; a timeout, 5xx, 429 or reset (a cold
                    # local model) dispatches and the run reports itself.
                    probe = status.worker_provider_check(config.home.parent, 5.0)
                    if probe.get("kind") in ("auth", "config"):
                        provider_block = _provider_blocked(
                            f"the worker provider probe failed ({probe.get('detail')})"
                        )
                blocked_state = provider_block
            elif external is not None and (
                pause := _external_engine_pause(config, schedule, external)
            ):
                blocked_state, refusals = pause  # Phase 04 4.2: before any run
                rows.extend(refusals)
            # v53-F5: a chained schedule reads its source's last output as
            # labeled CONTEXT — never as new instructions.
            chained = _chained_context(store, schedule)
            if blocked_state is not None:
                pass  # v127-F4: provider-blocked, nothing runs
            elif schedule.worker_kind == "note":
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
                    or _bound_by_slug(store, config, repo)
                )
                if template_bound:
                    candidates = _bound_candidates(config, repo, schedule)
                    policy = run_policy_for_repo(store, config, repo, binding_candidates=candidates)
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
                            binding_candidates=candidates,
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
                        policy = resolved.policy  # review R2 (F2): what the gates judge
                        permissions = resolved.permissions
                        budget = resolved.budget
                        execution_mode = resolved.execution_mode
                        project_context = resolved.project_context
                        verify_command = resolved.verify_command
                        coding_engine = resolved.coding_engine
                        planning_protocol = resolved.worker_protocol
                        raw_branch = resolved.policy.get("auto_apply_branch")
                        auto_apply_branch = raw_branch if isinstance(raw_branch, str) else None
                        raw_auto_apply = resolved.policy.get("auto_apply_verified_patch")
                        auto_apply_verified_patch = (
                            raw_auto_apply if isinstance(raw_auto_apply, bool) else None
                        )
                        dispatch_decision = dispatch_decision.with_project_context(
                            project_context
                        ).with_network_audit(resolved.network_requested, resolved.network_resolved)
                else:
                    dispatch_decision = _schedule_dispatch_decision(project_bound=False)
                    policy = run_policy_for_repo(
                        store,
                        config,
                        repo,
                        binding_candidates=_bound_candidates(config, repo, schedule),
                    )  # what an unbound launch's gates judge (dispatch reads the same)
                    # v24-F2 → ADR 0059 D3: run_task gives an unbound tick
                    # its provider_hosts, as it does every face.
                    permissions = instance.permissions
                    budget = instance.budget
                    execution_mode = "sandbox"
                    project_context = None
                    auto_apply_verified_patch = None
                if blocked_state is None:
                    permissions, dispatch_decision, blocked_state = _held_to_role(
                        store, config, holder, schedule, repo, permissions, dispatch_decision,
                        engine=coding_engine or None, caste=instance.worker_kind, rows=rows,
                    )  # fmt: skip
                if blocked_state is None:
                    blocked_state, taint = _external_gates(
                        store, config, schedule, repo, caste=instance.worker_kind,
                        coding_engine=coding_engine, policy=policy, permissions=permissions,
                        budget=budget, rows=rows,
                    )  # fmt: skip
                if blocked_state is None:
                    dispatch_decision = _with_chat_taint(taint, dispatch_decision)
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
                        verify_command=verify_command,
                        coding_engine=coding_engine,
                        auto_apply_branch=auto_apply_branch,
                        planning_protocol=planning_protocol,
                        # Phase 04 A10: an ACP run's cards land in the chat that made the schedule
                        chat_id=schedule.chat_id,
                    )
            else:
                repo = Path(schedule.repo).expanduser().resolve()
                if (
                    store.project_for_binding("repo_path", str(repo)) is not None
                    # v24-F2: a project bound by repo slug governs ticks too.
                    or _bound_by_slug(store, config, repo)
                ):
                    policy = run_policy_for_repo(
                        store, config, repo, binding_candidates=_slug_candidates(config, repo)
                    )
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
                            binding_candidates=_slug_candidates(config, repo),
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
                        policy = resolved.policy  # review R2 (F2): what the gates judge
                        permissions = resolved.permissions
                        budget = resolved.budget
                        execution_mode = resolved.execution_mode
                        project_context = resolved.project_context
                        verify_command = resolved.verify_command
                        coding_engine = resolved.coding_engine
                        planning_protocol = resolved.worker_protocol
                        raw_branch = resolved.policy.get("auto_apply_branch")
                        auto_apply_branch = raw_branch if isinstance(raw_branch, str) else None
                        raw_auto_apply = resolved.policy.get("auto_apply_verified_patch")
                        auto_apply_verified_patch = (
                            raw_auto_apply if isinstance(raw_auto_apply, bool) else None
                        )
                        dispatch_decision = dispatch_decision.with_project_context(
                            project_context
                        ).with_network_audit(resolved.network_requested, resolved.network_resolved)
                else:
                    dispatch_decision = _schedule_dispatch_decision(project_bound=False)
                    policy = run_policy_for_repo(
                        store,
                        config,
                        repo,
                        binding_candidates=_bound_candidates(config, repo, schedule),
                    )  # what an unbound launch's gates judge (dispatch reads the same)
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
                    permissions, dispatch_decision, blocked_state = _held_to_role(
                        store, config, holder, schedule, repo, permissions, dispatch_decision,
                        engine=coding_engine or None, caste=schedule.worker_kind, rows=rows,
                    )  # fmt: skip
                if blocked_state is None:
                    blocked_state, taint = _external_gates(
                        store, config, schedule, repo, caste=schedule.worker_kind,
                        coding_engine=coding_engine, policy=policy, permissions=permissions,
                        budget=budget, rows=rows,
                    )  # fmt: skip
                if blocked_state is None:
                    dispatch_decision = _with_chat_taint(taint, dispatch_decision)
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
                        verify_command=verify_command,
                        coding_engine=coding_engine,
                        auto_apply_branch=auto_apply_branch,
                        planning_protocol=planning_protocol,
                        # Phase 04 A10: an ACP run's cards land in the chat that made the schedule
                        chat_id=schedule.chat_id,
                    )
            if blocked_state is None:
                task_id, state = outcome.record.task_id, outcome.record.state
                auth_failure = _PROVIDER_AUTH_FAILURE.search(
                    getattr(outcome.record, "summary", None) or ""
                )
                if auth_failure and provider_block is None:
                    provider_block = _provider_blocked(
                        f"run {task_id} this tick failed on the provider ({auth_failure.group(1)})"
                    )
                if schedule.role is not None and dispatch_decision.delegation is not None:
                    held = dict(dispatch_decision.delegation), repo
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
                        worker=task_id,
                    )
            # v14 Step 2: a schedule that keeps failing (e.g. a persistent policy
            # block) is auto-disabled rather than retried every interval forever —
            # it advances and records health, but stops hot-looping on a broken
            # config until an operator fixes and re-enables it.
            health = store.schedule_health(schedule.name)
            paused = state.startswith(f"{PAUSED_STATE}: ")
            if paused:
                # Phase 04 4.2 (I9): paused, not re-tried every tick, and told
                # where the operator is — `skep doctor` lists it with the fix.
                store.set_schedule_enabled(schedule.name, enabled=False)
                store.set_schedule_disabled_reason(schedule.name, state)
                _deliver_tick_text(store, schedule, f"schedule {schedule.name!r} {state}", notify)
            elif (
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
            if schedule.once and not paused and not state.startswith(PROVIDER_BLOCKED_STATE):
                # v44-F2: one-shot means ONE fire, success or not — retrying a
                # failed one-shot every interval forever would be worse than the
                # failure. The disabled row + reason is the record of what happened.
                store.set_schedule_enabled(schedule.name, enabled=False)
                store.set_schedule_disabled_reason(schedule.name, f"one-shot: fired ({state})")
            if rows and task_id is not None:  # 2.1 (I13): with the run's id once it has one
                from .serve.actions import with_run_blame

                run = store.get_run(task_id)
                engine = run.coding_engine if run is not None else None
                rows = [with_run_blame(row, task_id, engine) for row in rows]
            for row in rows:
                log_decision(config.audit_dir, row)
            if held is not None and task_id is not None:
                # v123-F1 review (I13, R3): one ``delegated`` row naming
                # ``schedule:<name>@<role>``, as submit_run writes it — last,
                # so a ledger failure never re-fires or misreports the run
                from .serve.actions import record_delegation

                record_delegation(config.audit_dir, held[0], repo=str(held[1]), task=task_id)
        except Exception:
            logger.exception("schedule %r: recording its tick failed", schedule.name)
        results.append(TickResult(name=schedule.name, task_id=task_id, state=state))
    return results
