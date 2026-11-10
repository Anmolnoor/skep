"""ES-F1 (B47, I12/I8): a sandbox that is requested but unusable fails CLOSED.

Before: ``spawn_worker`` wrapped the worker only ``if use_sandbox and
availability().usable`` with no else, re-verify and diagnose ran their commands
bare when the probe said no, and dispatch refused only external engines — so a
host with a missing or rejected bwrap/sandbox-exec ran every "sandbox" run
unconfined while the record still said ``execution_mode=sandbox``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from skep.supervisor import RunStore, SupervisorConfig, run_task
from skep.supervisor import sandbox as supervisor_sandbox
from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task

_UNUSABLE = supervisor_sandbox.SandboxAvailability(False, "missing_binary", "bwrap not on PATH")


def _sandbox_unusable(monkeypatch: pytest.MonkeyPatch) -> None:
    import skep.supervisor.dispatch as dispatch_module

    monkeypatch.setattr(supervisor_sandbox, "availability", lambda backend=None: _UNUSABLE)
    monkeypatch.setattr(dispatch_module, "sandbox_availability", lambda backend=None: _UNUSABLE)


def test_write_profile_refuses_when_the_backend_is_unusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one chokepoint: no profile (and so no wrapped argv) without a wall."""
    _sandbox_unusable(monkeypatch)
    with pytest.raises(supervisor_sandbox.SandboxUnavailableError, match="missing_binary"):
        supervisor_sandbox.write_profile(tmp_path / "p.sb", workspace=tmp_path)
    assert not (tmp_path / "p.sb").exists()


def test_spawn_worker_refuses_instead_of_running_unconfined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import spawner

    _sandbox_unusable(monkeypatch)
    started: list[object] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: started.append(a))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    task = mint_task(workspace=workspace, instructions="x", budget=DEFAULT_BUDGET)
    config = SupervisorConfig(home=tmp_path / "home", worker_command=("w",), sandbox=True)

    with pytest.raises(supervisor_sandbox.SandboxUnavailableError):
        spawner.spawn_worker(
            config, task, tmp_path / "t.json", tmp_path / "o.json", log_path=tmp_path / "w.log"
        )
    assert started == [], "the worker must never start unconfined"


def test_dispatch_refuses_a_builtin_sandbox_run_before_any_record(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The v94-F4 chokepoint covered external engines only; the builtin worker
    (and the script caste behind run_code) took the silent fallback."""
    _sandbox_unusable(monkeypatch)
    with pytest.raises(ValueError) as excinfo:
        run_task(repo, "Fix the bug. MODE:happy", config=config, execution_mode="sandbox")
    message = str(excinfo.value)
    assert "sandbox" in message and "missing_binary" in message
    assert "workspace" in message  # names the way forward (I9)
    store = RunStore(config.db_path)
    try:
        assert store.recent_runs() == [], "no run record may claim a sandbox it never had"
    finally:
        store.close()


def test_reverify_refuses_to_run_patch_code_unconfined(
    repo: Path, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor.reverify import reverify_run

    happy = run_task(repo, "Fix the bug. MODE:happy", config=config)
    assert happy.record.state == "completed"
    _sandbox_unusable(monkeypatch)
    marker = tmp_path / "ran-on-host"
    store = RunStore(config.db_path)
    try:
        outcome = reverify_run(
            store=store,
            task_id=happy.record.task_id,
            worker_outcome="passed",
            repo=repo,
            ref=None,
            config=config,
            verify_command=f"touch {marker}",
        )
    finally:
        store.close()
    assert outcome.outcome == "unavailable"
    assert "sandbox" in outcome.detail
    assert not marker.exists(), "the patch's verify command ran unsandboxed"


def test_diagnose_refuses_to_run_unconfined(
    repo: Path, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor.serve.actions import diagnose_run

    workspace = tmp_path / "kept"
    workspace.mkdir()
    marker = tmp_path / "ran-on-host"
    store = RunStore(config.db_path)
    try:
        task = mint_task(workspace=workspace, instructions="x", budget=DEFAULT_BUDGET)
        store.create_run(task, repo=repo, ref=None, execution_mode="sandbox")
        _sandbox_unusable(monkeypatch)
        with pytest.raises(ValueError, match="sandbox"):
            diagnose_run(store, config, task.task_id, command=f"touch {marker}")
    finally:
        store.close()
    assert not marker.exists()


def test_a_spawn_time_sandbox_refusal_ends_the_run_without_starting_it(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backstop: the backend the spawner resolves can differ from the one the
    up-front check saw (podman → native for a resumed host grant). The run
    then ends before the worker exists, and says why — never a bare retry."""
    import skep.supervisor.dispatch as dispatch_module

    def _refuse(*_a: object, **_kw: object) -> None:
        raise supervisor_sandbox.SandboxUnavailableError("no usable sandbox backend (test)")

    monkeypatch.setattr(dispatch_module, "spawn_worker", _refuse)
    outcome = run_task(repo, "Fix the bug. MODE:happy", config=config)
    assert outcome.record.state == "worker_crashed"
    log = (config.audit_dir / outcome.record.task_id / "worker.log").read_text()
    assert log.startswith("sandbox_unavailable: no usable sandbox backend")
