"""unlocked mode (ADR 0052): the per-project zero-ask rung of the trust ramp.

F1: the policy key exists on every operator face, is ungroupable (I6), and
refuses to save without its two preconditions — a pinned verify_command (I2)
and a usable sandbox (I12).
F2: a worker escalation on an unlocked project answers itself through the
same resume_past_gate path a human click takes — recorded auto:unlocked,
granted once, never the session tier.
F3: unlocked runs are sandbox-forced (I12) — the resolver coerces the mode,
run_task refuses mismatches, and a REQUIRED sandbox that cannot be built is
a refusal, never a silent naked-host run.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.projects import validate_policy_group, validate_project_policy
from skep.supervisor.sandbox import SandboxAvailability, availability
from skep.supervisor.serve.settings import SESSION_ALLOWED_SHELL_COMMANDS

from .conftest import serve_client as _client
from .conftest import wait_terminal as _wait_terminal

_SANDBOX_GATE = pytest.mark.skipif(
    not availability().usable,
    reason="unlocked mode requires a real sandbox (its enable precondition)",
)


def _sandbox_probe(usable: bool) -> Any:
    def probe(backend: str | None = None) -> SandboxAvailability:
        if usable:
            return SandboxAvailability(usable=True, backend="bwrap")
        return SandboxAvailability(usable=False, reason="probe_failed", detail="forced off by test")

    return probe


def _setup(client: TestClient, repo: Path, overrides: dict[str, Any]) -> Any:
    return client.post(
        "/api/projects/setup",
        json={
            "project_id": "unlockable",
            "name": "Unlockable",
            "strategy": "trusted_local_dev",
            "repo_path": str(repo),
            "policy_overrides": overrides,
        },
    )


def test_unlocked_is_a_project_policy_bool() -> None:
    assert validate_project_policy({"unlocked": True})["unlocked"] is True
    assert validate_project_policy({"unlocked": False})["unlocked"] is False
    with pytest.raises(ValueError, match="unlocked must be a boolean"):
        validate_project_policy({"unlocked": "yes"})


def test_unlocked_cannot_ride_a_policy_group() -> None:
    # The trust ramp is climbed per project (I6); a group must never carry it.
    with pytest.raises(ValueError, match="cannot ride a policy group"):
        validate_policy_group("zero-ask", {"unlocked": True})


def test_unlocked_setup_refuses_without_a_pinned_verify_command(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import skep.supervisor.serve.registry as registry

    monkeypatch.setattr(registry, "sandbox_availability", _sandbox_probe(True))
    client = _client(config)

    refused = _setup(client, repo, {"unlocked": True})

    assert refused.status_code == 400
    assert "verify_command" in refused.json()["detail"]


def test_unlocked_setup_refuses_without_a_usable_sandbox(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import skep.supervisor.serve.registry as registry

    monkeypatch.setattr(registry, "sandbox_availability", _sandbox_probe(False))
    client = _client(config)

    refused = _setup(client, repo, {"unlocked": True, "verify_command": "pytest -q"})

    assert refused.status_code == 400
    detail = refused.json()["detail"]
    assert "sandbox" in detail
    assert "forced off by test" in detail  # the refusal names the probe's evidence (I9)


def test_unlocked_setup_saves_with_both_preconditions_and_is_a_named_warning(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import skep.supervisor.serve.registry as registry

    monkeypatch.setattr(registry, "sandbox_availability", _sandbox_probe(True))
    client = _client(config)

    saved = _setup(client, repo, {"unlocked": True, "verify_command": "pytest -q"})
    assert saved.status_code == 201
    assert saved.json()["policy"]["unlocked"] is True

    preview = client.post(
        "/api/projects/preview",
        json={
            "project_id": "unlockable",
            "name": "Unlockable",
            "strategy": "trusted_local_dev",
            "repo_path": str(repo),
            "policy_overrides": {"unlocked": True, "verify_command": "pytest -q"},
        },
    )
    assert "unlocked" in preview.json()["dangerous_grant_warnings"]

    # --no-unlocked / unlocked=false is a plain policy update back to asking.
    relocked = _setup(client, repo, {"unlocked": False})
    assert relocked.status_code == 201
    assert relocked.json()["policy"]["unlocked"] is False


# ---- F2: escalations answer themselves --------------------------------------


@_SANDBOX_GATE
def test_unlocked_gate_answers_itself_with_a_recorded_auto_verdict(
    repo: Path, config: SupervisorConfig
) -> None:
    """The full arc: a run suspends on an escalation, the gate resolves as
    auto:unlocked with no human click, the resumed run completes — and no
    session-tier grant was written (the auto path grants ONCE)."""
    client = _client(config)
    saved = _setup(
        client,
        repo,
        {"unlocked": True, "verify_command": f"{sys.executable} -c pass"},
    )
    assert saved.status_code == 201

    dispatched = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "Commit this. MODE:pending",
            "execution_mode": "workspace",
        },
    )
    assert dispatched.status_code == 202
    task_id = str(dispatched.json()["task_id"])

    # The gate appears and answers itself: pending_approval → superseded.
    store = RunStore(config.db_path)
    try:
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            run = store.get_run(task_id)
            if run is not None and run.state == "superseded":
                break
            time.sleep(0.05)
        else:
            last = store.get_run(task_id)
            state = None if last is None else last.state
            raise AssertionError(f"gate never auto-resumed; run stayed {state!r}")

        gates = store.approvals_for(task_id)
        assert len(gates) == 1
        assert gates[0].status == "approved"
        assert gates[0].resolved_by == "auto:unlocked"

        resumed_id = next(
            detail.removeprefix("resumed as ")
            for state, detail, _ts in store.transitions_for(task_id)
            if state == "superseded" and detail and detail.startswith("resumed as ")
        )
        assert _wait_terminal(client, resumed_id)["state"] == "completed"

        # Once means once: the auto verdict left no standing session grant.
        assert not store.get_setting(SESSION_ALLOWED_SHELL_COMMANDS)

        # F5: the finished patch lands through the v90-F4 verified-patch lane
        # with zero clicks — G10-confirmed against the PINNED verify command,
        # onto a skep/* branch, with the rule on the ledger (I1, I2, I13).
        # The landing row is written moments AFTER the completed transition
        # (re-verify → auto-apply on the same dispatch thread), so poll.
        deadline = time.monotonic() + 30.0
        landing = store.approvals_for(resumed_id)
        while time.monotonic() < deadline and not landing:
            time.sleep(0.05)
            landing = store.approvals_for(resumed_id)
        assert len(landing) == 1
        assert landing[0].action == "apply_patch"
        assert landing[0].status == "approved"
        assert landing[0].resolved_by == "auto:verified-patch"
        assert landing[0].landing_branch == f"skep/{resumed_id}"
    finally:
        store.close()

    # main never advances on its own (I1): the landing lives on skep/*.
    from .conftest import git

    assert git(repo, "branch", "--show-current").stdout.strip() == "main"
    branches = git(repo, "branch", "--list", f"skep/{resumed_id}").stdout
    assert f"skep/{resumed_id}" in branches


# ---- F3: the sandbox is forced, and its absence refuses ---------------------


def _bind_unlocked_project(config: SupervisorConfig, repo: Path) -> None:
    """Write an unlocked project straight into the store — the shape of a
    policy that was valid when saved, whatever the host looks like now."""
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="unlockable",
            name="Unlockable",
            strategy="trusted_local_dev",
            phase="build",
            policy={
                "unlocked": True,
                "verify_command": "pytest -q",
                "default_execution_mode": "workspace",
                "auto_dispatch_allowed": True,
            },
        )
        store.add_project_binding(
            project_id="unlockable", binding_kind="repo_path", binding_value=str(repo)
        )
    finally:
        store.close()


def test_unlocked_resolution_coerces_the_mode_to_sandbox(
    repo: Path, config: SupervisorConfig
) -> None:
    from skep.supervisor.policy_resolver import resolve_run_policy

    _bind_unlocked_project(config, repo)
    store = RunStore(config.db_path)
    try:
        resolved = resolve_run_policy(
            store=store,
            config=config,
            repo=repo,
            caste="coding",
            network=None,
            env_allowlist=None,
            wall_clock_seconds=None,
            max_iterations=None,
            max_actions=None,
            max_provider_calls=None,
            execution_mode="workspace",
        )
    finally:
        store.close()
    assert resolved.execution_mode == "sandbox"


def test_unlocked_run_task_refuses_a_non_sandbox_mode(repo: Path, tmp_path: Path) -> None:
    from skep.supervisor.cli_cmds import build_config

    config = build_config(tmp_path / "home", None)
    _bind_unlocked_project(config, repo)

    import skep.supervisor.dispatch as dispatch_module

    with pytest.raises(ValueError, match="unlocked"):
        dispatch_module.run_task(repo, "do a thing", config=config, execution_mode="workspace")


def test_unlocked_run_task_refuses_an_unusable_sandbox(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor.cli_cmds import build_config

    config = build_config(tmp_path / "home", None)
    _bind_unlocked_project(config, repo)

    import skep.supervisor.dispatch as dispatch_module

    monkeypatch.setattr(dispatch_module, "sandbox_availability", _sandbox_probe(False))
    with pytest.raises(ValueError, match="sandbox"):
        dispatch_module.run_task(repo, "do a thing", config=config, execution_mode="sandbox")


def test_spawner_refuses_a_required_sandbox_it_cannot_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sandbox_enabled=True means REQUIRED: no silent naked-host fallback."""
    from skep.supervisor import sandbox as supervisor_sandbox
    from skep.supervisor import spawner as supervisor_spawner
    from skep.supervisor.contracts_io import mint_task

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = mint_task(workspace=workspace, instructions="do a thing")
    config = SupervisorConfig(home=tmp_path / "home", worker_command=("fake-worker",))
    monkeypatch.setattr(supervisor_sandbox, "availability", _sandbox_probe(False))

    with pytest.raises(supervisor_sandbox.SandboxUnavailableError, match="forced off by test"):
        supervisor_spawner.spawn_worker(
            config,
            task,
            tmp_path / "task.json",
            tmp_path / "result.json",
            log_path=tmp_path / "worker.log",
            sandbox_enabled=True,
        )


