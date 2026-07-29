"""Task state machine for the Skep supervisor/worker boundary."""

from __future__ import annotations

from enum import StrEnum


class TaskState(StrEnum):
    CREATED = "created"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PENDING_APPROVAL = "pending_approval"
    WORKER_TIMEOUT = "worker_timeout"
    WORKER_CRASHED = "worker_crashed"
    REJECTED = "rejected"
    # v19-F8: supervisor-assigned when an approval resumes a run as a successor.
    # Workers never emit it; it only appears via store.transition.
    SUPERSEDED = "superseded"


TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.PENDING_APPROVAL,
        TaskState.WORKER_TIMEOUT,
        TaskState.WORKER_CRASHED,
        TaskState.REJECTED,
        TaskState.SUPERSEDED,
    }
)
