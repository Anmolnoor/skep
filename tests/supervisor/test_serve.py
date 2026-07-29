"""``skep serve`` Stage A tests — hermetic: fake worker, TestClient, no network."""

from __future__ import annotations

import json
import time
from pathlib import Path

from skep.supervisor import RunStore, SupervisorConfig, mint_task

from .conftest import git
from .conftest import serve_client as _client
from .conftest import wait_terminal as _wait_terminal


def test_status_is_healthy_against_a_real_store(config: SupervisorConfig) -> None:
    payload = _client(config).get("/api/status").json()
    assert payload["status"] == "ok"
    assert payload["store_ready"] is True
    assert payload["pending_approvals"] == 0


def test_run_detail_carries_remediation_for_a_known_failure(
    repo: Path, config: SupervisorConfig
) -> None:
    """v19-F12: a failed run's JSON carries the one-line remediation hint."""
    from skep.worker_contract import (
        CONTRACT_VERSION,
        Artifact,
        CodingWorkerResult,
        TaskState,
        Usage,
        Verification,
        VerificationOutcome,
    )

    store = RunStore(config.db_path)
    try:
        task = mint_task(workspace=repo, instructions="Switch to main.")
        store.create_run(task, repo=repo, ref=None, execution_mode="sandbox")
        store.transition(task.task_id, "failed")
        store.record_result(
            task.task_id,
            CodingWorkerResult(
                contract_version=CONTRACT_VERSION,
                task_id=task.task_id,
                trace_id=task.trace_id,
                status=TaskState.FAILED,
                summary="switch to main; command failed.",
                changed_files=[],
                commands=[],
                verification=Verification(
                    outcome=VerificationOutcome.NOT_ATTEMPTED,
                    details="fatal: 'main' is already used by worktree at /x",
                ),
                artifacts=[Artifact(kind="event_log", path="events.ndjson", sha256="")],
                usage=Usage(provider_calls=1),
            ),
        )
    finally:
        store.close()

    detail = _client(config).get(f"/api/runs/{task.task_id}").json()
    assert "as a patch" in detail["run"]["remediation"]


def test_dispatch_over_http_returns_202_and_runs_to_completion(
    repo: Path, config: SupervisorConfig
) -> None:
    client = _client(config)
    response = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "workspace",
        },
    )
    assert response.status_code == 202
    task_id = response.json()["task_id"]

    # 202 means the run row already exists — queryable before it finishes.
    assert client.get(f"/api/runs/{task_id}").status_code == 200

    run = _wait_terminal(client, task_id)
    assert run["state"] == "completed"
    assert run["verification_outcome"] == "passed"

    detail = client.get(f"/api/runs/{task_id}").json()
    assert {a["kind"] for a in detail["artifacts"]} == {"event_log", "patch"}
    assert len(detail["commands"]) == 1
    command = detail["commands"][0]
    assert {
        "command": command["command"],
        "exit_code": command["exit_code"],
        "purpose": command["purpose"],
    } == {"command": 'grep -q "value = 1" existing.py', "exit_code": 0, "purpose": "verify"}
    assert command["stdout_tail"] == ""
    assert command["stderr_tail"] == ""
    assert isinstance(command["duration_ms"], int)
    assert detail["transitions"][-1]["state"] == "completed"

    runs = client.get("/api/runs").json()["runs"]
    assert runs[0]["task_id"] == task_id

    diff = client.get(f"/api/runs/{task_id}/diff")
    assert diff.status_code == 200
    assert "value = 1" in diff.text


def test_runs_list_reports_bound_project_context(repo: Path, config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="project-1",
            name="trusted repo",
            strategy="trusted_local_dev",
            phase="maintain",
            policy={"default_execution_mode": "workspace"},
        )
        store.add_project_binding(
            project_id="project-1",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
    finally:
        store.close()

    client = _client(config)
    task_id = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "workspace",
        },
    ).json()["task_id"]
    _wait_terminal(client, task_id)

    runs = client.get("/api/runs").json()["runs"]
    assert runs[0]["task_id"] == task_id
    assert runs[0]["project_context"] == {
        "project_id": "project-1",
        "name": "trusted repo",
        "strategy": "trusted_local_dev",
        "phase": "maintain",
        "binding_kind": "repo_path",
        "binding_value": str(repo),
    }


def test_post_rejects_a_non_repo_target(tmp_path: Path, config: SupervisorConfig) -> None:
    response = _client(config).post(
        "/api/runs",
        json={
            "repo": str(tmp_path / "nowhere"),
            "instructions": "x",
            "execution_mode": "workspace",
        },
    )
    assert response.status_code == 400