# ---- F4: the dispatch family runs in-turn; everything else still cards ------


def test_unlocked_dispatch_family_auto_allows_but_overrides_and_policy_tools_card(
    repo: Path, config: SupervisorConfig
) -> None:
    from skep.supervisor.serve.actions import dispatch_run_decision
    from skep.supervisor.serve.settings import ConfigHolder
    from skep.supervisor.serve.tools import mutation_execution_decision

    _bind_unlocked_project(config, repo)
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)

        decision = dispatch_run_decision(holder, store, repo=str(repo))
        assert decision.verdict == "allow"
        assert decision.reason == "dispatch.auto_allowed.unlocked"

        # An explicit per-dispatch engine stays a carded choice (v95-F3, I6/I7).
        gated = dispatch_run_decision(holder, store, repo=str(repo), engine="claude_code")
        assert gated.verdict == "require_approval"

        # run_code rides the same decision (v51-F3).
        run_code = mutation_execution_decision(
            "run_code", {"repo": str(repo), "code": "print(1)"}, store=store, holder=holder
        )
        assert run_code is not None and run_code.verdict == "allow"

        # Self-widening and outbound verbs gain NO auto path from unlocked:
        # no decision means the card machinery asks, exactly as before.
        for tool, args in (
            ("setup_project", {"project_id": "unlockable", "name": "n", "strategy": "x"}),
            ("open_pr", {"repo": str(repo), "task_id": "t"}),
        ):
            assert mutation_execution_decision(tool, args, store=store, holder=holder) is None
    finally:
        store.close()


