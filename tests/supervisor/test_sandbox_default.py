"""v127-F8 (ADR 0066): new projects are sandboxed when the host can sandbox.

41 of 49 coding runs in the 2026-11-18 store ran `workspace` — every
strategy default said so. A NEW project now gets `sandbox` when the probe
says the host can enforce one, else `workspace` with the probe's reason
printed. An existing project's stored mode is never rewritten: not by a
re-setup, not by a phase change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.sandbox import SandboxAvailability

from .conftest import serve_client


def _probe(usable: bool) -> Any:
    def probe(backend: str | None = None) -> SandboxAvailability:
        if usable:
            return SandboxAvailability(usable=True, backend="test")
        return SandboxAvailability(usable=False, reason="probe_failed", detail="forced off by test")

    return probe


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> Any:
    import skep.supervisor.serve.registry as registry

    def set_usable(usable: bool) -> None:
        monkeypatch.setattr(registry, "new_project_sandbox_probe", _probe(usable))

    return set_usable


def _setup(config: SupervisorConfig, repo: Path, route: str = "setup", **body: Any) -> Any:
    payload = {"project_id": "sbx", "name": "sbx", "strategy": "trusted_local_dev",
               "repo_path": str(repo), **body}  # fmt: skip
    response = serve_client(config).post(f"/api/projects/{route}", json=payload)
    assert response.status_code in (200, 201), response.text
    return response.json()


def _mode(body: dict[str, Any]) -> Any:
    return (body.get("project") or body)["policy"]["default_execution_mode"]


@pytest.mark.parametrize("route", ["setup", "preview"])
def test_a_new_project_is_sandboxed_on_a_usable_host(
    repo: Path, config: SupervisorConfig, host: Any, route: str
) -> None:
    host(True)
    assert _mode(_setup(config, repo, route)) == "sandbox"


def test_a_new_project_stays_in_the_workspace_with_the_reason(
    repo: Path, config: SupervisorConfig, host: Any
) -> None:
    host(False)
    body = _setup(config, repo)
    assert _mode(body) == "workspace"
    assert any("forced off by test" in note for note in body["notes"]), body["notes"]


def test_an_explicit_mode_wins(repo: Path, config: SupervisorConfig, host: Any) -> None:
    host(True)
    body = _setup(config, repo, policy_overrides={"default_execution_mode": "workspace"})
    assert _mode(body) == "workspace"


@pytest.mark.parametrize("stored", ["workspace", "sandbox"])
def test_a_re_setup_keeps_the_stored_mode(
    repo: Path, config: SupervisorConfig, host: Any, stored: str
) -> None:
    host(stored == "workspace")  # the probe would say the opposite
    _setup(config, repo, policy_overrides={"default_execution_mode": stored})
    host(stored != "workspace")
    assert _mode(_setup(config, repo, phase="maintain")) == stored
    assert _mode(_setup(config, repo, "preview")) == stored


def test_a_phase_change_keeps_the_stored_mode(
    repo: Path, config: SupervisorConfig, host: Any
) -> None:
    from skep.supervisor.serve.registry import set_project_phase

    host(True)
    _setup(config, repo)
    store = RunStore(config.db_path)
    try:
        moved = set_project_phase(store, "sbx", "maintain")
    finally:
        store.close()
    assert moved["policy"]["default_execution_mode"] == "sandbox"


def test_doctor_names_projects_still_in_the_workspace(tmp_path: Path) -> None:
    from skep.status import build_status

    home = tmp_path / "home"
    (home / "supervisor").mkdir(parents=True)
    store = RunStore(home / "supervisor" / "supervisor.sqlite3")
    try:
        for project_id, mode in (("old", "workspace"), ("new", "sandbox")):
            store.add_project_policy(
                project_id=project_id, name=project_id, strategy="trusted_local_dev",
                phase="build", policy={"default_execution_mode": mode},
            )  # fmt: skip
    finally:
        store.close()
    # Phase 05 5.2: a Projects-needing-upgrade line, one per project
    upgrades = {u["project_id"]: u for u in build_status(home)["project_upgrades"]}
    # no repo binding: an instruction, not a command with a `<repo>` that fails
    assert upgrades["old"]["command"] is None
    assert "--execution-mode sandbox" in upgrades["old"]["instruction"]
    assert "sandbox" not in upgrades.get("new", {"needs": []})["needs"]


def test_a_scheduled_sandbox_run_on_a_failed_probe_is_refused_with_a_health_row(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing behaviour, pinned: the dispatch chokepoint refuses a sandbox
    run the host cannot confine, and the tick records it on the schedule."""
    import skep.supervisor.dispatch as dispatch_module
    from skep.supervisor.scheduler import make_schedule, run_due

    monkeypatch.setattr(dispatch_module, "sandbox_availability", _probe(False))
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="sbx", name="sbx", strategy="trusted_local_dev", phase="build",
            policy={"default_execution_mode": "sandbox", "auto_dispatch_allowed": True},
        )  # fmt: skip
        store.add_project_binding(
            project_id="sbx", binding_kind="repo_path", binding_value=str(repo)
        )
        store.add_schedule(
            make_schedule(
                name="sbx-tick",
                repo=repo,
                instructions="Fix it. MODE:happy",
                interval_seconds=86400,
                start_at="2026-06-11T00:00:00Z",
            )
        )
        (result,) = run_due(store=store, config=config, now="2026-06-11T09:00:00Z")
        health = store.schedule_health("sbx-tick")
    finally:
        store.close()
    assert result.task_id is None
    assert "sandbox" in result.state and "forced off by test" in result.state
    assert health is not None and "sandbox" in str(health)


def test_the_cli_prints_the_reason_and_takes_an_explicit_mode(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from skep.cli import main

    home = tmp_path / "home"
    base = ["--home", str(home), "project", "setup", str(repo), "--project-id", "sbx",
            "--strategy", "trusted_local_dev", "--no-seed-schedules"]  # fmt: skip
    assert main(base) == 0
    assert "note: workers run unsandboxed (workspace)" in capsys.readouterr().out
    assert main([*base, "--execution-mode", "sandbox"]) == 0
    store = RunStore(home / "supervisor" / "supervisor.sqlite3")
    try:
        record = store.get_project_policy("sbx")
    finally:
        store.close()
    assert record is not None and record.policy["default_execution_mode"] == "sandbox"
