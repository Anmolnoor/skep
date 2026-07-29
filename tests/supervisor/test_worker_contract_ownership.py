from __future__ import annotations

import tomllib
from pathlib import Path

import pytest


def test_worker_contract_is_owned_inside_skep() -> None:
    from skep.worker_contract import CONTRACT_VERSION, CodingWorkerTask, Permissions

    assert CONTRACT_VERSION == "0.3.5"
    assert CodingWorkerTask.__module__.startswith("skep.worker_contract")
    assert (
        Permissions.model_validate(
            {"read": [], "write": [], "network": False, "env_allowlist": []}
        ).network
        == []
    )


def test_worker_contract_rejects_unknown_plugin_risks() -> None:
    from skep.worker_contract import Permissions

    with pytest.raises(ValueError, match="allowed_plugin_risks must only contain"):
        Permissions.model_validate(
            {
                "read": [],
                "write": [],
                "network": [],
                "env_allowlist": [],
                "allowed_plugin_risks": ["wizard_mode"],
            }
        )


def test_agent_task_contract_is_not_a_runtime_dependency() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert "agent-task-contract" not in pyproject["project"]["dependencies"]
    assert "agent-task-contract" not in pyproject.get("tool", {}).get("uv", {}).get("sources", {})


def test_worker_docs_state_contract_version_policy() -> None:
    from skep.worker_contract import CONTRACT_VERSION

    docs = Path("docs/workers.md").read_text(encoding="utf-8")

    assert f"worker contract {CONTRACT_VERSION}" in docs
    assert "skep --version" in docs
    assert "Minor bumps are additive" in docs
    assert "Major bumps are breaking" in docs


def test_supported_contract_range_has_one_declaration() -> None:
    """v39-F3: the range literal lives ONLY in the contract package; the
    supervisor and every first-party worker import it. Six per-file copies
    were a skew bug waiting for a release."""
    import skep
    from skep.supervisor import SUPPORTED_CONTRACT_RANGE as supervisor_range
    from skep.worker_contract import SUPPORTED_CONTRACT_RANGE

    assert supervisor_range is SUPPORTED_CONTRACT_RANGE
    root = Path(skep.__file__).parent
    version_py = root / "worker_contract" / "version.py"
    holders = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if 'SUPPORTED_CONTRACT_RANGE = "' in path.read_text(encoding="utf-8")
    ]
    assert holders == [str(version_py.relative_to(root))]
