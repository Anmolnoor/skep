"""Background dispatch for ``skep serve`` (v5 Stage A).

``run_task`` is synchronous end-to-end (worktree → worker → ingest), so an HTTP
handler must never call it inline. The dispatcher submits the run to a small
executor and blocks only until dispatch mints the task id and creates the run
row (sub-second worktree setup, not the run itself) — that is what lets
``POST /api/runs`` answer 202 + task id while the worker keeps going.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from skep.worker_contract import (
    ApprovalVerdict,
    Budget,
    Permissions,
    ProjectContextPayload,
    TaskIntent,
)

from ..autonomy import AutonomyDecision
from ..dispatch import run_task
from ..store import RunStore
from .settings import ConfigHolder


class DispatchError(RuntimeError):
    """Dispatch ended (or stalled) before the run was created."""


class Dispatcher:
    """Run ``run_task`` on a background pool, returning the task id early.

    The pool shares the server's single ``RunStore`` (G4: one writer, RLock +
    WAL), exactly like the scheduler's dispatch pool. Each submit reads the
    holder's *current* config, so a policy edit applies to the next run (A5).
    """

    def __init__(
        self,
        holder: ConfigHolder,
        store: RunStore,
        *,
        max_workers: int = 4,
        id_timeout_seconds: float = 30.0,
    ) -> None:
        self._holder = holder
        self._store = store
        self._id_timeout = id_timeout_seconds
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="serve-run")
        # v43-F4: invoked with the task id after run_task returns (any outcome)
        # — the seam that lets a chat-dispatched run's death reach the chat.
        self.on_run_finished: Callable[[str], None] | None = None

    def submit(
        self,
        repo: Path,
        instructions: str,
        *,
        worker_kind: str = "coding",
        permissions: Permissions | None = None,
        budget: Budget | None = None,
        auto_apply_verified_patch: bool | None = None,
        auto_apply_branch: str | None = None,
        project_context: ProjectContextPayload | None = None,
        dispatch_decision: AutonomyDecision | None = None,
        intent: TaskIntent | None = None,
        ref: str | None = None,
        resume_of: str | None = None,
        approval_verdict: ApprovalVerdict | None = None,
        worker_state: dict[str, Any] | None = None,
        execution_mode: str = "sandbox",
        planning_protocol: str = "plan",
        verify_command: str = "",
        coding_engine: str = "",
    ) -> str:
        """Dispatch a run in the background; return its task id once minted.

        Raises ``DispatchError`` if the run dies before ``create_run`` (bad
        repo, worktree failure) or no id appears within the timeout.
        """
        created = threading.Event()
        box: dict[str, str] = {}

        def _on_created(task_id: str) -> None:
            box["task_id"] = task_id
            created.set()

        future = self._pool.submit(
            run_task,
            repo,
            instructions,
            config=self._holder.current,
            worker_kind=worker_kind,
            permissions=permissions,
            budget=budget,
            auto_apply_verified_patch=auto_apply_verified_patch,
            auto_apply_branch=auto_apply_branch,
            project_context=project_context,
            dispatch_decision=dispatch_decision,
            intent=intent,
            ref=ref,
            resume_of=resume_of,
            approval_verdict=approval_verdict,
            worker_state=worker_state,
            store=self._store,
            on_run_created=_on_created,
            execution_mode=execution_mode,
            planning_protocol=planning_protocol,
            verify_command=verify_command,
            coding_engine=coding_engine,
        )

        # Wake on whichever comes first: the id callback, or the run failing
        # before it ever created a row.
        def _done(_future: Any) -> None:
            created.set()
            callback = self.on_run_finished
            if callback is not None and "task_id" in box:
                # a notifier bug must never poison the pool
                with contextlib.suppress(Exception):
                    callback(box["task_id"])

        future.add_done_callback(_done)
        if not created.wait(self._id_timeout):
            raise DispatchError("dispatch did not produce a task id in time")
        if "task_id" in box:
            return box["task_id"]
        exc = future.exception()
        raise DispatchError(str(exc) if exc else "dispatch ended before creating a run")

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
