"""Stage B (v5): persisted settings + PUT /api/policy change the next run."""

from __future__ import annotations

import json
import shlex
import sys
import time
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from skep.supervisor import SupervisorConfig
from skep.supervisor.autonomy import AutonomyDecision
from skep.supervisor.serve import actions
from skep.supervisor.serve.app import TERMINAL_STATES
from skep.supervisor.serve.jobs import Dispatcher
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import execute_mutation
from skep.supervisor.store import RunStore

from .conftest import FAKE_WORKER, git, wait_terminal
from .conftest import serve_client as _client
from .fake_openai import FakeOpenAI

FAKE_WORKER_CMD = shlex.join([sys.executable, str(FAKE_WORKER)])
V9_GOLDEN_FIXTURE = Path(__file__).parents[1] / "fixtures" / "project_policy_v9_golden.json"


def _run_to_terminal(client: TestClient, repo: Path, instructions: str) -> dict[str, object]:
    task_id = client.post(
        "/api/runs",
        json={"repo": str(repo), "instructions": instructions, "execution_mode": "workspace"},
    ).json()["task_id"]
    return wait_terminal(client, task_id)


def _branch_exists(repo: Path, task_id: object) -> bool:
    return bool(git(repo, "branch", "--list", f"skep/{task_id}").stdout.strip())


