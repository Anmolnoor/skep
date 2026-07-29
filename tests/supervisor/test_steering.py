"""v69-F4 (R12a): mid-run steering — input into a running react loop.

Steering is INPUT, never authority: it resolves no card, approval, or gate.
The endpoint only reaches a RUNNING react run; the worker consumes new notes
as observations before its next action; everything is recorded.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from skep.supervisor import RunStore, SupervisorConfig, mint_task
from skep.supervisor.contracts_io import write_task_file
from skep.worker_contract import EventType

from .conftest import serve_client


def _running_run(
    config: SupervisorConfig,
    tmp_path: Path,
    *,
    protocol: str,
    state: str = "running",
) -> str:
    workspace = tmp_path / "worktree"
    workspace.mkdir(parents=True, exist_ok=True)
    task = mint_task(
        workspace=workspace,
        instructions="Do the thing, reactively." * 5,
        planning_protocol=protocol,
    )
    store = RunStore(config.db_path)
    try:
        store.create_run(task, repo=tmp_path, ref=None, execution_mode="workspace")
        if state != "queued":
            store.transition(task.task_id, state)
    finally:
        store.close()
    write_task_file(task, config.audit_dir / task.task_id / "task.json")
    return task.task_id


def test_steer_reaches_a_running_react_run(config: SupervisorConfig, tmp_path: Path) -> None:
    client: TestClient = serve_client(config)
    task_id = _running_run(config, tmp_path, protocol="react")

    response = client.post(f"/api/runs/{task_id}/steer", json={"text": "also rename X"})
    assert response.status_code == 201
    assert response.json() == {"task_id": task_id, "steered": True}

    steering_file = tmp_path / "worktree" / ".artifacts" / "steering.jsonl"
    (line,) = steering_file.read_text(encoding="utf-8").splitlines()
    assert json.loads(line) == {"actor": "operator", "text": "also rename X"}
    store = RunStore(config.db_path)
    try:
        rows = store.steering_for(task_id)
    finally:
        store.close()
    assert [(actor, text) for actor, text, _ts in rows] == [("operator", "also rename X")]


def test_steer_teaches_when_the_protocol_or_state_is_wrong(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    client: TestClient = serve_client(config)

    plan_run = _running_run(config, tmp_path / "a", protocol="plan")
    response = client.post(f"/api/runs/{plan_run}/steer", json={"text": "x"})
    assert response.status_code == 409
    assert "react" in response.json()["detail"]

    finished = _running_run(config, tmp_path / "b", protocol="react", state="completed")
    response = client.post(f"/api/runs/{finished}/steer", json={"text": "x"})
    assert response.status_code == 409
    assert "RUNNING" in response.json()["detail"]


def test_worker_consumes_new_steering_as_observations(tmp_path: Path) -> None:
    """The loop reads only NEW lines, emits the audit heartbeat, and appends
    the note as an observation; a resume skips notes that predate it."""
    from skep.workers.coding_minimal import _consume_steering, _steering_line_count

    events: list[tuple[EventType, dict[str, object]]] = []

    class _Stream:
        def emit(self, event_type: EventType, payload: dict[str, object]) -> None:
            events.append((event_type, payload))

    steering = tmp_path / ".artifacts"
    steering.mkdir()
    path = steering / "steering.jsonl"
    path.write_text(
        json.dumps({"actor": "operator", "text": "focus on the CLI"}) + "\n",
        encoding="utf-8",
    )

    conversation: list[dict[str, object]] = []
    consumed = _consume_steering(tmp_path, 0, conversation, _Stream())  # type: ignore[arg-type]
    assert consumed == 1
    assert "focus on the CLI" in str(conversation[-1]["content"])
    assert events and events[0][1]["phase"] == "steering received"

    # No new lines → nothing appended; a stale-skip init sees the same count.
    assert _consume_steering(tmp_path, consumed, conversation, _Stream()) == 1  # type: ignore[arg-type]
    assert len(conversation) == 1
    assert _steering_line_count(tmp_path) == 1