def test_dispatch_auto_initializes_a_plain_local_folder(
    tmp_path: Path, config: SupervisorConfig
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "existing.py").write_text("value = 0\n")

    client = _client(config)
    response = client.post(
        "/api/runs",
        json={
            "repo": str(plain),
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "workspace",
        },
    )

    assert response.status_code == 202
    task_id = response.json()["task_id"]
    run = _wait_terminal(client, task_id)
    assert run["state"] == "completed"
    assert (plain / ".git").is_dir()
    git(plain, "rev-parse", "--verify", "HEAD")
    assert git(plain, "ls-files").stdout.splitlines() == ["existing.py"]


def test_unknown_run_is_404_everywhere(config: SupervisorConfig) -> None:
    client = _client(config)
    assert client.get("/api/runs/nope").status_code == 404
    assert client.get("/api/runs/nope/events").status_code == 404
    assert client.get("/api/runs/nope/diff").status_code == 404


def test_sse_stream_replays_events_and_closes_on_terminal(
    repo: Path, config: SupervisorConfig
) -> None:
    client = _client(config)
    task_id = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "workspace",
        },
    ).json()["task_id"]

    types: list[str] = []
    done: dict[str, object] | None = None
    with client.stream("GET", f"/api/runs/{task_id}/events?stream=1") as stream:
        pending_name = ""
        for line in stream.iter_lines():
            if line.startswith("event: "):
                pending_name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                payload = json.loads(line.removeprefix("data: "))
                if pending_name == "done":
                    done = payload
                else:
                    types.append(str(payload["type"]))
                pending_name = ""

    assert types[0] == "run.created"
    assert types[1] == "task.start"
    assert "task.terminal" in types
    assert types[-1] == "reverify.result"
    assert types.index("reverify.result") > types.index("task.terminal")
    assert done is not None and done["state"] == "completed"

    # After ingest the worktree is gone; the plain endpoint serves the store copy.
    events = client.get(f"/api/runs/{task_id}/events").json()["events"]
    assert [e["type"] for e in events] == types


def test_events_endpoint_serves_the_audit_trail_after_completion(
    repo: Path, config: SupervisorConfig
) -> None:
    client = _client(config)
    task_id = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "workspace",
        },
    ).json()["task_id"]
    _wait_terminal(client, task_id)
    events: list[dict[str, object]] = []
    for _ in range(20):
        events = client.get(f"/api/runs/{task_id}/events").json()["events"]
        if any(event["type"] == "reverify.result" for event in events):
            break
        time.sleep(0.05)
    types = [event["type"] for event in events]
    assert events[0]["type"] == "run.created"
    assert events[1]["type"] == "task.start"
    assert "task.terminal" in types
    assert events[-1]["type"] == "reverify.result"
    assert types.index("reverify.result") > types.index("task.terminal")
    assert events[-1]["payload"] == {
        "outcome": "passed",
        "worker_outcome": "passed",
        "confirmed": True,
        "commands": ['grep -q "value = 1" existing.py'],
        "exit_codes": [0],
        # v88-F4: the detail names WHICH command was re-run — "passed" means
        # something different when the worker picked it than when the project
        # did (I8). The pin moved with the change.
        "detail": "re-ran clean: all exit 0 [command from the worker's own verify step]",
    }
    assert all(e["task_id"] == task_id for e in events)


