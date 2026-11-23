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


# --- 2C: the two callers 2A left — `skep run --role/--delegated-by`, schedules


def _cli_run(
    home: SupervisorConfig, repo: Path, monkeypatch: pytest.MonkeyPatch, mint: bool
) -> None:
    """`skep run --delegated-by queen@operator` up to run creation: the fake
    runner fires ``on_run_created`` (when ``mint``) and refuses, as a dispatch
    refusal does — no worker runs."""
    from skep.supervisor import cli_cmds

    def fake_run_task(repo: Path, instructions: str, **kwargs: Any) -> Any:
        if mint:
            kwargs["on_run_created"]("task-cli")
        raise ValueError("stop here")

    store = RunStore(home.db_path)
    try:
        store.add_project_policy(
            project_id="app", name="app", strategy="trusted_local_dev", phase="build",
            policy={
                "allowed_shell_commands": [["uv", "sync"], ["git", "status"]],
                "default_network": ["pypi.org", "example.com"],
            },
        )  # fmt: skip
    finally:
        store.close()
    monkeypatch.setattr(cli_cmds, "run_task", fake_run_task)
    run = ["--home", str(home.home.parent), "run", str(repo), "x", "--quiet"]
    main([*run, "--delegated-by", "queen@operator"])


def test_a_cli_delegated_runs_dispatch_rows_carry_task_engine_and_caste(
    home: SupervisorConfig, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli_run(home, repo, monkeypatch, mint=True)
    rows = [r for r in _rows(home) if r["actor"] == "dispatch"]
    assert rows  # the drop beyond the delegator
    assert {tuple(r[k] for k in BLAME) for r in rows} == {("task-cli", "builtin", "coding")}
    assert _rows(home)[-1]["source"] == "delegated"  # the hop row still last


def test_a_cli_run_refused_before_creation_still_logs_its_rows_id_less(
    home: SupervisorConfig, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli_run(home, repo, monkeypatch, mint=False)
    rows = [r for r in _rows(home) if r["actor"] == "dispatch"]
    assert rows and all((r["engine"], r["caste"]) == ("builtin", "coding") for r in rows)
    assert not any("task_id" in r for r in rows)  # no run was ever minted (I8)


def _schedule_tick(home: SupervisorConfig, repo: Path, outcome: Any, caste: str = "coding") -> None:
    """One due schedule held to role ``ops``; the fake dispatch returns
    ``outcome`` (or raises it)."""
    from skep.supervisor.scheduler import make_schedule, run_due

    from .test_delegation import _custom_role
    from .test_schedule_role import PROJECT

    _custom_role(home, "ops", policy_layer="operator")

    def dispatch(repo: Path, instructions: str, **kwargs: Any) -> Any:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    store = RunStore(home.db_path)
    try:
        store.add_project_policy(
            project_id="app", name="app", strategy="trusted_local_dev", phase="build",
            policy=PROJECT,
        )  # fmt: skip
        nightly = make_schedule(
            name="nightly", repo=repo, instructions="x", interval_seconds=86400,
            start_at="2026-06-11T00:00:00Z", role="ops", worker_kind=caste,
        )  # fmt: skip
        store.add_schedule(nightly)
        holder = ConfigHolder(home, store)
        run_due(
            store=store, config=home, now="2026-06-11T09:00:00Z", dispatch=dispatch, holder=holder
        )
    finally:
        store.close()


def test_a_schedules_dispatch_rows_carry_task_engine_and_caste(
    home: SupervisorConfig, repo: Path
) -> None:
    from types import SimpleNamespace

    record = SimpleNamespace(task_id="task-s", state="completed", summary=None)
    _schedule_tick(home, repo, SimpleNamespace(record=record))
    rows = [r for r in _rows(home) if r["actor"] == "dispatch"]
    assert rows  # uv sync and example.com, beyond the ops role
    assert {tuple(r[k] for k in BLAME) for r in rows} == {("task-s", "builtin", "coding")}
    assert _rows(home)[-1]["source"] == "delegated"  # the hop row still last


def test_a_schedule_whose_dispatch_fails_logs_its_rows_id_less(
    home: SupervisorConfig, repo: Path
) -> None:
    _schedule_tick(home, repo, RuntimeError("no worker"))
    rows = [r for r in _rows(home) if r["actor"] == "dispatch"]
    assert rows and all((r["engine"], r["caste"]) == ("builtin", "coding") for r in rows)
    assert not any("task_id" in r for r in rows)


# --- review R1 (E1-E3): dispatch rows at run creation, engine only for coding


def _serve_dispatch(
    home: SupervisorConfig,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_run_task: Any,
    *,
    chain: list[str] | None = None,
    caste: str = "coding",
    id_timeout: float = 30.0,
) -> Any:
    """``submit_run`` through the real ``Dispatcher`` (``run_task`` faked), as
    ``POST /api/runs`` dispatches; the pool drained before returning."""
    from skep.supervisor.serve import jobs
    from skep.supervisor.serve.actions import submit_run

    monkeypatch.setattr(jobs, "run_task", fake_run_task)
    store = RunStore(home.db_path)
    holder = ConfigHolder(home, store)
    runner = jobs.Dispatcher(holder, store, id_timeout_seconds=id_timeout)
    try:
        store.add_project_policy(
            project_id="app", name="app", strategy="trusted_local_dev", phase="build",
            policy={
                "allowed_shell_commands": [["uv", "sync"], ["git", "status"]],
                "default_network": ["pypi.org", "example.com"],
            },
        )  # fmt: skip
        return submit_run(
            holder, runner, store, repo=str(repo), instructions="x",
            delegation_chain=chain or ["queen@operator"], caste=caste,
        )  # fmt: skip
    finally:
        runner.shutdown(wait=True)
        store.close()


def _dispatch_rows(home: SupervisorConfig) -> list[dict[str, Any]]:
    return [r for r in _rows(home) if r["actor"] == "dispatch"]


def _no_duplicates(rows: list[dict[str, Any]]) -> bool:
    return len({(r["scope"], r["value"]) for r in rows}) == len(rows)


def test_dispatch_rows_are_written_with_the_id_before_the_worker_starts(
    home: SupervisorConfig, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[dict[str, Any]]] = []

    def fake_run_task(repo: Path, instructions: str, **kwargs: Any) -> None:
        kwargs["on_run_created"]("task-d")
        log = home.audit_dir / "policy" / "decisions.jsonl"
        seen.append(_dispatch_rows(home) if log.exists() else [])  # the worker spawns here

    assert _serve_dispatch(home, repo, monkeypatch, fake_run_task) == "task-d"
    (at_spawn,) = seen
    assert at_spawn and {r["task_id"] for r in at_spawn} == {"task-d"}
    rows = _dispatch_rows(home)
    assert rows == at_spawn and _no_duplicates(rows)
    assert _rows(home)[-1]["source"] == "delegated"  # the hop row still last


def test_a_dispatch_that_fails_before_creation_logs_its_rows_once_id_less(
    home: SupervisorConfig, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run_task(repo: Path, instructions: str, **kwargs: Any) -> None:
        raise ValueError("worktree failed")

    with pytest.raises(HTTPException) as refused:
        _serve_dispatch(home, repo, monkeypatch, fake_run_task)
    assert refused.value.status_code == 500
    rows = _dispatch_rows(home)
    assert rows and not any("task_id" in r for r in rows) and _no_duplicates(rows)
    assert not any(r.get("source") == "delegated" for r in _rows(home))  # no run, no hop


def test_a_timed_out_dispatch_that_still_starts_logs_its_rows_with_the_id(
    home: SupervisorConfig, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    go = threading.Event()

    def fake_run_task(repo: Path, instructions: str, **kwargs: Any) -> None:
        go.wait(5)  # set only after the dispatch has timed out
        kwargs["on_run_created"]("task-late")

    threading.Timer(0.5, go.set).start()
    with pytest.raises(HTTPException):
        _serve_dispatch(home, repo, monkeypatch, fake_run_task, id_timeout=0.05)
    rows = _dispatch_rows(home)  # never flushed id-less while the run could still start
    assert rows and {r.get("task_id") for r in rows} == {"task-late"} and _no_duplicates(rows)


def test_a_decision_log_failure_never_fails_a_started_dispatch(
    home: SupervisorConfig,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from skep.supervisor.serve import actions

    def broken(*_: Any, **__: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(actions, "log_decision", broken)

    def fake_run_task(repo: Path, instructions: str, **kwargs: Any) -> None:
        kwargs["on_run_created"]("task-ok")

    assert _serve_dispatch(home, repo, monkeypatch, fake_run_task) == "task-ok"
    assert "dispatch decision rows for task-ok were not logged" in caplog.text


def test_a_dispatch_the_pool_never_took_logs_its_rows_once_id_less(
    home: SupervisorConfig, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor.serve import jobs

    real_init = jobs.Dispatcher.__init__

    def shut(self: Any, *args: Any, **kwargs: Any) -> None:
        real_init(self, *args, **kwargs)
        self.shutdown()  # serve exiting: the pool refuses new work

    monkeypatch.setattr(jobs.Dispatcher, "__init__", shut)
    with pytest.raises(RuntimeError):
        _serve_dispatch(home, repo, monkeypatch, lambda *_a, **_k: None)
    rows = _dispatch_rows(home)
    assert rows and not any("task_id" in r for r in rows) and _no_duplicates(rows)


def test_a_researcher_dispatch_row_names_no_coding_engine(
    home: SupervisorConfig, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run_task(repo: Path, instructions: str, **kwargs: Any) -> None:
        kwargs["on_run_created"]("task-r")

    _serve_dispatch(home, repo, monkeypatch, fake_run_task, caste="researcher")
    rows = _dispatch_rows(home)
    assert rows and all(r["caste"] == "researcher" and "engine" not in r for r in rows)


@pytest.mark.parametrize(("caste", "engine"), [("coding", "builtin"), ("researcher", None)])
def test_the_dispatch_gates_refusal_row_carries_engine_and_caste(
    home: SupervisorConfig, repo: Path, caste: str, engine: str | None
) -> None:
    from skep.supervisor.serve.actions import dispatch_run_decision

    store = RunStore(home.db_path)
    try:
        decision = dispatch_run_decision(
            ConfigHolder(home, store), store, repo=str(repo), caste=caste,
            delegation_chain=["queen@assistant"],
        )  # fmt: skip
    finally:
        store.close()
    assert decision.verdict == "deny"
    (row,) = _dispatch_rows(home)
    assert row["caste"] == caste and row.get("engine") == engine


def test_a_cli_researcher_runs_rows_name_no_coding_engine(
    home: SupervisorConfig, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import cli_cmds

    def fake_run_task(repo: Path, instructions: str, **kwargs: Any) -> Any:
        kwargs["on_run_created"]("task-cr")
        raise ValueError("stop here")

    store = RunStore(home.db_path)
    try:
        store.add_project_policy(
            project_id="app", name="app", strategy="trusted_local_dev", phase="build",
            policy={
                "allowed_shell_commands": [["uv", "sync"], ["git", "status"]],
                "default_network": ["pypi.org", "example.com"],
            },
        )  # fmt: skip
    finally:
        store.close()
    monkeypatch.setattr(cli_cmds, "run_task", fake_run_task)
    run = ["--home", str(home.home.parent), "run", str(repo), "x", "--quiet"]
    main([*run, "--caste", "researcher", "--delegated-by", "queen@operator"])
    rows = _dispatch_rows(home)
    assert rows and all(r["caste"] == "researcher" and "engine" not in r for r in rows)


def test_a_researcher_schedules_rows_name_no_coding_engine(
    home: SupervisorConfig, repo: Path
) -> None:
    from types import SimpleNamespace

    record = SimpleNamespace(task_id="task-sr", state="completed", summary=None)
    _schedule_tick(home, repo, SimpleNamespace(record=record), caste="researcher")
    rows = _dispatch_rows(home)
    assert rows and all(r["caste"] == "researcher" and "engine" not in r for r in rows)
