"""Supervisor helpers for carrying worker plugin state into resumed runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from skep.worker_contract import (
    RESUME_CHECKPOINT_ARTIFACT_NAME,
    RESUME_CHECKPOINT_STATE_KEY,
    CodingWorkerTask,
)


def resume_checkpoint_version(worker_state: dict[str, Any] | None) -> int:
    """0 when absent or malformed; else the checkpoint's declared version."""
    if worker_state is None:
        return 0
    raw = worker_state.get(RESUME_CHECKPOINT_STATE_KEY)
    if not isinstance(raw, dict):
        return 0
    version = raw.get("version")
    return version if isinstance(version, int) and not isinstance(version, bool) else 0


def strip_resume_cursor(worker_state: dict[str, Any] | None) -> dict[str, Any] | None:
    """Drop the step cursor when the resume cannot reuse the suspended worktree.

    A fresh worktree has none of the completed steps' effects, so the plan
    must replay from step 0 (accumulated approval grants make that converge).
    """
    if worker_state is None:
        return None
    raw = worker_state.get(RESUME_CHECKPOINT_STATE_KEY)
    if not isinstance(raw, dict) or "cursor" not in raw:
        return worker_state
    checkpoint = {key: value for key, value in raw.items() if key != "cursor"}
    return {**worker_state, RESUME_CHECKPOINT_STATE_KEY: checkpoint}


def resume_worker_state_from_audit(audit_dir: Path, task_id: str) -> dict[str, Any] | None:
    checkpoint = audit_dir / task_id / RESUME_CHECKPOINT_ARTIFACT_NAME
    if not checkpoint.is_file():
        return None
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"resume checkpoint must be a JSON object: {checkpoint}")
    return payload


def prior_task_from_audit(audit_dir: Path, task_id: str) -> CodingWorkerTask | None:
    """The audited task envelope of the run being resumed, if still readable."""
    task_path = audit_dir / task_id / "task.json"
    if not task_path.is_file():
        return None
    try:
        return CodingWorkerTask.model_validate_json(task_path.read_text(encoding="utf-8"))
    except (OSError, ValidationError):
        return None
