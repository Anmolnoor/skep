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
        extra_readable: tuple[Path, ...],
        network: object,
        proxy_port: int | None,
        unix_socket_path: str | None = None,
        backend: str | None = None,
        skep_home: Path | None = None,
        worker_llm_key: bool = False,
        write_denied: tuple[Path, ...] = (),
        loopback_ports: tuple[int, ...] = (),
        unmasked: tuple[Path, ...] = (),
    ) -> Path:
        captured["workspace"] = workspace
        captured["extra_writable"] = tuple(extra_writable)
        captured["extra_readable"] = extra_readable
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
    common = (tmp_path / "repo" / ".git").resolve()
    monkeypatch.setattr(supervisor_spawner, "git_metadata_readable_roots", lambda _ws: (common,))
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
    assert captured["extra_readable"] == (common,)


def test_run_task_starts_filtering_proxy_for_resume_approved_network_host(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import dispatch as supervisor_dispatch

    config = build_config(tmp_path / "home", None)
    captured_domains: list[tuple[str, ...]] = []
    captured_ports: list[tuple[int, ...]] = []

    class _FakeProxy:
        def __init__(
            self,
            domains: tuple[str, ...],
            *,
            unix_socket_path: str | None = None,
            denied_loopback_ports: tuple[int, ...] = (),
            asks: tuple[str, ...] | list[str] = (),
            loopback_ports: tuple[int, ...] = (),
            relay: object = None,  # ADR 0062: None while the switch is off
        ) -> None:
            assert relay is None
            captured_domains.append(domains)
            captured_ports.append(denied_loopback_ports)
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
    assert captured_ports == [(8765,)]  # ES-F6: the control plane is refused
    store = RunStore(config.db_path)
    try:
        transitions = [state for state, _, _ in store.transitions_for(outcome.record.task_id)]
    finally:
        store.close()
    assert transitions == ["created", "dispatched", "worker_crashed"]


def test_proxy_env_sends_loopback_through_the_proxy_on_every_backend() -> None:
    """D6 (operator decision 2026-11-13) superseded v115-F9's NO_PROXY loopback
    exemption: loopback HTTP goes through the proxy on every backend, so a
    closed loopback port answers with a 403 naming the port and the command
    that opens it, not a bare "Operation not permitted"."""
    from skep.supervisor.spawner import _proxy_env

    env = _proxy_env(8080)
    assert "NO_PROXY" not in env and "no_proxy" not in env
    assert env["HTTP_PROXY"] == env["https_proxy"] == "http://127.0.0.1:8080"


@pytest.mark.parametrize("worker_argv", [None, ("claude-adapter",)])
def test_no_git_a_worker_starts_can_reach_a_remote(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worker_argv: tuple[str, ...] | None,
) -> None:
    """B144: the git floor reads only the command line, so an approved
    script's own ``subprocess.run(['git', 'push'])`` reached the remote. The
    worker env now allows git only its local ``file`` transport — for the
    worker, an external engine, and every grandchild the capability layer
    starts — and local git keeps working."""
    from skep.supervisor import spawner as supervisor_spawner
    from skep.workers import capabilities

    captured: dict[str, object] = {}
    config = SupervisorConfig(home=tmp_path / "home", worker_command=("w",), sandbox=False)
    task = mint_task(workspace=repo, instructions="x", budget=DEFAULT_BUDGET)
    with monkeypatch.context() as m:
        m.setattr(
            subprocess,
            "Popen",
            lambda *a, **kw: captured.update(kw) or type("P", (), {"pid": 1})(),
        )
        supervisor_spawner.spawn_worker(
            config,
            task,
            tmp_path / "t.json",
            tmp_path / "o.json",
            log_path=tmp_path / "w.log",
            worker_argv=worker_argv,
        )
    worker_env = captured["env"]
    assert isinstance(worker_env, dict)
    assert worker_env["GIT_ALLOW_PROTOCOL"] == "file"
    # A grandchild: env rebuilt by the real capability boundary, inside the worker.
    with monkeypatch.context() as m:
        m.setattr(os, "environ", dict(worker_env))
        child_env = capabilities._child_process_env(
            env_allowlist=[], env_baseline=("PATH", "HOME"), network_allowlist=()
        )
    assert child_env["GIT_ALLOW_PROTOCOL"] == "file"

    script = (
        "import subprocess, sys\n"
        "for argv in (['push', sys.argv[1], 'HEAD:main'], ['fetch', sys.argv[1]]):\n"
        "    print(subprocess.run(['git', *argv], capture_output=True, text=True).stderr)\n"
    )
    for url in (
        "https://example.invalid/x.git",
        "ssh://git@example.invalid/x",
        "git@example.invalid:x",
        "git://example.invalid/x",
    ):
        out = subprocess.run(
            [sys.executable, "-c", script, url],
            cwd=repo,
            env=child_env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert out.count("not allowed") == 2, (url, out)
    # Local git is untouched: status, a commit, a clone of a local path.
    local = subprocess.run(
        ["git", "clone", "-q", str(repo), str(tmp_path / "clone")],
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert local.returncode == 0, local.stderr
