"""unlocked mode (ADR 0052): the per-project zero-ask rung of the trust ramp.

F1: the policy key exists on every operator face, is ungroupable (I6), and
refuses to save without its two preconditions — a pinned verify_command (I2)
and a usable sandbox (I12).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from skep.supervisor import SupervisorConfig
from skep.supervisor.projects import validate_policy_group, validate_project_policy
from skep.supervisor.sandbox import SandboxAvailability

from .conftest import serve_client as _client


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
