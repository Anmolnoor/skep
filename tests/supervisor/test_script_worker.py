"""v51-F3: the script caste worker + the Queen's run_code tool (ADR 0024).

Worker tests are hermetic (the worker function runs on a task file, no
sandbox). The end-to-end run_code tests dispatch the REAL script worker
inside a real sandbox, so they carry the same bubblewrap gate as the
egress proof.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.supervisor import SupervisorConfig, sandbox
from skep.supervisor.contracts_io import mint_task, write_task_file
from skep.worker_contract import Permissions
from skep.workers.script_worker import (
    OUTPUT_PATH,
    parse_code,
    parse_language,
    run_script_worker_task,
    script_instructions,
)

from .conftest import FAKE_WORKER
from .fake_ollama import FakeOllama
from .test_serve_chat import sse_events
from .test_serve_chat_tools import chat_client

_SANDBOX_GATE = pytest.mark.skipif(
    sandbox.availability().backend != "bubblewrap",
    reason="run_code end-to-end needs a real sandbox (bwrap backend)",
)


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


@pytest.fixture()
def config(tmp_path: Path) -> SupervisorConfig:
    """The conftest config plus the script caste registration (v42 lesson:
    an unregistered caste falls back to the coding worker and gets rejected)."""
    return SupervisorConfig(
        home=tmp_path / "skep-home",
        worker_command=(sys.executable, str(FAKE_WORKER)),
        caste_worker_commands={
            "script": (sys.executable, "-m", "skep.workers.script_worker"),
        },
        grace_seconds=0.5,
        heartbeat_seconds=0.1,
        poll_seconds=0.01,
    )


# ---------- the instruction envelope ----------


def test_instruction_envelope_roundtrips() -> None:
    text = script_instructions("python", "print(2+2)\nprint('done')")
    assert parse_language(text) == "python"
    assert parse_code(text) == "print(2+2)\nprint('done')"
    # Free-form instructions degrade sanely: whole text as python code.
    assert parse_language("print(1)") == "python"
    assert parse_code("print(1)") == "print(1)"


# ---------- the worker, hermetic ----------


def _script_task_file(
    tmp_path: Path, code: str, *, language: str = "python", worker_kind: str = "script"
) -> tuple[Path, Path]:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    task = mint_task(
        workspace=workspace,
        instructions=script_instructions(language, code),
        worker_kind=worker_kind,
        permissions=Permissions(read=["workspace"], write=["workspace"], env_allowlist=[]),
    )
    return workspace, write_task_file(task, tmp_path / "task.json")


def test_python_script_runs_and_captures_output(tmp_path: Path) -> None:
    workspace, task_file = _script_task_file(tmp_path, "print(2+2)")
    out = tmp_path / "result.json"
    assert run_script_worker_task(task_file, out) == 0
    result = json.loads(out.read_text())
    assert result["status"] == "completed"
    assert result["changed_files"] == []  # scripts compute, they never land
    assert result["commands"][0]["purpose"] == "script"
    assert (workspace / OUTPUT_PATH).read_text().strip() == "4"
    # stdout rides the event stream — the audit trail run_code reads.
    event_log = next(iter((workspace / ".events").glob("*.ndjson")))
    events = [json.loads(line) for line in event_log.read_text().splitlines()]
    command_results = [e for e in events if e["type"] == "command.result"]
    assert command_results[0]["payload"]["stdout"].strip() == "4"


def test_script_produced_files_are_declared_artifacts(tmp_path: Path) -> None:
    """v81-F6: what the script writes IS the deliverable — it must be declared
    so ingest copies it out before the worktree dies."""
    code = "from pathlib import Path\nPath('portfolio.html').write_text('<h1>hi</h1>')"
    _workspace, task_file = _script_task_file(tmp_path, code)
    out = tmp_path / "result.json"
    assert run_script_worker_task(task_file, out) == 0
    result = json.loads(out.read_text())
    assert result["status"] == "completed"
    declared = {a["path"] for a in result["artifacts"] if a["kind"] == "file"}
    assert "portfolio.html" in declared
    assert OUTPUT_PATH in declared
    assert "Produced: portfolio.html" in result["summary"]
    # The script file itself and the bookkeeping dirs are never declared.
    assert not any(p.startswith(".artifacts/script") for p in declared)


def test_script_deliverables_project_into_the_workspace(tmp_path: Path) -> None:
    """v81-F6: declared script files ride the same delivery lane as research
    reports, and the run_code tool result points at them."""
    from skep.supervisor.ingest import deliver_research_artifacts
    from skep.supervisor.serve.tools import _script_run_result
    from skep.supervisor.store import RunStore

    workspace = tmp_path / "ws"
    workspace.mkdir()
    task = mint_task(
        workspace=workspace,
        instructions=script_instructions("python", "pass"),
        worker_kind="script",
        permissions=Permissions(read=["workspace"], write=["workspace"], env_allowlist=[]),
    )
    audit = tmp_path / "audit"
    audit.mkdir()
    (audit / "portfolio.html").write_text("<h1>hi</h1>")
    (audit / "output.txt").write_text("stdout capture")

    store = RunStore(tmp_path / "db.sqlite")
    try:
        store.create_run(task, repo=tmp_path, ref=None, execution_mode="sandbox")
        store.transition(task.task_id, "completed")
        store.add_artifact(
            task.task_id, kind="file", audit_path=audit / "portfolio.html", sha256="x"
        )
        store.add_artifact(task.task_id, kind="file", audit_path=audit / "output.txt", sha256="x")

        target = deliver_research_artifacts(
            store, task=task, audit_task_dir=audit, delivery_root=tmp_path / "deliver"
        )
        assert target is not None
        assert (target / "portfolio.html").read_text() == "<h1>hi</h1>"
        # stdout capture is audit evidence, not a deliverable.
        assert not (target / "output.txt").exists()

        result = _script_run_result(store, task.task_id)
    finally:
        store.close()
    assert result["delivered_to"] == str(target)
    assert result["delivered_files"] == ["portfolio.html"]


def test_failing_script_reports_honestly(tmp_path: Path) -> None:
    _workspace, task_file = _script_task_file(tmp_path, "import sys\nsys.exit(3)")
    out = tmp_path / "result.json"
    assert run_script_worker_task(task_file, out) != 0
    result = json.loads(out.read_text())
    assert result["status"] == "failed"
    assert result["verification"]["outcome"] == "failed"
    assert "exited 3" in result["verification"]["details"]


def test_shell_language_runs(tmp_path: Path) -> None:
    workspace, task_file = _script_task_file(tmp_path, "echo hive", language="shell")
    out = tmp_path / "result.json"
    assert run_script_worker_task(task_file, out) == 0
    assert (workspace / OUTPUT_PATH).read_text().strip() == "hive"


def test_unknown_language_fails_before_running(tmp_path: Path) -> None:
    _workspace, task_file = _script_task_file(tmp_path, "DISPLAY 'HI'.", language="cobol")
    out = tmp_path / "result.json"
    assert run_script_worker_task(task_file, out) != 0
    result = json.loads(out.read_text())
    assert result["status"] == "failed"
    assert result["verification"]["outcome"] == "not_attempted"
    assert "cobol" in result["summary"]


def test_wrong_caste_is_rejected(tmp_path: Path) -> None:
    _workspace, task_file = _script_task_file(tmp_path, "print(1)", worker_kind="coding")
    out = tmp_path / "result.json"
    assert run_script_worker_task(task_file, out) == 5
    assert json.loads(out.read_text())["status"] == "rejected"


def test_script_caste_is_registered_in_the_default_config(tmp_path: Path) -> None:
    # The v42 lesson, pinned for this caste too: an unregistered caste falls
    # back to the coding worker and gets rejected.
    from skep.supervisor.cli_cmds import build_config

    config = build_config(tmp_path, None)
    command = config.command_for("script")
    assert command != config.worker_command
    assert command[-2:] == ("-m", "skep.workers.script_worker")


# ---------- run_code end to end (real sandbox) ----------


@_SANDBOX_GATE
def test_run_code_cards_then_confirm_runs_sandboxed(
    repo: Path, config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """An unbound repo cards; confirming dispatches the sandboxed script
    worker and the script's stdout comes back as the tool result."""
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("run_code", {"repo": str(repo), "code": "print(2+2)"})
    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "calc"}).text
    )
    actions = [d for name, d in events if name == "action"]
    assert len(actions) == 1
    assert actions[0]["tool"] == "run_code"
    assert actions[0]["args"]["code"] == "print(2+2)"  # the card shows the code verbatim
    assert events[-1] == ("done", {"state": "awaiting_confirmation"})

    action_id = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]
    ollama.script_reply("the answer is 4")
    confirm = sse_events(client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm").text)
    assert confirm[-1] == ("done", {"state": "complete"})

    resolved = client.get(f"/api/chats/{chat_id}").json()["actions"][0]
    assert resolved["status"] == "confirmed"
    payload = resolved["result"]["result"]
    assert payload["state"] == "completed"
    assert payload["exit_code"] == 0
    assert payload["output"].strip() == "4"


@_SANDBOX_GATE
def test_run_code_auto_runs_on_a_trusted_project(
    repo: Path, config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """Where dispatch_run would auto-dispatch, run_code runs without a card —
    the script envelope is strictly tighter than the trusted default."""
    client, chat_id = chat_client(config, ollama)
    created = client.post(
        "/api/projects",
        json={
            "project_id": "trusted-fixture",
            "name": "Trusted Fixture",
            "strategy": "trusted_local_dev",
            "phase": "build",
            "policy": {
                "default_execution_mode": "workspace",
                "auto_dispatch_allowed": True,
            },
            "bindings": [{"kind": "repo_path", "value": str(repo)}],
        },
    )
    assert created.status_code == 201
    ollama.script_tool_call("run_code", {"repo": str(repo), "code": "print('hive'*2)"})
    ollama.script_reply("ran it")
    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "calc"}).text
    )
    assert [name for name, _ in events].count("action") == 0  # no card
    tool_events = [d for name, d in events if name == "tool"]
    assert tool_events[0]["tool"] == "run_code"
    payload = tool_events[0]["result"]["result"]
    assert payload["state"] == "completed"
    assert payload["output"].strip() == "hivehive"
    # The run is a real, separately audited worker run.
    run = client.get(f"/api/runs/{payload['task_id']}").json()["run"]
    assert run["state"] == "completed"


