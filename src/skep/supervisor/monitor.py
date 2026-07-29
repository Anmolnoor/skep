"""Worker death path (decision Q3): deadline, heartbeat loss, crash synthesis.

The on-disk NDJSON file is read post-exit per the v1 contract; the polling here
is supervisor-internal liveness tracking, not a transport. A worker that dies or
hangs cannot self-report — the supervisor synthesizes the terminal event with
``synthesized: true`` (spec §4).
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from skep.worker_contract import CONTRACT_VERSION, Event, EventType, TaskState

from .contracts_io import read_event_log

VerdictKind = Literal["worker_reported", "wall_clock", "heartbeat_lost", "worker_crashed"]


@dataclass(frozen=True)
class MonitorVerdict:
    kind: VerdictKind
    exit_code: int | None
    events: list[Event]
    synthesized_terminal: Event | None

    @property
    def terminal_event(self) -> Event | None:
        if self.synthesized_terminal is not None:
            return self.synthesized_terminal
        if self.events and self.events[-1].type is EventType.TASK_TERMINAL:
            return self.events[-1]
        return None


def kill_process_tree(process: subprocess.Popen[bytes]) -> None:
    """SIGKILL the worker's whole session; never leave zombies (Q3)."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    process.wait()


def synthesize_terminal(
    *,
    task_id: str,
    trace_id: str,
    seq: int,
    status: TaskState,
    summary: str,
    reason: str,
    exit_code: int | None = None,
) -> Event:
    payload: dict[str, object] = {
        "status": status.value,
        "summary": summary,
        "synthesized": True,
        "reason": reason,
    }
    if exit_code is not None:
        payload["exit_code"] = exit_code
    return Event(
        contract_version=CONTRACT_VERSION,
        event_id=str(uuid.uuid4()),
        seq=seq,
        task_id=task_id,
        trace_id=trace_id,
        ts=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        type=EventType.TASK_TERMINAL,
        payload=payload,
    )


def append_event(path: Path, event: Event) -> None:
    """Append a (synthesized) event to an NDJSON log copy."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event.model_dump(mode="json"), ensure_ascii=True) + "\n")


def watch_worker(
    process: subprocess.Popen[bytes],
    events_path: Path,
    *,
    task_id: str,
    trace_id: str,
    wall_clock_seconds: float,
    grace_seconds: float,
    heartbeat_seconds: float,
    poll_seconds: float = 0.05,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> MonitorVerdict:
    """Babysit one worker process until it exits or breaches its grant.

    Breach conditions (Q3):
    - wall clock: ``budget.wall_clock_seconds + grace`` elapsed → kill tree
    - heartbeat loss: no new event activity for 3xN with a live process → kill tree
    Either way the tree is killed, and a ``task.terminal`` is synthesized if the
    worker did not write one. Process exit without a terminal event → crash.
    """
    started = clock()
    deadline = started + wall_clock_seconds + grace_seconds
    last_activity = started
    seen_bytes = 0
    breach: VerdictKind | None = None

    while True:
        exit_code = process.poll()
        try:
            size = events_path.stat().st_size
        except FileNotFoundError:
            size = 0
        if size > seen_bytes:
            seen_bytes = size
            last_activity = clock()
        if exit_code is not None:
            break
        now = clock()
        if now > deadline:
            breach = "wall_clock"
            kill_process_tree(process)
            exit_code = process.returncode
            break
        if now - last_activity > 3 * heartbeat_seconds:
            breach = "heartbeat_lost"
            kill_process_tree(process)
            exit_code = process.returncode
            break
        sleep(poll_seconds)

    events = read_event_log(events_path)
    worker_reported_terminal = bool(events) and events[-1].type is EventType.TASK_TERMINAL
    next_seq = (events[-1].seq + 1) if events else 1

    if breach is not None:
        synthesized = synthesize_terminal(
            task_id=task_id,
            trace_id=trace_id,
            seq=next_seq,
            status=TaskState.WORKER_TIMEOUT,
            summary=(
                "Wall-clock budget breached; worker killed and torn down."
                if breach == "wall_clock"
                else f"No heartbeat for 3x{heartbeat_seconds}s with a live process; "
                "worker killed and torn down."
            ),
            reason="wall_clock_exceeded" if breach == "wall_clock" else "heartbeat_lost",
            exit_code=exit_code,
        )
        return MonitorVerdict(
            kind=breach, exit_code=exit_code, events=events, synthesized_terminal=synthesized
        )

    if not worker_reported_terminal:
        synthesized = synthesize_terminal(
            task_id=task_id,
            trace_id=trace_id,
            seq=next_seq,
            status=TaskState.WORKER_CRASHED,
            summary="Worker process exited without a terminal event.",
            reason="process_exit_without_terminal",
            exit_code=exit_code,
        )
        return MonitorVerdict(
            kind="worker_crashed",
            exit_code=exit_code,
            events=events,
            synthesized_terminal=synthesized,
        )

    return MonitorVerdict(
        kind="worker_reported", exit_code=exit_code, events=events, synthesized_terminal=None
    )
