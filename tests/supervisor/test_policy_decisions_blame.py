"""Phase 02 / 2.1 (I8, I13): a run's decision rows name the run, its engine
and its caste — ``task_id``, ``engine``, ``caste`` — so one task's trail reads
back from ``decisions.jsonl`` alone. Optional keys: a Queen row carries none,
and an old row without them still reads back."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from skep.cli import main
from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.policy_v3 import Request, queen_decide
from skep.supervisor.serve.settings import ConfigHolder

from .test_delegation import _dispatch
from .test_role_policy import _home
from .test_unlocked_mode import UNLOCKED, _bound, lock_baseline

BLAME = ("task_id", "engine", "caste")


def _rows(config: SupervisorConfig) -> list[dict[str, Any]]:
    log = config.audit_dir / "policy" / "decisions.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_a_gated_unlocked_runs_rows_carry_task_engine_and_caste(
    repo: Path, config: SupervisorConfig
) -> None:
    from skep.supervisor import mint_task
    from skep.supervisor.serve.actions import UNLOCKED_ACTOR, unlocked_gate_allowed

    config.home.mkdir(parents=True, exist_ok=True)
    lock_baseline(config.home, shell_rules=False)
    store = _bound(config, repo, UNLOCKED)
    try:
        task = mint_task(workspace=repo, instructions="gate", worker_kind="coding")
        store.create_run(
            task, repo=repo.resolve(), ref=None, execution_mode="sandbox", coding_engine="codex"
        )
        (config.audit_dir / task.task_id).mkdir(parents=True)
        (config.audit_dir / task.task_id / "task.json").write_text(task.model_dump_json())
        store.transition(task.task_id, "pending_approval", None)
        store.enqueue_approval(
            task.task_id, action="shell.run", reason="gate", commands=[["make", "lint"]]
        )
        assert unlocked_gate_allowed(store, ConfigHolder(config, store), task.task_id)
    finally:
        store.close()
    (row,) = [r for r in _rows(config) if r["actor"] == UNLOCKED_ACTOR]
    assert {k: row[k] for k in BLAME} == {
        "task_id": task.task_id,
        "engine": "codex",
        "caste": "coding",
    }


@pytest.fixture
def home(tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> SupervisorConfig:
    return _home(tmp_path, repo, capsys)


def test_a_dispatch_decision_row_carries_task_engine_and_caste(
    home: SupervisorConfig, repo: Path
) -> None:
    _dispatch(home, repo, ["queen@operator"])
    rows = [r for r in _rows(home) if r["actor"] == "dispatch"]
    assert rows  # the drop beyond the delegator
    assert {tuple(r[k] for k in BLAME) for r in rows} == {("task-1", "builtin", "coding")}
    # the hop row still comes last, after the stamped drops (R3)
    assert _rows(home)[-1]["source"] == "delegated"


def test_a_refused_dispatch_row_carries_engine_and_caste_but_no_task(
    home: SupervisorConfig, repo: Path
) -> None:
    with pytest.raises(HTTPException):
        _dispatch(home, repo, ["queen@assistant"])
    (row,) = [r for r in _rows(home) if r["actor"] == "dispatch"]
    assert (row["engine"], row["caste"]) == ("builtin", "coding")
    assert "task_id" not in row  # no run was ever minted (I8)


def test_a_queen_row_carries_no_run_blame(home: SupervisorConfig) -> None:
    store = RunStore(home.db_path)
    try:
        queen_decide(ConfigHolder(home, store), store, Request("shell", "run", "git status"))
    finally:
        store.close()
    (row,) = [r for r in _rows(home) if r["actor"] == "queen"]
    assert not set(BLAME) & set(row)


def test_an_old_shape_row_still_reads_back(
    home: SupervisorConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    old = {
        "at": "2026-11-01T00:00:00Z", "actor": "dispatch", "repo": "/r", "scope": "shell",
        "action": "run", "value": "uv sync", "source": "run", "verdict": "allow",
        "rule_id": "r", "decided_by": "baseline/r", "layer": "baseline",
    }  # fmt: skip
    log = home.audit_dir / "policy" / "decisions.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps(old) + "\n")
    assert main(["--home", str(home.home.parent), "policy", "decisions"]) == 0
    view = json.loads(capsys.readouterr().out)
    assert view["rows"] == [old] and view["summary"]["allow"] == 1
