"""Unlocked mode on policy v3 (ADR 0060): the per-project zero-ask rung.

The key exists on every operator face, is project-only and never groupable
(I6), and refuses to save without its preconditions — a pinned
verify_command (I2), a usable sandbox (I12) and a baseline that binds
nothing (on a zero-state home the layer could answer nothing, I8).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from skep.cli import main
from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.projects import validate_policy_group, validate_project_policy
from skep.supervisor.sandbox import SandboxAvailability

from .conftest import serve_client


def _sandbox(usable: bool) -> Any:
    def probe(backend: str | None = None) -> SandboxAvailability:
        if usable:
            return SandboxAvailability(usable=True, backend="test")
        return SandboxAvailability(usable=False, reason="probe_failed", detail="forced off by test")

    return probe


def lock_baseline(home: Path, name: str = "builder") -> None:
    """An approved baseline, so the zero-state guard binds nothing."""
    from skep.supervisor import policy_baseline as pb
    from skep.supervisor.policy_templates import load_policy_template

    layer = load_policy_template(name).model_dump(mode="json")
    pb.write_lock(home, pb._load_layer(layer), approved_by="op")


@pytest.fixture
def unlockable(config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch) -> SupervisorConfig:
    import skep.supervisor.serve.registry as registry

    monkeypatch.setattr(registry, "sandbox_availability", _sandbox(True))
    config.home.mkdir(parents=True, exist_ok=True)
    lock_baseline(config.home)
    return config


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


def test_unlocked_is_on_the_trust_ramp_project_only() -> None:
    from skep.supervisor.policy_registry import TRUST_RAMP_KEYS, registry_lookup

    key = registry_lookup("unlocked")
    assert key is not None and key.layers == ("project",)
    assert "unlocked" in TRUST_RAMP_KEYS
    with pytest.raises(ValueError, match="cannot ride a policy group"):
        validate_policy_group("zero-ask", {"unlocked": True})


def test_setup_refuses_unlocked_without_a_pinned_verify_command(
    repo: Path, unlockable: SupervisorConfig
) -> None:
    refused = _setup(serve_client(unlockable), repo, {"unlocked": True, "verify_command": ""})
    assert refused.status_code == 400
    assert "verify_command" in refused.json()["detail"]


def test_setup_refuses_unlocked_without_a_usable_sandbox(
    repo: Path, unlockable: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import skep.supervisor.serve.registry as registry

    monkeypatch.setattr(registry, "sandbox_availability", _sandbox(False))
    refused = _setup(serve_client(unlockable), repo, {"unlocked": True, "verify_command": "true"})
    assert refused.status_code == 400
    assert "sandbox" in refused.json()["detail"]
    assert "forced off by test" in refused.json()["detail"]  # the evidence (I9)


def test_setup_refuses_unlocked_on_a_home_with_no_approved_baseline(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import skep.supervisor.serve.registry as registry

    monkeypatch.setattr(registry, "sandbox_availability", _sandbox(True))
    refused = _setup(serve_client(config), repo, {"unlocked": True, "verify_command": "true"})
    assert refused.status_code == 400
    assert "skep policy approve" in refused.json()["detail"]


def test_setup_saves_unlocked_and_turns_it_back_off(
    repo: Path, unlockable: SupervisorConfig
) -> None:
    client = serve_client(unlockable)
    saved = _setup(client, repo, {"unlocked": True, "verify_command": "true"})
    assert saved.status_code == 201, saved.text
    assert saved.json()["policy"]["unlocked"] is True
    preview = client.post(
        "/api/projects/preview",
        json={"project_id": "unlockable", "name": "Unlockable", "strategy": "trusted_local_dev",
              "repo_path": str(repo), "policy_overrides": {"unlocked": True}},
    )  # fmt: skip
    assert "unlocked" in preview.json()["dangerous_grant_warnings"]
    off = _setup(client, repo, {"unlocked": False})
    assert off.status_code == 201
    assert off.json()["policy"]["unlocked"] is False


def test_the_cli_switches_unlocked_on_and_off(
    repo: Path, unlockable: SupervisorConfig, tmp_path: Path
) -> None:
    from skep.supervisor.cli_cmds import build_config

    root = tmp_path / "cli-home"
    cli = build_config(root, None)
    cli.home.mkdir(parents=True)
    lock_baseline(cli.home)
    base = ["--home", str(root), "project", "setup", str(repo), "--strategy", "trusted_local_dev"]
    base += ["--project-id", "unlockable", "--verify-command", "true", "--no-seed-schedules"]
    assert main([*base, "--unlocked"]) == 0
    store = RunStore(cli.db_path)
    try:
        record = store.get_project_policy("unlockable")
        assert record is not None and record.policy["unlocked"] is True
        assert main([*base, "--no-unlocked"]) == 0
        record = store.get_project_policy("unlockable")
        assert record is not None and record.policy["unlocked"] is False
    finally:
        store.close()


def test_copy_project_policy_never_copies_unlocked(
    repo: Path, unlockable: SupervisorConfig, tmp_path: Path
) -> None:
    from skep.supervisor.serve.actions import copy_project_policy

    client = serve_client(unlockable)
    assert _setup(client, repo, {"unlocked": True, "verify_command": "true"}).status_code == 201
    other = tmp_path / "other"
    other.mkdir()
    (other / ".git").mkdir()
    made = client.post(
        "/api/projects/setup",
        json={"project_id": "other", "name": "Other", "strategy": "trusted_local_dev",
              "repo_path": str(other)},
    )  # fmt: skip
    assert made.status_code == 201, made.text
    store = RunStore(unlockable.db_path)
    try:
        copied = copy_project_policy(store, src="unlockable", dst="other")
        assert "unlocked" not in copied["copied_keys"]
        assert "unlocked" not in copied["project"]["policy"]
    finally:
        store.close()


def test_turning_unlocked_on_from_chat_is_a_high_card_and_off_is_not() -> None:
    from skep.supervisor.serve.cards import high_tier

    for args in ({"unlocked": True}, {"policy_overrides": {"unlocked": True}}):
        why = high_tier("setup_project", {"project_id": "p", **args})
        assert why is not None and "unlocked" in why, args
    assert high_tier("setup_project", {"project_id": "p", "unlocked": False}) is None


# --- ES-F143: what unlocked implies, composed by the resolver -----------------


def _bound(config: SupervisorConfig, repo: Path, policy: dict[str, Any]) -> RunStore:
    store = RunStore(config.db_path)
    store.add_project_policy(
        project_id="unlockable",
        name="Unlockable",
        strategy="trusted_local_dev",
        phase="build",
        policy=policy,
    )
    store.add_project_binding(
        project_id="unlockable", binding_kind="repo_path", binding_value=str(repo.resolve())
    )
    return store


UNLOCKED = {"unlocked": True, "verify_command": "true"}


@pytest.mark.parametrize("v2", [False, True], ids=["engine-v1", "engine-v2"])
def test_unlocked_implies_auto_dispatch_sandbox_and_verified_landing(
    repo: Path, config: SupervisorConfig, v2: bool
) -> None:
    from skep.supervisor.policy_resolver import run_policy_for_repo
    from skep.supervisor.policy_schema import SCOPES
    from skep.supervisor.serve.settings import POLICY_ENGINE_V2_SCOPES

    store = _bound(config, repo, UNLOCKED)
    store.set_setting(POLICY_ENGINE_V2_SCOPES, list(SCOPES) if v2 else [])
    try:
        policy = run_policy_for_repo(store, config, repo.resolve())
        implied = {"auto_dispatch_allowed": True, "default_execution_mode": "sandbox",
                   "auto_apply_verified_patch": True}  # fmt: skip
        for key, value in implied.items():
            assert policy[key] == value, key
            assert policy["_provenance"][key] == "unlocked", key
    finally:
        store.close()


def test_an_unlocked_run_is_sandboxed_whatever_the_request_says(
    repo: Path, config: SupervisorConfig
) -> None:
    from skep.supervisor.policy_resolver import resolve_run_policy

    store = _bound(config, repo, UNLOCKED)
    try:
        resolved = resolve_run_policy(
            store=store, config=config, repo=repo.resolve(), caste="coding", network=None,
            env_allowlist=None, wall_clock_seconds=None, max_iterations=None,
            max_actions=None, max_provider_calls=None, execution_mode="workspace",
        )  # fmt: skip
        assert resolved.execution_mode == "sandbox"
    finally:
        store.close()


def test_an_unlocked_dispatch_runs_in_turn_and_says_why(
    repo: Path, config: SupervisorConfig
) -> None:
    from skep.supervisor.serve.actions import dispatch_run_decision
    from skep.supervisor.serve.settings import ConfigHolder

    store = _bound(config, repo, UNLOCKED)
    try:
        holder = ConfigHolder(config, store)
        decision = dispatch_run_decision(holder, store, repo=str(repo))
        assert (decision.verdict, decision.reason) == ("allow", "dispatch.auto_allowed.unlocked")
        # an explicit run override still cards (v95-F3, I6/I7)
        explicit = dispatch_run_decision(holder, store, repo=str(repo), engine="builtin")
        assert explicit.verdict == "require_approval"
    finally:
        store.close()


def test_a_phase_change_drops_unlocked(repo: Path, config: SupervisorConfig) -> None:
    from skep.supervisor.serve.registry import set_project_phase

    store = _bound(config, repo, UNLOCKED)
    try:
        moved = set_project_phase(store, "unlockable", "maintain")
        assert "unlocked" not in moved["policy"]
        assert moved["policy"]["verify_command"] == "true"
    finally:
        store.close()