# ---- F6: the mode is visible everywhere it matters --------------------------


def test_doctor_names_unlocked_projects(repo: Path, tmp_path: Path) -> None:
    from skep.status import _unlocked_advisories
    from skep.supervisor.cli_cmds import build_config

    home = tmp_path / "home"
    config = build_config(home, None)
    assert _unlocked_advisories(home) == []  # store not created yet — quiet

    _bind_unlocked_project(config, repo)
    advisories = _unlocked_advisories(home)
    assert len(advisories) == 1
    assert "UNLOCKED" in advisories[0]
    assert "unlockable" in advisories[0]
    assert "--no-unlocked" in advisories[0]  # the way out rides the advisory (I9)


def test_effective_policy_view_says_unlocked_with_provenance(
    repo: Path, config: SupervisorConfig
) -> None:
    from skep.supervisor.serve.actions import effective_policy_view
    from skep.supervisor.serve.settings import ConfigHolder

    _bind_unlocked_project(config, repo)
    store = RunStore(config.db_path)
    try:
        view = effective_policy_view(ConfigHolder(config, store), store, repo=str(repo))
    finally:
        store.close()
    assert view["unlocked"] is True
    assert view["execution_mode"] == "sandbox"  # F3 coercion shows on the view
    # RSoP (v109-F9): the implied landing flag names the layer that decided it.
    assert view["policy_provenance"]["auto_apply_verified_patch"] == {
        "value": True,
        "decided_by": "unlocked",
    }
