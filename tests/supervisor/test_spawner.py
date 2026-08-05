from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task
from skep.supervisor.dispatch import run_task
from skep.supervisor.store import RunStore
from skep.worker_contract import ApprovalVerdict, AutonomyDecisionPayload, Permissions


def test_build_worker_env_strips_own_venv_from_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skep.supervisor import spawner

    monkeypatch.setattr(sys, "prefix", "/opt/skep/.venv")
    monkeypatch.setattr(sys, "base_prefix", "/usr/local")
    monkeypatch.setenv(
        "PATH", os.pathsep.join(["/opt/skep/.venv/bin", "/usr/local/bin", "/usr/bin"])
    )

    env = spawner.build_worker_env([])

    assert env["PATH"] == os.pathsep.join(["/usr/local/bin", "/usr/bin"])


def test_build_worker_env_keeps_path_when_not_in_a_venv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skep.supervisor import spawner

    monkeypatch.setattr(sys, "prefix", "/usr/local")
    monkeypatch.setattr(sys, "base_prefix", "/usr/local")
    path = os.pathsep.join(["/usr/local/bin", "/usr/bin"])
    monkeypatch.setenv("PATH", path)

    env = spawner.build_worker_env([])

    assert env["PATH"] == path


def test_spawn_worker_uses_proxy_env_for_resume_approved_network_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import spawner as supervisor_spawner

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = mint_task(
        workspace=workspace,
        instructions="Fetch metadata from example.com.",
        permissions=Permissions(
            read=["workspace"],
            write=["workspace"],
            network=[],
            env_allowlist=[],
        ),
        budget=DEFAULT_BUDGET,
        resume_of="task-suspended",
        approval_verdict=ApprovalVerdict(
            approved=True,
            actor="tester",
            ts="2026-06-16T00:00:00Z",
            reason="network.fetch requires approval with a task network allowlist",
            action="network.fetch",
            decision=AutonomyDecisionPayload(
                verdict="require_approval",
                reason="capability.require_approval.network_allowlist_missing",
                detail="example.com",
            ),
        ),
    )
    config = SupervisorConfig(
        home=tmp_path / "home",
        worker_command=("fake-worker",),
        sandbox=False,
    )
    task_path = tmp_path / "task.json"
    out_path = tmp_path / "result.json"
    log_path = tmp_path / "worker.log"
    captured: dict[str, object] = {}

    class _DummyPopen:
        def __init__(
            self,
            argv: list[str],
            *,
            env: dict[str, str],
            cwd: str,
            stdin: object,
            stdout: object,
            stderr: object,
            start_new_session: bool,
        ) -> None:
            captured["argv"] = argv
            captured["env"] = env
            captured["cwd"] = cwd
            self.pid = 12345

    monkeypatch.setattr(subprocess, "Popen", _DummyPopen)

    supervisor_spawner.spawn_worker(
        config,
        task,
        task_path,
        out_path,
        log_path=log_path,
        network_proxy_port=8765,
        sandbox_enabled=False,
    )

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["HTTP_PROXY"] == "http://127.0.0.1:8765"
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:8765"


def test_spawn_worker_grants_sandbox_write_to_worktree_gitdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import sandbox as supervisor_sandbox
    from skep.supervisor import spawner as supervisor_spawner

    workspace = tmp_path / "worktree"
    workspace.mkdir()
    gitdir = tmp_path / "repo" / ".git" / "worktrees" / "task-1"
    gitdir.mkdir(parents=True)

    task = mint_task(workspace=workspace, instructions="Edit one file.", budget=DEFAULT_BUDGET)
    config = SupervisorConfig(
        home=tmp_path / "home",
        worker_command=("fake-worker",),
        sandbox=True,
    )
    task_path = tmp_path / "task.json"
    out_path = tmp_path / "results" / "result.json"
    log_path = tmp_path / "logs" / "worker.log"
    captured: dict[str, object] = {}

    class _DummyPopen:
        def __init__(
            self,
            argv: list[str],
            *,
            env: dict[str, str],
            cwd: str,
            stdin: object,
            stdout: object,
            stderr: object,
            start_new_session: bool,
        ) -> None:
            captured["argv"] = argv
            self.pid = 12345

    def _write_profile(
        profile_path: Path,
        *,
        workspace: Path,
        extra_writable: list[Path],
        network: object,
        proxy_port: int | None,
        unix_socket_path: str | None = None,
        backend: str | None = None,
    ) -> Path:
        captured["workspace"] = workspace
        captured["extra_writable"] = tuple(extra_writable)
        return profile_path

    # v44-F7: the spawner resolves a backend then asks availability(backend).
    monkeypatch.setattr(
        supervisor_sandbox,
        "availability",
        lambda backend=None: supervisor_sandbox.SandboxAvailability(
            True, backend=backend or "bubblewrap"
        ),
    )
    monkeypatch.setattr(supervisor_sandbox, "write_profile", _write_profile)
    monkeypatch.setattr(
        supervisor_sandbox,
        "wrap_command",
        lambda argv, profile_path, backend=None: ["sandboxed", *argv],
    )
    monkeypatch.setattr(
        supervisor_spawner,
        "git_metadata_writable_roots",
        lambda _workspace: (gitdir.resolve(),),
    )
    monkeypatch.setattr(subprocess, "Popen", _DummyPopen)

    supervisor_spawner.spawn_worker(
        config,
        task,
        task_path,
        out_path,
        log_path=log_path,
        sandbox_enabled=True,
        # v109-F4: the per-run cache root dispatch passes joins the writable
        # roots — one list, consumed by every backend.
        extra_writable=(tmp_path / "cache" / "projects" / "proj-1",),
    )

    assert captured["workspace"] == workspace
    assert captured["extra_writable"] == (
        out_path.parent,
        gitdir.resolve(),
        tmp_path / "cache" / "projects" / "proj-1",
    )


def test_run_task_starts_filtering_proxy_for_resume_approved_network_host(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import dispatch as supervisor_dispatch

    config = build_config(tmp_path / "home", None)
    captured_domains: list[tuple[str, ...]] = []

    class _FakeProxy:
        def __init__(
            self, domains: tuple[str, ...], *, unix_socket_path: str | None = None
        ) -> None:
            captured_domains.append(domains)
            self.port = 8765
            self.unix_socket_path = unix_socket_path

        def start(self) -> _FakeProxy:
            return self

        def stop(self) -> None:
            return None

    def _explode_spawn(*args: object, **kwargs: object) -> None:
        raise OSError("stop after proxy start")

    monkeypatch.setattr(supervisor_dispatch, "FilteringProxy", _FakeProxy)
    monkeypatch.setattr(supervisor_dispatch, "spawn_worker", _explode_spawn)

    outcome = run_task(
        repo,
        "Fetch metadata from example.com.",
        config=config,
        permissions=Permissions(
            read=["workspace"],
            write=["workspace"],
            network=[],
            env_allowlist=[],
        ),
        approval_verdict=ApprovalVerdict(
            approved=True,
            actor="tester",
            ts="2026-06-16T00:00:00Z",
            reason="network.fetch requires approval with a task network allowlist",
            action="network.fetch",
            decision=AutonomyDecisionPayload(
                verdict="require_approval",
                reason="capability.require_approval.network_allowlist_missing",
                detail="example.com",
            ),
        ),
    )

    assert outcome.record.state == "worker_crashed"
    assert captured_domains == [("example.com",)]
    store = RunStore(config.db_path)
    try:
        transitions = [state for state, _, _ in store.transitions_for(outcome.record.task_id)]
    finally:
        store.close()
    assert transitions == ["created", "dispatched", "worker_crashed"]
