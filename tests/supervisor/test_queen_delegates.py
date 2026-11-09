"""first-run R3 (ADR 0055 D5): the Queen delegates. A worker dispatched with
an explicit role gets that role's layer AND its delegator's bound; every hop
is a ``delegated`` ledger row with its role; the run cards read the chain off
the dispatch decision. Temp homes, the builder baseline approved."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from skep.cli import main
from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.roles import EXCEEDS_DELEGATOR
from skep.supervisor.serve.actions import (
    created_transition_views_for_task,
    decision_detail_view,
    submit_run,
)
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import execute_mutation

from .conftest import FAKE_WORKER, serve_client
from .test_role_policy import _home

APP_POLICY = {
    "allowed_shell_commands": [["uv", "sync"], ["git", "status"]],
    "default_network": ["pypi.org"],
}


@pytest.fixture
def config(tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> SupervisorConfig:
    config = _home(tmp_path, repo, capsys)
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="app",
            name="app",
            strategy="trusted_local_dev",
            phase="build",
            policy=APP_POLICY,
        )
    finally:
        store.close()
    return config


class _Runner:
    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []

    def submit(self, repo: Path, instructions: str, **kwargs: Any) -> str:
        self.submitted.append({"instructions": instructions, **kwargs})
        return f"task-{len(self.submitted)}"

    def shutdown(self, **_: Any) -> None:
        pass


def _dispatch(config: SupervisorConfig, repo: Path, **kwargs: Any) -> dict[str, Any]:
    store = RunStore(config.db_path)
    runner = _Runner()
    try:
        submit_run(
            ConfigHolder(config, store),
            runner,  # type: ignore[arg-type]
            store,
            repo=str(repo),
            instructions="x",
            **kwargs,
        )
    finally:
        store.close()
    return runner.submitted[0]


def _worker_cmd() -> str:
    return shlex.join([sys.executable, str(FAKE_WORKER)])


def _ledger(config: SupervisorConfig) -> list[dict[str, Any]]:
    path = config.audit_dir / "policy" / "decisions.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_a_worker_role_is_held_to_its_delegator(config: SupervisorConfig, repo: Path) -> None:
    """Queen `operator` → worker `builder`: builder alone would keep `uv sync`,
    but the operator Queen only asks for it, so the worker loses it (and
    cards for it); `git status` both hold, so it stays. The decision carries
    the chain and the worker's role; the hop is one `delegated` row."""
    alone = _dispatch(config, repo, role="builder")
    assert ["uv", "sync"] in alone["permissions"].shell_allowlist
    sent = _dispatch(config, repo, role="builder", delegation_chain=["queen@operator"])
    allowlist = sent["permissions"].shell_allowlist
    assert ["uv", "sync"] not in allowlist and ["git", "status"] in allowlist
    decision = sent["dispatch_decision"]
    assert decision.delegation == {"chain": ["queen@operator"], "role": "builder", "team": None}
    assert "delegated by queen@operator · role builder; beyond the delegator, dropped: " in (
        decision.detail
    )
    *drops, hop = _ledger(config)[1:]  # [0] is the builder-alone dispatch's hop
    # The operator Queen asks for installs and test runners too: each drops,
    # each recorded under the worker's role (G10 still re-verifies, I2).
    assert {(d["rule_id"], d["role"]) for d in drops} == {(EXCEEDS_DELEGATOR, "builder")}
    assert {"uv sync", "uv run pytest"} <= {d["value"] for d in drops}
    assert "git status" not in {d["value"] for d in drops}
    assert {k: hop[k] for k in ("source", "actor", "value", "role", "delegation_chain")} == {
        "source": "delegated",
        "actor": "queen@operator",
        "value": "worker@builder",
        "role": "builder",
        "delegation_chain": ["queen@operator"],
    }
    # The payload the store keeps, and the view every card reads, carry it.
    view = decision_detail_view(decision.to_payload().model_dump(mode="json"))
    assert view is not None and view["delegation"]["chain"] == ["queen@operator"]


def test_the_role_alone_narrows_an_operator_dispatch(config: SupervisorConfig, repo: Path) -> None:
    """No chain, a role: the operator dispatching a worker `operator` gets
    the role's layer — `uv sync` drops under role:operator, recorded under it,
    and the hop row's actor is the operator."""
    sent = _dispatch(config, repo, role="operator")
    assert ["uv", "sync"] not in sent["permissions"].shell_allowlist
    assert sent["dispatch_decision"].delegation == {"chain": [], "role": "operator", "team": None}
    *drops, hop = _ledger(config)
    assert {d["decided_by"] for d in drops} == {"role:operator/shell-gated"}
    assert (hop["actor"], hop["value"]) == ("operator", "worker@operator")