def test_run_code_timeout_seconds_is_honored_and_capped(
    repo: Path, config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """v106-F7: a caller who knows the work is slow raises the wall clock
    (two field npm scripts died at exactly the 120s default); the ceiling
    still binds — a wild ask clamps to 600."""
    import json as json_mod

    client, chat_id = chat_client(config, ollama)
    client.post(
        "/api/projects",
        json={
            "project_id": "trusted-timeout",
            "name": "Trusted Timeout",
            "strategy": "trusted_local_dev",
            "phase": "build",
            "policy": {
                "default_execution_mode": "workspace",
                "auto_dispatch_allowed": True,
            },
            "bindings": [{"kind": "repo_path", "value": str(repo)}],
        },
    )
    ollama.script_tool_call(
        "run_code",
        {"repo": str(repo), "code": "print('ok')", "timeout_seconds": 9999},
    )
    ollama.script_reply("ran it")
    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "slow calc"}).text
    )
    tool_events = [d for name, d in events if name == "tool"]
    payload = tool_events[0]["result"]["result"]
    assert payload["state"] == "completed"
    # The audited task envelope carries the clamped budget: 9999 → 600.
    task = json_mod.loads((config.audit_dir / payload["task_id"] / "task.json").read_text())
    assert task["budget"]["wall_clock_seconds"] == 600
