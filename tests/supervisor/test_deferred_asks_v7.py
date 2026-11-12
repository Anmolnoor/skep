"""ADR 0059 D3 (V7): a grant the delegation chain makes an ask is a deferred
approval, not a drop. ``Permissions.network_ask`` / ``shell_ask`` carry it;
the worker cards it naming the chain; the approval holds for that run only;
a headless face drops it (deny); a deny never becomes an ask (B12 residue,
B22 card half, B147)."""

from __future__ import annotations

from pathlib import Path

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.autonomy import AutonomyDecision
from skep.supervisor.dispatch import run_task
from skep.worker_contract import CodingWorkerTask, Permissions


def _perms(**kw: object) -> Permissions:
    return Permissions.model_validate({"read": [], "write": [], "env_allowlist": [], **kw})


# --- the contract ---------------------------------------------------------


def test_asks_default_empty_and_an_old_task_still_loads(task_payload: dict[str, object]) -> None:
    """A task.json persisted before contract 0.3.7 has neither field."""
    task = CodingWorkerTask.model_validate(task_payload)
    assert task.permissions.network_ask == []
    assert task.permissions.shell_ask == []


@pytest.mark.parametrize("entry", ["*", "*.example.com", "https://example.com", "a/b", "", "x:1"])
def test_network_ask_is_bare_hosts_only(entry: str) -> None:
    """A wildcard is never askable (ADR 0059 D4: never offered `*`)."""
    with pytest.raises(ValueError, match="network_ask"):
        _perms(network_ask=[entry])


@pytest.mark.parametrize("entry", [[], [""]])
def test_shell_ask_entries_are_non_empty_argvs(entry: list[str]) -> None:
    with pytest.raises(ValueError, match="shell_ask"):
        _perms(shell_ask=[entry])


def test_asks_are_not_egress() -> None:
    """An ask opens nothing until a human approves it for that run."""
    perms = _perms(network=["pypi.org"], network_ask=["example.com"])
    assert perms.egress() == ["pypi.org"]


@pytest.fixture
def task_payload(tmp_path: Path) -> dict[str, object]:
    return {
        "contract_version": "0.3.6",
        "task_id": "t-old",
        "trace_id": "tr-old",
        "worker_kind": "coding",
        "workspace": str(tmp_path),
        "instructions": "x",
        "permissions": {
            "read": ["workspace"],
            "write": ["workspace"],
            "network": ["pypi.org"],
            "env_allowlist": [],
        },
        "budget": {
            "wall_clock_seconds": 60,
            "max_iterations": 1,
            "max_actions": 1,
            "max_provider_calls": 0,
        },
    }


@pytest.mark.parametrize("delegated", [False, True])
def test_run_task_keeps_asks_only_on_a_delegated_dispatch(
    config: SupervisorConfig, repo: Path, delegated: bool
) -> None:
    """The chain narrowing is the asks' one writer: an envelope that names
    them with no delegation (a hand-edited task, a stray caller) loses them."""
    decision = AutonomyDecision(
        verdict="allow",
        reason="dispatch.allow.test",
        delegation={"chain": ["queen@builder"], "role": None} if delegated else None,
    )
    outcome = run_task(
        repo,
        "x",
        config=config,
        permissions=_perms(
            read=["workspace"],
            write=["workspace"],
            network_ask=["example.com"],
            shell_ask=[["make"]],
        ),
        dispatch_decision=decision,
        execution_mode="workspace",
    )
    task = CodingWorkerTask.model_validate_json(
        (config.audit_dir / outcome.record.task_id / "task.json").read_text()
    )
    assert task.permissions.network_ask == (["example.com"] if delegated else [])
    assert task.permissions.shell_ask == ([["make"]] if delegated else [])