def test_a_role_denied_writes_cannot_be_dispatched_as_a_writer(
    config: SupervisorConfig, repo: Path
) -> None:
    with pytest.raises(HTTPException) as refused:
        _dispatch(config, repo, role="assistant")
    assert refused.value.status_code == 403
    assert "role assistant: filesystem.write workspace exceeds the role" in refused.value.detail
    assert not [row for row in _ledger(config) if row.get("source") == "delegated"]


def test_no_role_no_chain_is_today(config: SupervisorConfig, repo: Path) -> None:
    sent = _dispatch(config, repo)
    assert sent["dispatch_decision"].delegation is None
    assert _ledger(config) == []


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"role": "warp"}, "no role 'warp'"),
        ({"delegation_chain": ["worker@builder"]}, "a worker never dispatches"),
        ({"delegation_chain": ["queen@operator", "worker@builder"]}, "a worker never dispatches"),
    ],
)
def test_bad_roles_and_worker_hops_refuse(
    config: SupervisorConfig, repo: Path, kwargs: dict[str, Any], message: str
) -> None:
    with pytest.raises(HTTPException) as refused:
        _dispatch(config, repo, **kwargs)
    assert refused.value.status_code == 400 and message in refused.value.detail


# --- R3-2: the faces (ADR 0050) — REST, `skep run`, the chat tools ----------


def test_rest_post_runs_takes_the_role_and_the_chain(config: SupervisorConfig, repo: Path) -> None:
    runner = _Runner()
    with serve_client(config, dispatcher=runner) as client:
        body = {"repo": str(repo), "instructions": "x", "role": "builder"}
        sent = client.post("/api/runs", json=body | {"delegation_chain": ["queen@operator"]})
        assert sent.status_code == 202, sent.text
        refused = client.post("/api/runs", json=body | {"delegation_chain": ["worker@builder"]})
        assert refused.status_code == 400 and "a worker never dispatches" in refused.text
    decision = runner.submitted[0]["dispatch_decision"]
    assert decision.delegation == {"chain": ["queen@operator"], "role": "builder", "team": None}
    assert _ledger(config)[-1]["task_id"] == "task-1"


def test_skep_run_takes_the_role_and_the_chain(
    config: SupervisorConfig, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`skep run --role --delegated-by`: the in-process dispatch narrows the
    same way, the hop row names the real task, and the run's dispatch
    decision carries the chain on the store's record."""
    run = ["--home", str(config.home.parent), "run", str(repo), "Fix. MODE:happy", "--quiet"]
    run += ["--execution-mode", "workspace", "--worker-cmd", _worker_cmd()]
    assert main([*run, "--role", "builder", "--delegated-by", "queen@operator"]) == 0
    capsys.readouterr()
    hop = _ledger(config)[-1]
    store = RunStore(config.db_path)
    try:
        (record,) = store.recent_runs(5)
        created = created_transition_views_for_task(store, record.task_id)
    finally:
        store.close()
    assert (hop["source"], hop["task_id"], hop["role"]) == ("delegated", record.task_id, "builder")
    assert created["dispatch_decision"]["delegation"]["chain"] == ["queen@operator"]
    assert main([*run, "--role", "warp"]) != 0
    assert "no role 'warp'" in capsys.readouterr().err


def test_the_queen_dispatches_a_worker_with_a_role(config: SupervisorConfig, repo: Path) -> None:
    """dispatch_run role=: the worker's role rides to submit_run beside the
    Queen's chain, and the result the chat card renders names both."""
    store = RunStore(config.db_path)
    runner = _Runner()
    try:
        chat_id = store.create_chat(title="r", model=None).chat_id
        store.set_chat_role(chat_id, "operator")
        result = execute_mutation(
            "dispatch_run",
            {"repo": str(repo), "instructions": "x", "role": "builder"},
            store=store,
            holder=ConfigHolder(config, store),
            runner=runner,  # type: ignore[arg-type]
            actor="chat-user",
            chat_id=chat_id,
        )
    finally:
        store.close()
    assert result["delegation"] == {"chain": ["queen@operator"], "role": "builder"}
    assert runner.submitted[0]["dispatch_decision"].delegation["role"] == "builder"