def _wait_branch(repo: Path, task_id: object, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _branch_exists(repo, task_id):
            return True
        time.sleep(0.05)
    return _branch_exists(repo, task_id)


def _wait_integration_branch(
    repo: Path, branch: str = "skep/maintain", timeout: float = 5.0
) -> bool:
    """v30: maintain-phase auto-apply accumulates on ONE integration branch."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if git(repo, "branch", "--list", branch).stdout.strip():
            return True
        time.sleep(0.05)
    return bool(git(repo, "branch", "--list", branch).stdout.strip())


def _wait_applied_branch(client: TestClient, task_id: object, timeout: float = 5.0) -> str | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        branch = client.get(f"/api/runs/{task_id}").json()["applied_branch"]
        if branch is not None:
            return str(branch)
        time.sleep(0.05)
    branch = client.get(f"/api/runs/{task_id}").json()["applied_branch"]
    return None if branch is None else str(branch)


def _project_dispatch_decision(
    *, reason: str, project_id: str, strategy: str = "trusted_local_dev", phase: str
) -> dict[str, object]:
    return {
        "verdict": "allow",
        "reason": reason,
        "detail": None,
        "decided_by": None,  # v40-F8 additive field
        "project_id": project_id,
        "strategy": strategy,
        "phase": phase,
        "policy_source": "project_policy",
        # v23-F5: trusted dev workspace runs with no explicit network resolve
        # the package-registry hosts into the audit constraints.
        "constraints": {
            "network_requested": None,
            "network_resolved": [
                "files.pythonhosted.org",
                "proxy.golang.org",
                "pypi.org",
                "registry.npmjs.org",
            ],
        },
    }


def _load_v9_golden_policy(repo_path: Path) -> dict[str, object]:
    payload = cast(dict[str, Any], json.loads(V9_GOLDEN_FIXTURE.read_text(encoding="utf-8")))
    payload["bindings"] = [{"kind": "repo_path", "value": str(repo_path)}]
    return cast(dict[str, object], payload)


def test_policy_defaults_then_roundtrip_and_persistence(config: SupervisorConfig) -> None:
    client = _client(config)
    policy = client.get("/api/policy").json()
    assert policy["auto_approve"] is False
    assert policy["default_network"] == []
    assert policy["default_execution_mode"] == "ask"
    assert policy["trusted_workspace_roots"] == []
    assert policy["sandbox_required_for"] == ["email", "browser", "secrets", "unknown_repo"]
    assert policy["ticker_interval_seconds"] == 30
    assert policy["default_wall_clock_seconds"] == 900
    assert policy["default_max_iterations"] == 16
    assert policy["default_max_actions"] == 100
    assert policy["default_max_provider_calls"] == 64
    assert policy["allowed_shell_commands"] == []
    assert policy["allowed_plugin_risks"] == []

    updated = client.put(
        "/api/policy",
        json={
            "auto_approve": True,
            "default_network": ["pypi.org"],
            "default_env_allowlist": ["CI"],
            "default_execution_mode": "workspace",
            "trusted_workspace_roots": ["/workspace/Developer"],
            "sandbox_required_for": ["email", "browser"],
            "ticker_interval_seconds": 5,
            "default_wall_clock_seconds": 600,
            "default_max_iterations": 8,
            "default_max_actions": 40,
            "default_max_provider_calls": 20,
            "allowed_shell_commands": [["pytest"], ["python", "-m", "pytest"]],
            "allowed_plugin_risks": ["write"],
        },
    ).json()
    assert updated["auto_approve"] is True
    assert updated["default_network"] == ["pypi.org"]
    assert updated["default_execution_mode"] == "workspace"
    assert updated["trusted_workspace_roots"] == ["/workspace/Developer"]
    assert updated["sandbox_required_for"] == ["email", "browser"]
    assert updated["ticker_interval_seconds"] == 5
    assert updated["default_wall_clock_seconds"] == 600
    assert updated["default_max_iterations"] == 8
    assert updated["default_max_actions"] == 40
    assert updated["default_max_provider_calls"] == 20
    assert updated["allowed_shell_commands"] == [["pytest"], ["python", "-m", "pytest"]]
    assert updated["allowed_plugin_risks"] == ["write"]

    # A fresh app over the same home (a restart) sees the stored settings.
    rebooted = _client(config).get("/api/policy").json()
    assert rebooted["auto_approve"] is True
    assert rebooted["default_network"] == ["pypi.org"]
    assert rebooted["default_env_allowlist"] == ["CI"]
    assert rebooted["default_execution_mode"] == "workspace"
    assert rebooted["trusted_workspace_roots"] == ["/workspace/Developer"]
    assert rebooted["sandbox_required_for"] == ["email", "browser"]
    assert rebooted["ticker_interval_seconds"] == 5
    assert rebooted["default_wall_clock_seconds"] == 600
    assert rebooted["default_max_iterations"] == 8
    assert rebooted["default_max_actions"] == 40
    assert rebooted["default_max_provider_calls"] == 20
    assert rebooted["allowed_shell_commands"] == [["pytest"], ["python", "-m", "pytest"]]
    assert rebooted["allowed_plugin_risks"] == ["write"]


def test_shell_command_policy_rejects_dangerous_prefixes(config: SupervisorConfig) -> None:
    client = _client(config)

    response = client.put("/api/policy", json={"allowed_shell_commands": [["bash", "-lc"]]})

    assert response.status_code == 400
    assert "too broad" in response.json()["detail"]


def test_dispatch_decision_records_network_requested_and_resolved(
    repo: Path, config: SupervisorConfig
) -> None:
    """v19-F11: the dispatch decision carries the requested vs resolved network."""
    client = _client(config)
    task_id = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "workspace",
            "network": ["github.com", "api.example.com"],
        },
    ).json()["task_id"]
    run = wait_terminal(client, task_id)
    task = json.loads((config.audit_dir / str(run["task_id"]) / "task.json").read_text())
    constraints = task["dispatch_decision"]["constraints"]
    assert constraints["network_requested"] == ["github.com", "api.example.com"]
    # Resolved is sorted + deduped for reproducibility.
    assert constraints["network_resolved"] == ["api.example.com", "github.com"]
    assert task["permissions"]["network"] == ["api.example.com", "github.com"]


def test_rest_dispatch_takes_an_engine_and_keeps_its_walls(
    repo: Path, config: SupervisorConfig
) -> None:
    """v100-F9: the per-dispatch engine has been on the chat tool since v95-F3
    and `submit_run` has taken it since v90 — this route just never passed it,
    so a CLI/REST operator could not run one brief on a named engine at all.
    The field-test acceptance hit exactly that. The walls come with it: an
    unknown name is refused, and a CLI engine with no pinned verify_command
    still fails closed (policy_resolver.py:543)."""
    client = _client(config)

    unknown = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "x",
            "execution_mode": "workspace",  # resolved before the engine is
            "engine": "warp-drive",
        },
    )
    assert unknown.status_code in (400, 409)
    assert "warp-drive" in unknown.json()["detail"]

    unpinned = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "x",
            "execution_mode": "workspace",
            "engine": "claude_code",
        },
    )
    assert unpinned.status_code in (400, 409)
    assert "verify_command" in unpinned.json()["detail"]

    # The explicit default engine still dispatches, and v95-F3's rule holds:
    # naming an engine is an explicit run override, never an auto-dispatch (I6).
    accepted = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "workspace",
            "engine": "builtin",
        },
    )
    assert accepted.status_code == 202
    run = wait_terminal(client, accepted.json()["task_id"])
    assert run["state"] == "completed"


def test_shell_command_policy_rejects_git_push(config: SupervisorConfig) -> None:
    """v19-F3: git push can never be allowlisted."""
    client = _client(config)

    response = client.put("/api/policy", json={"allowed_shell_commands": [["git", "push"]]})

    assert response.status_code == 400
    assert "remote git commands cannot be allowlisted" in response.json()["detail"]


def test_shell_command_policy_rejects_catastrophic_commands(config: SupervisorConfig) -> None:
    """v109-F10: a machine-wrecker can never be allowlisted, and the refusal
    is the joke+teach line, not a bare no."""
    client = _client(config)

    response = client.put("/api/policy", json={"allowed_shell_commands": [["rm", "-rf", "/"]]})

    assert response.status_code == 400
    assert "does not fit in a worktree" in response.json()["detail"]


def test_policy_view_filters_poisoned_git_push_entry(config: SupervisorConfig) -> None:
    """v19-F3: a stored git push entry is filtered from policy_view output."""
    store = RunStore(config.db_path)
    try:
        # Simulate a store poisoned by an older allow-command grant.
        store.set_setting("allowed_shell_commands", [["git", "push"], ["git", "status"]])
        from skep.supervisor.serve.settings import policy_view

        holder = ConfigHolder(config, store)
        allowed = policy_view(store, holder.current)["allowed_shell_commands"]
        assert allowed == [["git", "status"]]
    finally:
        store.close()


# v100-F7: the exact shape found in ~/.skep on 2026-07-27 — a JSON array stored
# as a JSON *string*, which `list(...)` reads as its characters.
POISONED_NETWORK = '["ollama.com", "youtube.com", "www.youtube.com"]'


def test_set_policy_rejects_a_stringified_list(config: SupervisorConfig) -> None:
    """v100-F7: PUT /api/policy is typed, so pydantic catches this at the REST
    door — but the chat `set_policy` tool hands `update_policy` raw args, and the
    Queen still stringifies arrays (v95-F1). These two fields fell through to
    set_setting unvalidated; their neighbours did not."""
    store = RunStore(config.db_path)
    holder = ConfigHolder(config, store)
    runner = Dispatcher(holder, store)
    kwargs: dict[str, Any] = {
        "store": store,
        "holder": holder,
        "runner": runner,
        "actor": "chat-user",
    }
    try:
        for field in ("default_network", "default_env_allowlist"):
            with pytest.raises(HTTPException) as excinfo:
                execute_mutation("set_policy", {field: POISONED_NETWORK}, **kwargs)
            assert excinfo.value.status_code == 400
            assert excinfo.value.detail == f"{field} must be a list of strings"
            assert store.get_setting(field) is None  # nothing was persisted

        ok = execute_mutation("set_policy", {"default_network": ["ollama.com"]}, **kwargs)
        assert ok["default_network"] == ["ollama.com"]
    finally:
        store.close()


def test_policy_view_ignores_a_stringified_network_setting(config: SupervisorConfig) -> None:
    """A store already poisoned reads as deny-all, not as 47 one-character hosts.
    Fail closed: policy_view does not repair the value, it refuses it (I5)."""
    store = RunStore(config.db_path)
    try:
        store.set_setting("default_network", POISONED_NETWORK)
        store.set_setting("default_env_allowlist", POISONED_NETWORK)
        from skep.supervisor.serve.settings import policy_view

        view = policy_view(store, ConfigHolder(config, store).current)
        assert view["default_network"] == []
        assert view["default_env_allowlist"] == []
    finally:
        store.close()


def test_poisoned_network_setting_never_reaches_a_task_allowlist(
    repo: Path, config: SupervisorConfig
) -> None:
    """The assertion that would have caught this two days ago: the resolved
    allowlist in task.json describes what the proxy enforces (I8), so it carries
    real hosts and NO single-character entries."""
    store = RunStore(config.db_path)
    try:
        store.set_setting("default_network", POISONED_NETWORK)
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
    run = wait_terminal(client, task_id)

    task = json.loads((config.audit_dir / str(run["task_id"]) / "task.json").read_text())
    network = task["permissions"]["network"]
    assert [host for host in network if len(host) == 1] == []
    assert all("." in host for host in network)  # only the merged provider host survives


def test_startup_sweep_removes_poisoned_git_push_entry(config: SupervisorConfig) -> None:
    """v19-F3: the daemon startup sweep durably drops remote-git allowlist entries."""
    store = RunStore(config.db_path)
    try:
        store.set_setting("allowed_shell_commands", [["git", "push"], ["git", "status"]])
    finally:
        store.close()

    # Building the app runs the startup sweep.
    _client(config)

    store = RunStore(config.db_path)
    try:
        assert store.get_setting("allowed_shell_commands") == [["git", "status"]]
    finally:
        store.close()


def test_startup_sweep_removes_preseeded_catastrophic_entry(config: SupervisorConfig) -> None:
    """v109-F10: a store already holding `rm -rf /` stops granting it durably —
    swept at startup, not grandfathered (the v19-F3 sweep, wider)."""
    store = RunStore(config.db_path)
    try:
        store.set_setting("allowed_shell_commands", [["rm", "-rf", "/"], ["git", "status"]])
    finally:
        store.close()

    # Building the app runs the startup sweep.
    _client(config)

    store = RunStore(config.db_path)
    try:
        assert store.get_setting("allowed_shell_commands") == [["git", "status"]]
    finally:
        store.close()


def test_plugin_risk_policy_rejects_unknown_risk_names(config: SupervisorConfig) -> None:
    client = _client(config)

    response = client.put("/api/policy", json={"allowed_plugin_risks": ["wizard_mode"]})

    assert response.status_code == 400
    assert "allowed_plugin_risks must only contain" in response.json()["detail"]


def test_shell_command_policy_applies_only_to_trusted_workspace_runs(
    repo: Path, tmp_path: Path, config: SupervisorConfig
) -> None:
    client = _client(config)
    client.put(
        "/api/policy",
        json={
            "default_execution_mode": "workspace",
            "trusted_workspace_roots": [str(tmp_path)],
            "allowed_shell_commands": [["echo"]],
        },
    )

    run = _run_to_terminal(client, repo, "Fix the bug. MODE:happy")

    task_json = config.audit_dir / str(run["task_id"]) / "task.json"
    permissions = json.loads(task_json.read_text())["permissions"]
    assert permissions["shell_allowlist"] == [["echo"]]


def test_shell_command_policy_applies_to_sandbox_runs(repo: Path, config: SupervisorConfig) -> None:
    """Sandbox runs honor the allowlist too - the seatbelt bounds the blast radius."""
    client = _client(config)
    client.put("/api/policy", json={"allowed_shell_commands": [["echo"]]})

    task_id = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "sandbox",
        },
    ).json()["task_id"]
    run = wait_terminal(client, task_id)

    task_json = config.audit_dir / str(run["task_id"]) / "task.json"
    permissions = json.loads(task_json.read_text())["permissions"]
    assert permissions["shell_allowlist"] == [["echo"]]


def test_apply_policy_preset_unions_git_workflow_into_allowlist(
    config: SupervisorConfig,
) -> None:
    from fastapi import HTTPException
    from pytest import raises

    from skep.supervisor.serve.settings import GIT_PRESET_SHELL_COMMANDS
    from skep.supervisor.serve.tools import MUTATING_TOOL_SPECS

    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        store.set_setting("allowed_shell_commands", [["echo"]])

        allowed = actions.apply_shell_preset(store, holder, "git")
        assert allowed == [["echo"], *[list(entry) for entry in GIT_PRESET_SHELL_COMMANDS]]
        # Idempotent: re-applying does not duplicate entries.
        assert actions.apply_shell_preset(store, holder, "git") == allowed

        with raises(HTTPException) as excinfo:
            actions.apply_shell_preset(store, holder, "wizard")
        assert excinfo.value.status_code == 400
    finally:
        store.close()
    names = [spec["function"]["name"] for spec in MUTATING_TOOL_SPECS]
    assert "apply_policy_preset" in names


def test_allow_shell_command_unions_one_vetted_command(config: SupervisorConfig) -> None:
    """v49-F2: 'add pytest to the allowlist' works from chat — union-of-one
    through the same guard as the presets, never a replace."""
    from fastapi import HTTPException
    from pytest import raises

    from skep.supervisor.serve.tools import MUTATING_TOOL_SPECS

    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        store.set_setting("allowed_shell_commands", [["echo"]])

        allowed = actions.allow_shell_command(store, holder, "python3 -m pytest")
        assert allowed == [["echo"], ["python3", "-m", "pytest"]]
        # Idempotent, and never a replace: the existing entry survives.
        assert actions.allow_shell_command(store, holder, "python3 -m pytest") == allowed

        for dangerous in ("git push", "rm -rf /", "sudo make install"):
            with raises(HTTPException) as excinfo:
                actions.allow_shell_command(store, holder, dangerous)
            assert excinfo.value.status_code == 400
        with raises(HTTPException):
            actions.allow_shell_command(store, holder, "   ")
    finally:
        store.close()
    names = [spec["function"]["name"] for spec in MUTATING_TOOL_SPECS]
    assert "allow_shell_command" in names  # carded, never a free tool


def test_budget_policy_defaults_apply_to_runs_unless_explicit(
    repo: Path, config: SupervisorConfig
) -> None:
    client = _client(config)
    updated = client.put(
        "/api/policy",
        json={
            "default_execution_mode": "workspace",
            "default_wall_clock_seconds": 321,
            "default_max_iterations": 7,
            "default_max_actions": 11,
            "default_max_provider_calls": 13,
        },
    ).json()
    assert updated["default_wall_clock_seconds"] == 321
    assert updated["default_max_iterations"] == 7
    assert updated["default_max_actions"] == 11
    assert updated["default_max_provider_calls"] == 13

    rebooted = _client(config).get("/api/policy").json()
    assert rebooted["default_wall_clock_seconds"] == 321
    assert rebooted["default_max_iterations"] == 7
    assert rebooted["default_max_actions"] == 11
    assert rebooted["default_max_provider_calls"] == 13

    run = _run_to_terminal(client, repo, "Fix the bug. MODE:happy")
    default_task = json.loads((config.audit_dir / str(run["task_id"]) / "task.json").read_text())
    assert default_task["budget"] == {
        "wall_clock_seconds": 321,
        "max_iterations": 7,
        "max_actions": 11,
        "max_provider_calls": 13,
    }

    task_id = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "workspace",
            "wall_clock_seconds": 111,
            "max_iterations": 3,
            "max_actions": 5,
            "max_provider_calls": 2,
        },
    ).json()["task_id"]
    explicit = wait_terminal(client, task_id)
    explicit_task = json.loads(
        (config.audit_dir / str(explicit["task_id"]) / "task.json").read_text()
    )
    assert explicit_task["budget"] == {
        "wall_clock_seconds": 111,
        "max_iterations": 3,
        "max_actions": 5,
        "max_provider_calls": 2,
    }


def test_project_policy_overrides_global_defaults_for_bound_repo(
    repo: Path, config: SupervisorConfig
) -> None:
    seeded = RunStore(config.db_path)
    try:
        seeded.add_project_policy(
            project_id="project-1",
            name="trusted repo",
            strategy="trusted_local_dev",
            phase="build",
            policy={
                "default_execution_mode": "workspace",
                "default_network": ["*"],
                "allowed_shell_commands": [["pytest"]],
                "default_wall_clock_seconds": 321,
                "default_max_iterations": 7,
                "default_max_actions": 11,
                "default_max_provider_calls": 13,
            },
        )
        seeded.add_project_binding(
            project_id="project-1",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
    finally:
        seeded.close()

    client = _client(config)
    client.put(
        "/api/policy",
        json={
            "default_execution_mode": "ask",
            "default_network": ["global.example"],
            "trusted_workspace_roots": [],
            "allowed_shell_commands": [],
            "default_wall_clock_seconds": 900,
            "default_max_iterations": 16,
            "default_max_actions": 100,
            "default_max_provider_calls": 64,
        },
    )

    response = client.post(
        "/api/runs",
        json={"repo": str(repo), "instructions": "Fix the bug. MODE:happy"},
    )
    assert response.status_code == 202
    task_id = response.json()["task_id"]
    run = wait_terminal(client, task_id)

    assert run["state"] == "completed"
    detail = client.get(f"/api/runs/{run['task_id']}").json()["run"]
    assert detail["execution_mode"] == "workspace"
    task_json = json.loads((config.audit_dir / str(run["task_id"]) / "task.json").read_text())
    assert task_json["permissions"]["network"] == ["*"]
    assert task_json["permissions"]["shell_allowlist"] == [["pytest"]]
    assert task_json["budget"] == {
        "wall_clock_seconds": 321,
        "max_iterations": 7,
        "max_actions": 11,
        "max_provider_calls": 13,
    }


def test_project_policy_can_bind_registered_repo_slug(repo: Path, config: SupervisorConfig) -> None:
    client = _client(config)
    created_repo = client.post("/api/repos", json={"url": str(repo), "name": "fixture"})
    assert created_repo.status_code == 201

    created = client.post(
        "/api/projects",
        json={
            "project_id": "project-1",
            "name": "trusted repo",
            "strategy": "trusted_local_dev",
            "phase": "build",
            "policy": {
                "default_execution_mode": "workspace",
                "default_network": ["*"],
                "allowed_shell_commands": [["pytest"]],
                "default_wall_clock_seconds": 321,
                "default_max_iterations": 7,
                "default_max_actions": 11,
                "default_max_provider_calls": 13,
            },
            "bindings": [{"kind": "repo_slug", "value": "fixture"}],
        },
    )
    assert created.status_code == 201

    client.put(
        "/api/policy",
        json={
            "default_execution_mode": "ask",
            "default_network": ["global.example"],
            "trusted_workspace_roots": [],
            "allowed_shell_commands": [],
            "default_wall_clock_seconds": 900,
            "default_max_iterations": 16,
            "default_max_actions": 100,
            "default_max_provider_calls": 64,
        },
    )

    response = client.post(
        "/api/runs",
        json={"repo": "fixture", "instructions": "Fix the bug. MODE:happy"},
    )
    assert response.status_code == 202
    task_id = response.json()["task_id"]
    run = wait_terminal(client, task_id)

    assert run["state"] == "completed"
    detail = client.get(f"/api/runs/{run['task_id']}").json()["run"]
    assert detail["execution_mode"] == "workspace"


def test_v9_golden_project_policy_roundtrips_through_api(
    repo: Path, config: SupervisorConfig
) -> None:
    client = _client(config)
    payload = _load_v9_golden_policy(repo)

    created = client.post("/api/projects", json=payload)

    assert created.status_code == 201
    assert created.json() == payload
    detail = client.get(f"/api/projects/{payload['project_id']}")
    assert detail.status_code == 200
    assert detail.json() == payload


def test_project_policy_carries_allowed_plugin_risks_into_task(
    repo: Path, config: SupervisorConfig
) -> None:
    seeded = RunStore(config.db_path)
    try:
        seeded.add_project_policy(
            project_id="project-plugins",
            name="trusted repo",
            strategy="trusted_local_dev",
            phase="build",
            policy={
                "default_execution_mode": "workspace",
                "allowed_plugin_risks": ["write"],
            },
        )
        seeded.add_project_binding(
            project_id="project-plugins",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
    finally:
        seeded.close()

    client = _client(config)
    response = client.post(
        "/api/runs",
        json={"repo": str(repo), "instructions": "Fix the bug. MODE:happy"},
    )
    assert response.status_code == 202
    task_id = response.json()["task_id"]
    run = wait_terminal(client, task_id)
    assert run["state"] == "completed"

    task_json = json.loads((config.audit_dir / str(run["task_id"]) / "task.json").read_text())
    assert task_json["permissions"]["allowed_plugin_risks"] == ["write"]


def test_project_policy_carries_allow_git_mutation_into_task(
    repo: Path, config: SupervisorConfig
) -> None:
    seeded = RunStore(config.db_path)
    try:
        seeded.add_project_policy(
            project_id="project-git",
            name="trusted repo",
            strategy="trusted_local_dev",
            phase="build",
            policy={
                "default_execution_mode": "workspace",
                "allow_git_mutation": True,
            },
        )
        seeded.add_project_binding(
            project_id="project-git",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
    finally:
        seeded.close()

    client = _client(config)
    response = client.post(
        "/api/runs",
        json={"repo": str(repo), "instructions": "Fix the bug. MODE:happy"},
    )
    assert response.status_code == 202
    task_id = response.json()["task_id"]
    run = wait_terminal(client, task_id)
    assert run["state"] == "completed"

    task_json = json.loads((config.audit_dir / str(run["task_id"]) / "task.json").read_text())
    assert task_json["permissions"]["allow_git_mutation"] is True


def test_project_policy_can_auto_apply_verified_patch_to_branch(
    repo: Path, config: SupervisorConfig
) -> None:
    seeded = RunStore(config.db_path)
    try:
        seeded.add_project_policy(
            project_id="project-auto-apply",
            name="trusted repo",
            strategy="trusted_local_dev",
            phase="maintain",
            policy={
                "default_execution_mode": "workspace",
                "auto_apply_verified_patch": True,
                # v90-F4: the auto-landing lane only fires on a project-pinned
                # verify command — a worker-nominated one no longer satisfies it.
                "verify_command": 'grep -q "value = 1" existing.py',
            },
        )
        seeded.add_project_binding(
            project_id="project-auto-apply",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
    finally:
        seeded.close()

    client = _client(config)
    run = _run_to_terminal(client, repo, "Fix the bug. MODE:happy")

    assert run["state"] == "completed"
    # v30: a maintain-phase project auto-applies onto the integration branch
    # skep/maintain (the phase default), not a fresh per-task branch.
    assert _wait_integration_branch(repo), "project policy did not auto-apply the patch"
    assert _wait_applied_branch(client, run["task_id"]) == "skep/maintain"
    task_json = json.loads((config.audit_dir / str(run["task_id"]) / "task.json").read_text())
    assert task_json["auto_apply_verified_patch"] is True


def test_maintain_does_not_auto_apply_without_a_pinned_verify_command(
    repo: Path, config: SupervisorConfig
) -> None:
    """v90-F4: the case that motivated the rule.

    An unpinned project re-verifies with whatever the worker nominated, so
    `confirmed` means only "the worker's own command exited 0". The run is not
    blocked — it completes and the patch waits for a human — but the lane that
    lands without a human does not fire.
    """
    seeded = RunStore(config.db_path)
    try:
        seeded.add_project_policy(
            project_id="unpinned-maintain",
            name="trusted repo",
            strategy="trusted_local_dev",
            phase="maintain",
            policy={"default_execution_mode": "workspace"},  # no verify_command
        )
        seeded.add_project_binding(
            project_id="unpinned-maintain",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
    finally:
        seeded.close()

    client = _client(config)
    run = _run_to_terminal(client, repo, "Fix the bug. MODE:happy")

    # The work still happened and is still landable — by hand.
    assert run["state"] == "completed"
    assert not _wait_integration_branch(repo), "auto-landed without a pinned verify_command"
    assert _wait_applied_branch(client, run["task_id"]) is None


def test_project_phase_maintain_auto_applies_verified_patch_by_default(
    repo: Path, config: SupervisorConfig
) -> None:
    seeded = RunStore(config.db_path)
    try:
        seeded.add_project_policy(
            project_id="project-phase-maintain",
            name="trusted repo",
            strategy="trusted_local_dev",
            phase="maintain",
            # v90-F4: maintain auto-lands only with a pinned verify command.
            policy={
                "default_execution_mode": "workspace",
                "verify_command": 'grep -q "value = 1" existing.py',
            },
        )
        seeded.add_project_binding(
            project_id="project-phase-maintain",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
    finally:
        seeded.close()

    client = _client(config)
    run = _run_to_terminal(client, repo, "Fix the bug. MODE:happy")

    assert run["state"] == "completed"
    # v30: maintain phase accumulates auto-applied patches on skep/maintain.
    assert _wait_integration_branch(repo), "maintain phase did not auto-apply by default"
    assert _wait_applied_branch(client, run["task_id"]) == "skep/maintain"
    task_json = json.loads((config.audit_dir / str(run["task_id"]) / "task.json").read_text())
    assert task_json["auto_apply_verified_patch"] is True
    assert task_json["project_context"] == {
        "project_id": "project-phase-maintain",
        "name": "trusted repo",
        "strategy": "trusted_local_dev",
        "phase": "maintain",
        "binding_kind": "repo_path",
        "binding_value": str(repo),
    }
    dispatch_decision = _project_dispatch_decision(
        reason="dispatch.allow.run_request_resolved",
        project_id="project-phase-maintain",
        phase="maintain",
    )
    assert task_json["dispatch_decision"] == dispatch_decision
    assert task_json["landing_decision"] == {
        "verdict": "allow",
        "reason": "landing.auto_apply.project_policy_enabled",
        "detail": None,
        "decided_by": None,  # v40-F8 additive field
    }
    detail = client.get(f"/api/runs/{run['task_id']}").json()
    assert detail["transitions"][0]["state"] == "created"
    assert detail["project_context"] == {
        "project_id": "project-phase-maintain",
        "name": "trusted repo",
        "strategy": "trusted_local_dev",
        "phase": "maintain",
        "binding_kind": "repo_path",
        "binding_value": str(repo),
    }
    assert detail["dispatch_decision"] == dispatch_decision
    assert detail["landing_decision"] == {
        "verdict": "allow",
        "reason": "landing.auto_apply.project_policy_enabled",
        "detail": None,
        "decided_by": None,  # v40-F8 additive field
    }
    assert detail["transitions"][0]["detail"] == {
        "project_context": {
            "project_id": "project-phase-maintain",
            "name": "trusted repo",
            "strategy": "trusted_local_dev",
            "phase": "maintain",
            "binding_kind": "repo_path",
            "binding_value": str(repo),
        },
        "dispatch_decision": dispatch_decision,
        "landing_decision": {
            "verdict": "allow",
            "reason": "landing.auto_apply.project_policy_enabled",
            "detail": None,
            "decided_by": None,  # v40-F8 additive field
        },
    }
    events = client.get(f"/api/runs/{run['task_id']}/events").json()["events"]
    assert events[0]["type"] == "run.created"
    assert events[0]["payload"]["project_context"] == {
        "project_id": "project-phase-maintain",
        "name": "trusted repo",
        "strategy": "trusted_local_dev",
        "phase": "maintain",
        "binding_kind": "repo_path",
        "binding_value": str(repo),
    }
    assert events[0]["payload"]["dispatch_decision"] == dispatch_decision
    assert events[0]["payload"]["landing_decision"] == {
        "verdict": "allow",
        "reason": "landing.auto_apply.project_policy_enabled",
        "detail": None,
        "decided_by": None,  # v40-F8 additive field
    }
    assert events[1]["type"] == "task.start"
    assert events[1]["payload"]["project_context"] == events[0]["payload"]["project_context"]
    assert events[1]["payload"]["dispatch_decision"] == events[0]["payload"]["dispatch_decision"]
    assert events[1]["payload"]["landing_decision"] == events[0]["payload"]["landing_decision"]
    assert any(
        event["type"] == "approval.requested"
        and event["payload"]["action"] == "apply_patch"
        and event["payload"]["reason"].startswith("rule 'verified-patch' fired:")
        and event["payload"]["decision"]
        == {
            "verdict": "allow",
            "reason": "landing.auto_apply.project_policy_enabled",
            "detail": None,
            "decided_by": None,  # v40-F8 additive field
        }
        for event in events
    )
    assert any(
        event["type"] == "approval.resolved"
        and event["payload"]["action"] == "apply_patch"
        and event["payload"]["status"] == "approved"
        and event["payload"]["actor"] == "auto:verified-patch"
        and event["payload"]["branch"] == "skep/maintain"
        and event["payload"]["decision"]
        == {
            "verdict": "allow",
            "reason": "landing.auto_apply.project_policy_enabled",
            "detail": None,
            "decided_by": None,  # v40-F8 additive field
        }
        for event in events
    )


def test_maintain_phase_lands_on_one_integration_branch_and_freezes_main(
    repo: Path, config: SupervisorConfig
) -> None:
    """v30 (the v24-deferred decision, resolved): maintain-phase auto-apply
    targets ONE skep/maintain branch, main never advances, and a second run
    whose patch conflicts on append escalates to a human instead of corrupting
    the branch (the append mechanism itself is covered by the v24-F1 test)."""
    seeded = RunStore(config.db_path)
    try:
        seeded.add_project_policy(
            project_id="acc",
            name="trusted repo",
            strategy="trusted_local_dev",
            phase="maintain",
            # v90-F4: maintain auto-lands only with a pinned verify command.
            policy={
                "default_execution_mode": "workspace",
                "verify_command": 'grep -q "value = 1" existing.py',
            },
        )
        seeded.add_project_binding(
            project_id="acc", binding_kind="repo_path", binding_value=str(repo)
        )
    finally:
        seeded.close()

    default_head = git(repo, "rev-parse", "HEAD").stdout.strip()
    client = _client(config)

    run1 = _run_to_terminal(client, repo, "Fix the bug. MODE:happy")
    assert run1["state"] == "completed"
    assert _wait_applied_branch(client, run1["task_id"]) == "skep/maintain"
    tip_after_one = git(repo, "rev-parse", "skep/maintain").stdout.strip()

    # A second run's identical patch cannot append onto the diverged branch;
    # v81-F2: the failed auto-apply is DENIED with the failure on record —
    # approving again would fail identically, so a pending row would lie (I8).
    run2 = _run_to_terminal(client, repo, "Fix the bug. MODE:happy")
    assert run2["state"] == "completed"
    assert _wait_applied_branch(client, run2["task_id"]) is None
    pending = client.get("/api/approvals").json()["approvals"]
    assert not any(a["run"]["task_id"] == run2["task_id"] for a in pending)
    store = RunStore(config.db_path)
    try:
        failed = [a for a in store.approvals_for(str(run2["task_id"]))]
    finally:
        store.close()
    assert failed and failed[0].status == "denied"
    assert (failed[0].resolution_note or "").startswith("apply failed:")

    # The integration branch was not corrupted, there are no per-task branches,
    # and main never advanced.
    assert git(repo, "rev-parse", "skep/maintain").stdout.strip() == tip_after_one
    assert git(repo, "branch", "--list", "skep/*").stdout.split() == ["skep/maintain"]
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == default_head


def test_project_policy_can_disable_global_auto_apply_for_one_repo(
    repo: Path, config: SupervisorConfig
) -> None:
    seeded = RunStore(config.db_path)
    try:
        seeded.add_project_policy(
            project_id="project-manual-landing",
            name="trusted repo",
            strategy="trusted_local_dev",
            phase="maintain",
            policy={
                "default_execution_mode": "workspace",
                "auto_apply_verified_patch": False,
            },
        )
        seeded.add_project_binding(
            project_id="project-manual-landing",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
    finally:
        seeded.close()

    client = _client(config)
    client.put("/api/policy", json={"auto_approve": True})
    run = _run_to_terminal(client, repo, "Fix the bug. MODE:happy")

    assert run["state"] == "completed"
    assert _wait_applied_branch(client, run["task_id"]) is None
    assert not _branch_exists(repo, run["task_id"])
    task_json = json.loads((config.audit_dir / str(run["task_id"]) / "task.json").read_text())
    assert task_json["auto_apply_verified_patch"] is False


def test_dispatch_run_decision_explains_trusted_auto_dispatch(
    repo: Path, config: SupervisorConfig
) -> None:
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        store.add_project_policy(
            project_id="trusted-fixture",
            name="Trusted Fixture",
            strategy="trusted_local_dev",
            phase="build",
            policy={
                "default_execution_mode": "workspace",
                "auto_dispatch_allowed": True,
            },
        )
        store.add_project_binding(
            project_id="trusted-fixture",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
        decision = actions.dispatch_run_decision(
            holder=holder,
            store=store,
            repo=str(repo),
            execution_mode="workspace",
        )
    finally:
        store.close()

    assert decision.verdict == "allow"
    assert decision.reason == "dispatch.auto_allowed.project_policy_match"
    assert decision.detail is None


def test_dispatch_run_decision_inherits_project_default_execution_mode(
    repo: Path, config: SupervisorConfig
) -> None:
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        store.add_project_policy(
            project_id="trusted-fixture",
            name="Trusted Fixture",
            strategy="trusted_local_dev",
            phase="build",
            policy={
                "default_execution_mode": "workspace",
                "auto_dispatch_allowed": True,
            },
        )
        store.add_project_binding(
            project_id="trusted-fixture",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
        decision = actions.dispatch_run_decision(
            holder=holder,
            store=store,
            repo=str(repo),
        )
    finally:
        store.close()

    assert decision.verdict == "allow"
    assert decision.reason == "dispatch.auto_allowed.project_policy_match"
    assert decision.detail is None


def test_dispatch_run_decision_explains_when_explicit_overrides_require_confirmation(
    repo: Path, config: SupervisorConfig
) -> None:
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        store.add_project_policy(
            project_id="trusted-fixture",
            name="Trusted Fixture",
            strategy="trusted_local_dev",
            phase="build",
            policy={
                "default_execution_mode": "workspace",
                "auto_dispatch_allowed": True,
            },
        )
        store.add_project_binding(
            project_id="trusted-fixture",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
        decision = actions.dispatch_run_decision(
            holder=holder,
            store=store,
            repo=str(repo),
            execution_mode="workspace",
            network=["example.com"],
        )
    finally:
        store.close()

    assert decision.verdict == "require_approval"
    assert decision.reason == "dispatch.require_approval.explicit_run_overrides"
    assert decision.detail is None


def test_an_explicit_engine_choice_always_cards(repo: Path, config: SupervisorConfig) -> None:
    """v95-F3 (I6/I7): naming an engine deviates from the project default, so
    even a project whose policy auto-allows dispatch gets a confirmation card."""
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        store.add_project_policy(
            project_id="trusted-fixture",
            name="Trusted Fixture",
            strategy="trusted_local_dev",
            phase="build",
            policy={
                "default_execution_mode": "workspace",
                "auto_dispatch_allowed": True,
            },
        )
        store.add_project_binding(
            project_id="trusted-fixture",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
        decision = actions.dispatch_run_decision(
            holder=holder,
            store=store,
            repo=str(repo),
            engine="builtin",
        )
    finally:
        store.close()

    assert decision.verdict == "require_approval"
    assert decision.reason == "dispatch.require_approval.explicit_run_overrides"


def test_chat_auto_dispatch_decision_survives_into_task_json(
    repo: Path, config: SupervisorConfig
) -> None:
    store = RunStore(config.db_path)
    holder = ConfigHolder(config, store)
    runner = Dispatcher(holder, store)
    try:
        store.add_project_policy(
            project_id="trusted-fixture",
            name="Trusted Fixture",
            strategy="trusted_local_dev",
            phase="maintain",
            policy={
                "default_execution_mode": "workspace",
                "auto_dispatch_allowed": True,
            },
        )
        store.add_project_binding(
            project_id="trusted-fixture",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
        result = execute_mutation(
            "dispatch_run",
            {
                "repo": str(repo),
                "instructions": "Fix the bug. MODE:happy",
            },
            store=store,
            holder=holder,
            runner=runner,
            actor="chat-user",
            decision=AutonomyDecision(
                verdict="allow",
                reason="dispatch.auto_allowed.project_policy_match",
            ),
        )
        task_id = str(result["task_id"])
        run = wait_terminal(_client(config), task_id)
        assert run["state"] == "completed"

        task_json = json.loads((config.audit_dir / task_id / "task.json").read_text())
        assert task_json["dispatch_decision"] == _project_dispatch_decision(
            reason="dispatch.auto_allowed.project_policy_match",
            project_id="trusted-fixture",
            phase="maintain",
        )
    finally:
        runner.shutdown()
        store.close()


def test_direct_run_records_project_policy_dispatch_reason_when_auto_dispatch_matches(
    repo: Path, config: SupervisorConfig
) -> None:
    seeded = RunStore(config.db_path)
    try:
        seeded.add_project_policy(
            project_id="project-direct-auto-dispatch",
            name="trusted repo",
            strategy="trusted_local_dev",
            phase="build",
            policy={
                "default_execution_mode": "workspace",
                "auto_dispatch_allowed": True,
            },
        )
        seeded.add_project_binding(
            project_id="project-direct-auto-dispatch",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
    finally:
        seeded.close()

    client = _client(config)
    response = client.post(
        "/api/runs",
        json={"repo": str(repo), "instructions": "Fix the bug. MODE:happy"},
    )
    assert response.status_code == 202
    task_id = response.json()["task_id"]
    run = wait_terminal(client, task_id)
    assert run["state"] == "completed"

    expected_decision = _project_dispatch_decision(
        reason="dispatch.auto_allowed.project_policy_match",
        project_id="project-direct-auto-dispatch",
        phase="build",
    )
    task_json = json.loads((config.audit_dir / task_id / "task.json").read_text())
    assert task_json["dispatch_decision"] == expected_decision
    detail = client.get(f"/api/runs/{task_id}").json()
    assert detail["project_context"] == {
        "project_id": "project-direct-auto-dispatch",
        "name": "trusted repo",
        "strategy": "trusted_local_dev",
        "phase": "build",
        "binding_kind": "repo_path",
        "binding_value": str(repo),
    }
    assert detail["transitions"][0]["detail"]["dispatch_decision"] == expected_decision


def test_project_policy_dispatch_decision_carries_project_evidence(
    repo: Path, config: SupervisorConfig
) -> None:
    seeded = RunStore(config.db_path)
    try:
        seeded.add_project_policy(
            project_id="project-dispatch-evidence",
            name="trusted repo",
            strategy="trusted_local_dev",
            phase="maintain",
            policy={
                "default_execution_mode": "workspace",
                "auto_dispatch_allowed": True,
            },
        )
        seeded.add_project_binding(
            project_id="project-dispatch-evidence",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
    finally:
        seeded.close()

    client = _client(config)
    response = client.post(
        "/api/runs",
        json={"repo": str(repo), "instructions": "Fix the bug. MODE:happy"},
    )
    assert response.status_code == 202
    task_id = response.json()["task_id"]
    run = wait_terminal(client, task_id)
    assert run["state"] == "completed"

    expected_decision = {
        "verdict": "allow",
        "reason": "dispatch.auto_allowed.project_policy_match",
        "detail": None,
        "decided_by": None,  # v40-F8 additive field
        "project_id": "project-dispatch-evidence",
        "strategy": "trusted_local_dev",
        "phase": "maintain",
        "policy_source": "project_policy",
        # v23-F5: trusted dev workspace runs resolve the registry hosts.
        "constraints": {
            "network_requested": None,
            "network_resolved": [
                "files.pythonhosted.org",
                "proxy.golang.org",
                "pypi.org",
                "registry.npmjs.org",
            ],
        },
    }
    task_json = json.loads((config.audit_dir / task_id / "task.json").read_text())
    assert task_json["dispatch_decision"] == expected_decision
    detail = client.get(f"/api/runs/{task_id}").json()
    assert detail["dispatch_decision"] == expected_decision
    assert detail["transitions"][0]["detail"]["dispatch_decision"] == expected_decision


def test_project_phase_build_keeps_verified_patch_manual_by_default_even_with_global_auto_approve(
    repo: Path, config: SupervisorConfig
) -> None:
    seeded = RunStore(config.db_path)
    try:
        seeded.add_project_policy(
            project_id="project-phase-build",
            name="trusted repo",
            strategy="trusted_local_dev",
            phase="build",
            policy={"default_execution_mode": "workspace"},
        )
        seeded.add_project_binding(
            project_id="project-phase-build",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
    finally:
        seeded.close()

    client = _client(config)
    client.put("/api/policy", json={"auto_approve": True})
    run = _run_to_terminal(client, repo, "Fix the bug. MODE:happy")

    assert run["state"] == "completed"
    assert _wait_applied_branch(client, run["task_id"]) is None
    assert not _branch_exists(repo, run["task_id"])
    task_json = json.loads((config.audit_dir / str(run["task_id"]) / "task.json").read_text())
    assert task_json["auto_apply_verified_patch"] is False
    expected_build_decision = _project_dispatch_decision(
        reason="dispatch.allow.run_request_resolved",
        project_id="project-phase-build",
        phase="build",
    )
    assert task_json["dispatch_decision"] == expected_build_decision
    assert task_json["landing_decision"] == {
        "verdict": "require_approval",
        "reason": "landing.require_approval.project_policy_disabled_auto_apply",
        "detail": None,
        "decided_by": None,  # v40-F8 additive field
    }


def test_ask_execution_policy_requires_explicit_mode(repo: Path, config: SupervisorConfig) -> None:
    client = _client(config)

    missing = client.post(
        "/api/runs", json={"repo": str(repo), "instructions": "Fix the bug. MODE:happy"}
    )
    assert missing.status_code == 409
    assert "execution_mode" in missing.json()["detail"]

    task_id = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "workspace",
        },
    ).json()["task_id"]
    detail = wait_terminal(client, task_id)

    assert detail["state"] == "completed"
    run = client.get(f"/api/runs/{task_id}").json()["run"]
    assert run["execution_mode"] == "workspace"
    assert Path(run["workspace"]).parent == repo.parent / ".skep" / "worktrees"


def test_workspace_execution_must_match_trusted_root(
    repo: Path, tmp_path: Path, config: SupervisorConfig
) -> None:
    client = _client(config)
    client.put("/api/policy", json={"trusted_workspace_roots": [str(tmp_path / "trusted")]})

    response = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "workspace",
        },
    )

    assert response.status_code == 409
    assert "trusted workspace root" in response.json()["detail"]


def test_global_auto_approve_is_inert(repo: Path, config: SupervisorConfig) -> None:
    """v81-F14 (ends v23-F6's deprecation): the global toggle installs no rule.
    The setting round-trips for display, the write warns, and a completed run
    still waits for a human — per-project maintain is the only auto-apply ramp."""
    client = _client(config)
    written = client.put("/api/policy", json={"auto_approve": True}).json()
    assert any("inert" in note for note in written["deprecations"])
    assert client.get("/api/policy").json()["auto_approve"] is True  # display only

    run = _run_to_terminal(client, repo, "Fix the bug. MODE:happy")

    assert run["state"] == "completed"
    assert _wait_applied_branch(client, run["task_id"], timeout=1.0) is None
    assert not _wait_branch(repo, run["task_id"], timeout=0.2)  # nothing landed on its own
    detail = client.get(f"/api/runs/{run['task_id']}").json()
    assert any(artifact["kind"] == "patch" for artifact in detail["artifacts"])


def test_worker_cmd_policy_edit_changes_the_next_run(
    repo: Path, tmp_path: Path, config: SupervisorConfig
) -> None:
    # Startup config points at a worker that dies instantly without a contract
    # stream — runs crash. The policy edit swaps in the fake worker at runtime.
    broken = SupervisorConfig(
        home=config.home,
        worker_command=(sys.executable, "-c", "raise SystemExit(9)"),
        grace_seconds=0.5,
        heartbeat_seconds=0.1,
        poll_seconds=0.01,
    )
    client = _client(broken)
    before = _run_to_terminal(client, repo, "Fix the bug. MODE:happy")
    assert before["state"] == "worker_crashed"

    client.put("/api/policy", json={"worker_cmd": FAKE_WORKER_CMD})

    after = _run_to_terminal(client, repo, "Fix the bug. MODE:happy")
    assert after["state"] == "completed"


def test_default_network_policy_applies_to_runs_without_explicit_network(
    repo: Path, config: SupervisorConfig
) -> None:
    client = _client(config)
    client.put("/api/policy", json={"default_network": ["example.com"]})

    run = _run_to_terminal(client, repo, "Fix the bug. MODE:happy")
    task_json = config.audit_dir / str(run["task_id"]) / "task.json"
    permissions = json.loads(task_json.read_text())["permissions"]
    assert permissions["network"] == ["example.com"]

    # An explicit empty list still means deny-all, overriding the default.
    task_id = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "workspace",
            "network": [],
        },
    ).json()["task_id"]
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if client.get(f"/api/runs/{task_id}").json()["run"]["state"] in TERMINAL_STATES:
            break
        time.sleep(0.05)
    explicit = json.loads((config.audit_dir / task_id / "task.json").read_text())["permissions"]
    assert explicit["network"] == []


def test_dispatch_adds_configured_llm_host_when_network_is_omitted(
    repo: Path, config: SupervisorConfig
) -> None:
    client = _client(config)
    server = FakeOpenAI(api_key="sk-fake").start()
    try:
        client.put(
            "/api/llm/config",
            json={
                "base_url": server.base_url,
                "default_model": "gpt-oss",
                "protocol": "openai-compat",
                "api_key": "sk-fake",
            },
        )

        run = _run_to_terminal(client, repo, "Fix the bug. MODE:happy")
    finally:
        server.stop()

    task_json = config.audit_dir / str(run["task_id"]) / "task.json"
    permissions = json.loads(task_json.read_text())["permissions"]
    assert permissions["network"] == ["127.0.0.1"]


def test_auto_approve_write_returns_deprecation_notice(
    repo: Path, config: SupervisorConfig
) -> None:
    """v23-F6: enabling global auto_approve still works but says it is
    deprecated and names the per-project replacement."""
    client = _client(config)
    enabled = client.put("/api/policy", json={"auto_approve": True}).json()
    assert enabled["auto_approve"] is True
    assert any("set-phase" in notice for notice in enabled["deprecations"])
    disabled = client.put("/api/policy", json={"auto_approve": False}).json()
    assert disabled["auto_approve"] is False
    assert "deprecations" not in disabled


def test_set_operator_policy_rule_takes_effect_and_guards_denied_space(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    """v52-F4: a confirmed rule governs the next Queen decision; an allow
    into composed deny space is rejected with the deny's rule id; the tool
    is carded (never free)."""
    from fastapi import HTTPException
    from pytest import raises

    from skep.supervisor.policy_resolver import resolve_operator_policy
    from skep.supervisor.policy_schema import POLICY_DOCUMENT_SETTINGS_KEY, PolicyDocument
    from skep.supervisor.serve.tools import MUTATING_TOOL_SPECS

    store = RunStore(config.db_path)
    try:
        result = actions.set_operator_policy_rule(
            store, scope="filesystem", action="read", pattern=f"{tmp_path}/data/*", verdict="allow"
        )
        assert result["rule"]["rule_id"] == f"op:filesystem:read:{tmp_path}/data/*"
        decision = resolve_operator_policy(store).decision(
            "filesystem", "read", f"{tmp_path}/data/report.csv"
        )
        assert decision.verdict == "allow"
        assert decision.decided_by.endswith(f"op:filesystem:read:{tmp_path}/data/*")

        # Idempotent: the same rule again does not duplicate.
        again = actions.set_operator_policy_rule(
            store, scope="filesystem", action="read", pattern=f"{tmp_path}/data/*", verdict="allow"
        )
        assert len(again["allow"]) == len(result["allow"])

        # The default net:search allowance survives the first stored edit.
        assert resolve_operator_policy(store).decision("network", "search", "ddgs").verdict == (
            "allow"
        )

        # An allow reaching into denied space is rejected with the deny's id.
        store.set_setting(
            POLICY_DOCUMENT_SETTINGS_KEY,
            PolicyDocument.model_validate(
                {
                    "scopes": [
                        {
                            "scope": "filesystem",
                            "deny": [
                                {"rule_id": "no-vault", "action": "read", "pattern": "/vault/*"}
                            ],
                        }
                    ]
                }
            ).model_dump_json(),
        )
        with raises(HTTPException) as excinfo:
            actions.set_operator_policy_rule(
                store, scope="filesystem", action="read", pattern="/vault/key", verdict="allow"
            )
        assert "no-vault" in str(excinfo.value.detail)

        # Unknown scope/action are clean errors.
        with raises(HTTPException):
            actions.set_operator_policy_rule(
                store, scope="shell", action="run", pattern="ls", verdict="allow"
            )
        with raises(HTTPException):
            actions.set_operator_policy_rule(
                store, scope="network", action="teleport", pattern="*", verdict="allow"
            )
    finally:
        store.close()
    names = [spec["function"]["name"] for spec in MUTATING_TOOL_SPECS]
    assert "set_operator_policy" in names  # carded, never a free tool


def test_workers_roster_lists_every_caste_and_engine(config: SupervisorConfig) -> None:
    """v101-F9: the roster is read from the registries, not a hand-kept list —
    the defect F1 fixed was five copies of the roster, already diverged. A
    caste or engine added to a registry appears here with no route change."""
    from skep.supervisor.castes import CASTES
    from skep.supervisor.engines import CODING_ENGINES

    roster = _client(config).get("/api/workers").json()

    assert [c["name"] for c in roster["castes"]] == sorted(CASTES)
    assert [e["name"] for e in roster["engines"]] == sorted(CODING_ENGINES)
    # The summary is the registry's string verbatim: the Settings roster and
    # the Queen's tool schema read the same one, so they cannot drift.
    for row in roster["castes"]:
        caste = CASTES[row["name"]]
        assert row["summary"] == caste.summary
        assert (row["lands"], row["needs_provider"], row["needs_network"]) == (
            caste.lands,
            caste.needs_provider,
            caste.needs_network,
        )

    coding = next(c for c in roster["castes"] if c["name"] == "coding")
    assert coding["command"] == ""  # defers to config.command_for, by design
    assert coding["present"] is True
    assert all(c["present"] for c in roster["castes"])  # they ship in the wheel


def test_an_absent_engine_names_the_binary_it_probed(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The v87-F6 lesson applied BEFORE dispatch: an operator burned three runs
    on a binary that was not on the host. Absence is probed and says what was
    looked for — never assumed, never silent (I8)."""
    monkeypatch.setattr("skep.supervisor.engines.shutil.which", lambda _: None)
    roster = _client(config).get("/api/workers").json()

    builtin = next(e for e in roster["engines"] if e["name"] == "builtin")
    assert builtin["present"] is True  # it is this interpreter
    assert builtin["external"] is False  # the only one the capability layer binds

    external = [e for e in roster["engines"] if e["external"]]
    assert external, "the CLI adapters are the point of the probe"
    for engine in external:
        assert engine["present"] is False
        assert engine["binary"] in engine["detail"]
        assert "not on PATH" in engine["detail"]


def test_an_assign_shaped_post_dispatches_a_non_default_caste(
    repo: Path, config: SupervisorConfig
) -> None:
    """v101-F10: the Assign form offered two castes, so five of seven were
    undispatchable from the UI. The form posts the caste it was given and the
    route routes it — the run records the caste that actually ran (F4), which
    is what makes this checkable rather than a claim about a <select>."""
    client = _client(config)
    accepted = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "Answer the question. MODE:happy",
            "caste": "audit",
            "execution_mode": "workspace",
        },
    )
    assert accepted.status_code == 202
    run = wait_terminal(client, accepted.json()["task_id"])
    assert run["worker_kind"] == "audit"
    # An audit run is not a coding run, so it inherits no engine (F4).
    assert run["coding_engine"] is None


def test_an_unknown_caste_is_refused_by_name_not_run_as_coding(
    repo: Path, config: SupervisorConfig
) -> None:
    """The v42 defect, pinned at the route: an unregistered caste fell through
    to the CODING worker and the run was rejected downstream with no useful
    reason. F1's resolver refuses and names the valid choices (I9) — a UI that
    can now offer every caste must not be able to invent a tenth."""
    refused = _client(config).post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "x",
            "caste": "archivist",
            "execution_mode": "workspace",
        },
    )
    # 400, not 500: before F10 this reached the contract validator deep in
    # dispatch and surfaced as an unhandled error with no usable detail.
    assert refused.status_code == 400
    detail = refused.json()["detail"]
    assert "archivist" in detail
    assert "researcher" in detail and "reviewer" in detail  # names what IS valid