def test_run_detail_prefers_live_event_log_for_policy_blocks_and_approval_decision(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    workspace = tmp_path / "live-workspace"
    events_dir = workspace / ".events"
    events_dir.mkdir(parents=True)

    store = RunStore(config.db_path)
    try:
        task = mint_task(
            workspace=workspace,
            instructions="Use a shell command that needs approval.",
        )
        store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
        store.transition(task.task_id, "pending_approval")
        store.enqueue_approval(
            task.task_id,
            action="shell.run",
            reason="shell.run requires approval",
        )
    finally:
        store.close()

    lines = [
        {
            "contract_version": task.contract_version,
            "event_id": "e-1",
            "seq": 1,
            "task_id": task.task_id,
            "trace_id": task.trace_id,
            "ts": "2026-06-15T00:00:00Z",
            "type": "approval.requested",
            "payload": {
                "action": "shell.run",
                "reason": "shell.run requires approval",
                "decision": {
                    "verdict": "require_approval",
                    "reason": "capability.require_approval.shell_nonverify_not_allowlisted",
                    "detail": "python write.py",
                    "decided_by": None,  # v40-F8 additive field
                },
            },
        },
        {
            "contract_version": task.contract_version,
            "event_id": "e-2",
            "seq": 2,
            "task_id": task.task_id,
            "trace_id": task.trace_id,
            "ts": "2026-06-15T00:00:01Z",
            "type": "command.result",
            "payload": {
                "command": "python write.py",
                "exit_code": 1,
                "duration_ms": 5,
                "stdout_tail": "",
                "stderr_tail": "",
                "capability_id": "shell.run",
                "error": "shell.run requires approval",
                "decision": {
                    "verdict": "require_approval",
                    "reason": "capability.require_approval.shell_nonverify_not_allowlisted",
                    "detail": "python write.py",
                    "decided_by": None,  # v40-F8 additive field
                },
            },
        },
    ]
    (events_dir / f"{task.task_id}.ndjson").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n",
        encoding="utf-8",
    )

    detail = _client(config).get(f"/api/runs/{task.task_id}").json()
    assert detail["approvals"][0]["decision"] == {
        "verdict": "require_approval",
        "reason": "capability.require_approval.shell_nonverify_not_allowlisted",
        "detail": "python write.py",
        "decided_by": None,  # v40-F8 additive field
    }
    assert detail["policy_blocks"] == [
        {
            "type": "command.result",
            "capability_id": "shell.run",
            "command": "python write.py",
            "decision": {
                "verdict": "require_approval",
                "reason": "capability.require_approval.shell_nonverify_not_allowlisted",
                "detail": "python write.py",
                "decided_by": None,  # v40-F8 additive field
            },
            "detail": "shell.run requires approval",
        }
    ]


def test_run_detail_does_not_report_policy_blocks_for_constrained_allowed_commands(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    workspace = tmp_path / "live-workspace"
    events_dir = workspace / ".events"
    events_dir.mkdir(parents=True)

    store = RunStore(config.db_path)
    try:
        task = mint_task(
            workspace=workspace,
            instructions="Run a policy-allowed constrained command.",
        )
        store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
        store.transition(task.task_id, "completed")
    finally:
        store.close()

    lines = [
        {
            "contract_version": task.contract_version,
            "event_id": "e-1",
            "seq": 1,
            "task_id": task.task_id,
            "trace_id": task.trace_id,
            "ts": "2026-06-15T00:00:00Z",
            "type": "command.result",
            "payload": {
                "command": "echo ok",
                "exit_code": 0,
                "duration_ms": 5,
                "stdout_tail": "ok\n",
                "stderr_tail": "",
                "capability_id": "shell.run",
                "decision": {
                    "verdict": "allow_with_constraints",
                    "reason": "capability.allow.shell_allowlist_prefix",
                    "detail": "echo ok",
                    "decided_by": None,  # v40-F8 additive field
                },
            },
        }
    ]
    (events_dir / f"{task.task_id}.ndjson").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n",
        encoding="utf-8",
    )

    detail = _client(config).get(f"/api/runs/{task.task_id}").json()
    assert detail["policy_blocks"] == []


def test_runs_list_includes_dispatch_and_landing_decisions(
    repo: Path, config: SupervisorConfig
) -> None:
    client = _client(config)
    task_id = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "workspace",
        },
    ).json()["task_id"]
    _wait_terminal(client, task_id)

    runs = client.get("/api/runs").json()["runs"]
    assert runs[0]["task_id"] == task_id
    assert runs[0]["dispatch_decision"] == {
        "verdict": "allow",
        "reason": "dispatch.allow.run_request_resolved",
        "detail": "no project binding; global defaults",
        "decided_by": None,  # v40-F8 additive field
    }
    assert runs[0]["landing_decision"] == {
        "verdict": "require_approval",
        "reason": "landing.require_approval.no_auto_apply_rule",
        "detail": None,
        "decided_by": None,  # v40-F8 additive field
    }


def test_run_event_views_carry_the_fields_the_ui_consumes(
    repo: Path, config: SupervisorConfig
) -> None:
    """v40-F3 (v35): the chat's worker-activity block reads event_id, type,
    ts, and payload off every event view — pin the shape."""
    client = _client(config)
    task_id = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "workspace",
        },
    ).json()["task_id"]
    _wait_terminal(client, task_id)

    events = client.get(f"/api/runs/{task_id}/events").json()["events"]
    assert events
    for view in events:
        assert isinstance(view.get("event_id"), str) and view["event_id"]
        assert isinstance(view.get("type"), str) and view["type"]
        assert isinstance(view.get("ts"), str) and view["ts"]
        assert isinstance(view.get("payload"), dict)
