"""v12 Step 2: the policy regression corpus.

Every fixture in ``tests/fixtures/policy_regression/*.json`` drives a *real*
decision engine and asserts its verdict. The corpus reuses the trust engine —
it never re-derives policy in the test — so a verdict that drifts (a reason code
renamed, an auto-dispatch grant loosened, a guard removed) fails the corpus
instead of silently changing behaviour.

Fixtures are discriminated by ``kind``:

- ``dispatch``  -> ``project_policy_dispatch_decision`` (autonomy.py)
- ``landing``   -> ``auto_apply_decision`` (dispatch.py)
- ``capability``-> ``CapabilityRegistry.decision_for`` (workers/capabilities.py)
- ``shell_guard`` -> the pure v19 shell guards (shell_prefixes.py)
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.autonomy import project_policy_dispatch_decision
from skep.supervisor.dispatch import auto_apply_decision
from skep.supervisor.policy import SAFE_DEPENDENCY_RULE, VERIFIED_PATCH_RULE, AutoApprovalRule
from skep.supervisor.policy_resolver import resolve_run_policy
from skep.supervisor.projects import first_party_project_policy
from skep.supervisor.shell_prefixes import (
    dangerous_prefix_reason,
    is_remote_git_command,
    normalize_remembered_command,
)
from skep.supervisor.store import RunStore
from skep.workers.capabilities import CapabilityRegistry

CORPUS_DIR = Path(__file__).parents[1] / "fixtures" / "policy_regression"

_RULES_BY_NAME: dict[str, AutoApprovalRule] = {
    VERIFIED_PATCH_RULE.name: VERIFIED_PATCH_RULE,
    SAFE_DEPENDENCY_RULE.name: SAFE_DEPENDENCY_RULE,
}


def _load_fixtures() -> list[dict[str, Any]]:
    fixtures = [json.loads(path.read_text("utf-8")) for path in sorted(CORPUS_DIR.glob("*.json"))]
    assert fixtures, "policy regression corpus must not be empty"
    return fixtures


def _fixture_id(fixture: dict[str, Any]) -> str:
    return str(fixture["name"])


def _effective_policy(fixture: dict[str, Any]) -> dict[str, Any]:
    """Build the effective run policy from strategy/phase and/or explicit overrides."""
    policy: dict[str, Any] = {}
    if "strategy" in fixture and "phase" in fixture:
        policy = first_party_project_policy(strategy=fixture["strategy"], phase=fixture["phase"])
    policy.update(fixture.get("policy", {}))
    return policy


def _check_dispatch(fixture: dict[str, Any]) -> None:
    decision = project_policy_dispatch_decision(
        policy=_effective_policy(fixture),
        requested_execution_mode=fixture.get("requested_execution_mode"),
        explicit_run_overrides=bool(fixture.get("explicit_run_overrides", False)),
    )
    expected = fixture["expected"]
    assert decision.verdict == expected["verdict"], fixture["name"]
    assert decision.reason == expected["reason"], fixture["name"]


def _check_landing(fixture: dict[str, Any]) -> None:
    policy = _effective_policy(fixture)
    raw = policy.get("auto_apply_verified_patch")
    auto_apply = raw if isinstance(raw, bool) else None
    rules = tuple(_RULES_BY_NAME[name] for name in fixture.get("config_rules", []))
    decision = auto_apply_decision(rules, auto_apply)
    expected = fixture["expected"]
    assert decision.verdict == expected["verdict"], fixture["name"]
    assert decision.reason == expected["reason"], fixture["name"]


def _check_capability(fixture: dict[str, Any], tmp_path: Path) -> None:
    grants = fixture.get("grants", {})
    registry = CapabilityRegistry(
        tmp_path,
        emit=lambda _type, _payload: None,
        shell_allowlist=[tuple(entry) for entry in grants.get("shell_allowlist", [])],
        allow_git_mutation=bool(grants.get("allow_git_mutation", False)),
        network_allowlist=tuple(grants.get("network_allowlist", ())),
        allowed_plugin_risks=tuple(grants.get("allowed_plugin_risks", ())),
        approved_capability_ids=tuple(grants.get("approved_capability_ids", ())),
        approved_network_hosts=tuple(grants.get("approved_network_hosts", ())),
        approved_plugin_risks=dict(grants.get("approved_plugin_risks", {})),
        plugin_tools=(),
    )
    decision = registry.decision_for(fixture["capability"], fixture["arguments"])
    payload = decision.to_payload()
    expected = fixture["expected"]
    assert payload["verdict"] == expected["verdict"], fixture["name"]
    assert payload["reason"] == expected["reason"], fixture["name"]
    if "detail" in expected:
        assert payload["detail"] == expected["detail"], fixture["name"]


def _check_shell_guard(fixture: dict[str, Any]) -> None:
    for case in fixture["cases"]:
        argv = list(case["argv"])
        # The real remember path normalizes first, then guards the result.
        normalized = normalize_remembered_command(argv)
        assert normalized == case["normalized"], case
        assert is_remote_git_command(argv) is case["is_remote_git"], case
        blocked = dangerous_prefix_reason(normalized) is not None
        assert blocked is case["dangerous_prefix"], case


def _check_network_merge(fixture: dict[str, Any], tmp_path: Path) -> None:
    """v19-F2/F11: the configured provider host is merged into a coding run's
    network allowlist on every creation path (and only for the coding caste),
    driven by the real ``resolve_run_policy``."""
    config = SupervisorConfig(home=tmp_path / "home", worker_command=("false",))
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    store = RunStore(config.db_path)
    try:
        for case in fixture["cases"]:
            resolved = resolve_run_policy(
                store=store,
                config=config,
                repo=repo,
                caste=case["caste"],
                network=list(case["network"]),
                env_allowlist=None,
                wall_clock_seconds=None,
                max_iterations=None,
                max_actions=None,
                max_provider_calls=None,
                execution_mode=fixture.get("execution_mode", "sandbox"),
                extra_network_hosts=tuple(case.get("provider_hosts", ())),
            )
            assert resolved.permissions.network == case["network_out"], case
    finally:
        store.close()


_CHECKERS: dict[str, Callable[[dict[str, Any], Path], None]] = {
    "dispatch": lambda fx, _tmp: _check_dispatch(fx),
    "landing": lambda fx, _tmp: _check_landing(fx),
    "capability": lambda fx, tmp: _check_capability(fx, tmp),
    "shell_guard": lambda fx, _tmp: _check_shell_guard(fx),
    "network_merge": _check_network_merge,
}


@pytest.mark.parametrize("fixture", _load_fixtures(), ids=_fixture_id)
def test_policy_regression_corpus(fixture: dict[str, Any], tmp_path: Path) -> None:
    kind = fixture["kind"]
    checker = _CHECKERS.get(kind)
    assert checker is not None, f"unknown fixture kind {kind!r}"
    checker(fixture, tmp_path)
