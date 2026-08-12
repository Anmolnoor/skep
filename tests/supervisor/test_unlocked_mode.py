"""unlocked mode (ADR 0052): the per-project zero-ask rung of the trust ramp.

F1: the policy key exists on every operator face, is ungroupable (I6), and
refuses to save without its two preconditions — a pinned verify_command (I2)
and a usable sandbox (I12).
F2: a worker escalation on an unlocked project answers itself through the
same resume_past_gate path a human click takes — recorded auto:unlocked,
granted once, never the session tier.
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
    finally:
        store.close()
