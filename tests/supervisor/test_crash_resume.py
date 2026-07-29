"""v72-F8: R8 closed — same-worktree crash resume.

All three parts existed and never met: salvaged checkpoints (ingest),
worktree reuse (_resume_workspace), and the resume_of dispatch seam. These
tests pin the joins: a crash WITH a checkpoint keeps its worktree through
recovery and sweeps, the crash notification offers resume_run, and
resume_crashed_run re-dispatches through the existing seam with the fate
stated honestly. A crash WITHOUT a checkpoint keeps today's behavior.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task
from skep.supervisor.dispatch import (
    _keep_worktree_names,
    recover_interrupted_runs,
    salvaged_checkpoint_version,
)
from skep.supervisor.serve.actions import resume_crashed_run
from skep.supervisor.serve.run_status import run_terminal_text
from skep.worker_contract import RESUME_CHECKPOINT_STATE_KEY

from .test_recover import _strand_run


def _salvage_checkpoint(config: SupervisorConfig, task_id: str, *, cursor: int = 3) -> None:
    checkpoint_dir = config.audit_dir / task_id
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    (checkpoint_dir / "resume-checkpoint.json").write_text(
        json.dumps({RESUME_CHECKPOINT_STATE_KEY: {"version": 2, "cursor": cursor}})
    )


def test_crash_with_checkpoint_keeps_worktree_and_offers_resume(
    config: SupervisorConfig, repo: Path
) -> None:
    store = RunStore(config.db_path)
    try:
        task_id, workspace = _strand_run(store, config, repo)
        _salvage_checkpoint(config, task_id)
        recover_interrupted_runs(store, config)
        record = store.get_run(task_id)
        assert record is not None and record.state == "worker_crashed"
        assert workspace.is_dir()  # the tree survived the crash sweep
        assert workspace.name in _keep_worktree_names(store)  # and future sweeps
        notice = run_terminal_text(store, task_id, audit_dir=config.audit_dir)
        assert notice is not None
        text, kind = notice
        # v73-F2: the model-free deck path leads; the verb keeps its name.
        assert f"/resume {task_id}" in text
        assert "resume_run" in text
        # v78-F1: a resumable crash is a call to action.
        assert kind == "action_needed"
    finally:
        store.close()


def test_crash_without_checkpoint_keeps_todays_behavior(
    config: SupervisorConfig, repo: Path
) -> None:
    store = RunStore(config.db_path)
    try:
        task_id, workspace = _strand_run(store, config, repo)
        recover_interrupted_runs(store, config)
        assert not workspace.is_dir()  # removed exactly as before
        assert salvaged_checkpoint_version(config, task_id) == 0
        notice = run_terminal_text(store, task_id, audit_dir=config.audit_dir)
        assert notice is not None
        text, kind = notice
        assert "resume_run" not in text
        # v78-F1: without a checkpoint there is nothing to act on.
        assert kind == "info"
    finally:
        store.close()


def test_resume_reuses_the_preserved_worktree_and_states_the_fate(
    config: SupervisorConfig, repo: Path
) -> None:
    store = RunStore(config.db_path)
    submitted: dict[str, Any] = {}

    class FakeRunner:
        def submit(self, submit_repo: Path, instructions: str, **kwargs: Any) -> str:
            submitted["repo"] = submit_repo
            submitted["instructions"] = instructions
            submitted.update(kwargs)
            return "resumed-1"

    try:
        task_id, workspace = _strand_run(store, config, repo)
        _salvage_checkpoint(config, task_id, cursor=3)
        recover_interrupted_runs(store, config)

        result = resume_crashed_run(store, config, FakeRunner(), task_id, "operator")  # type: ignore[arg-type]
        assert result["resumed_as"] == "resumed-1"
        assert result["resume_of"] == task_id
        assert "continuing in place from checkpoint cursor 3" in result["worktree"]
        assert submitted["resume_of"] == task_id
        assert submitted["dispatch_decision"].reason == "dispatch.allow.resume_after_crash"

        # Worktree gone → the fate says step 0, before anyone is surprised.
        import shutil

        shutil.rmtree(workspace)
        second = resume_crashed_run(store, config, FakeRunner(), task_id, "operator")  # type: ignore[arg-type]
        assert "replay from step 0" in second["worktree"]
    finally:
        store.close()


def test_react_checkpoint_without_cursor_states_the_saved_round(
    config: SupervisorConfig, repo: Path
) -> None:
    """v73-F5: react checkpoints (v69) carry rounds, not a plan cursor — the
    field fate line printed 'checkpoint cursor None' for a perfect resume,
    a true state rendered as its failure mode (I8)."""
    store = RunStore(config.db_path)

    class FakeRunner:
        def submit(self, submit_repo: Path, instructions: str, **kwargs: Any) -> str:
            return "resumed-react-1"

    try:
        task_id, _workspace = _strand_run(store, config, repo)
        checkpoint_dir = config.audit_dir / task_id
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        (checkpoint_dir / "resume-checkpoint.json").write_text(
            json.dumps({RESUME_CHECKPOINT_STATE_KEY: {"version": 2}})  # no cursor
        )
        recover_interrupted_runs(store, config)

        result = resume_crashed_run(store, config, FakeRunner(), task_id, "operator")  # type: ignore[arg-type]
        assert result["worktree"] == "preserved — continuing in place from the saved round"
        assert "None" not in result["worktree"]
    finally:
        store.close()


def test_resume_refuses_wrong_state_and_missing_checkpoint(
    config: SupervisorConfig, repo: Path, tmp_path: Path
) -> None:
    store = RunStore(config.db_path)

    class NeverRunner:
        def submit(self, *args: Any, **kwargs: Any) -> str:
            raise AssertionError("must not dispatch")

    try:
        # Wrong state: a completed run does not resume.
        done = mint_task(workspace=tmp_path / "ws", instructions="x", budget=DEFAULT_BUDGET)
        store.create_run(done, repo=repo, ref=None, execution_mode="sandbox")
        store.transition(done.task_id, "completed", None)
        with pytest.raises(ValueError, match="only continues"):
            resume_crashed_run(store, config, NeverRunner(), done.task_id, "op")  # type: ignore[arg-type]

        # Crashed but checkpoint-less: the error teaches the alternative.
        task_id, _workspace = _strand_run(store, config, repo)
        recover_interrupted_runs(store, config)
        with pytest.raises(ValueError, match="no resume checkpoint"):
            resume_crashed_run(store, config, NeverRunner(), task_id, "op")  # type: ignore[arg-type]
    finally:
        store.close()


def test_resume_command_route_resumes_a_crashed_run_without_a_model(
    config: SupervisorConfig, repo: Path
) -> None:
    """v73-F2: /resume maps onto the commands API — propose resume_run as an
    operator command, confirm it, and the crashed run continues. No LLM is
    configured anywhere in this test: the recovery path cannot depend on the
    thing that is down."""
    from .conftest import serve_client

    store = RunStore(config.db_path)
    try:
        task_id, _workspace = _strand_run(store, config, repo)
        _salvage_checkpoint(config, task_id)
        recover_interrupted_runs(store, config)
    finally:
        store.close()

    client = serve_client(config)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    proposed = client.post(
        f"/api/chats/{chat_id}/commands",
        json={"tool": "resume_run", "args": {"task_id": task_id}},
    )
    assert proposed.status_code == 201
    action_id = proposed.json()["action_id"]
    confirmed = client.post(f"/api/chats/{chat_id}/commands/{action_id}/confirm")
    assert confirmed.status_code == 200
    body = confirmed.json()
    assert body["ok"] is True
    assert body["result"]["resume_of"] == task_id
    resumed_as = body["result"]["resumed_as"]
    verify = RunStore(config.db_path)
    try:
        resumed = verify.get_run(resumed_as)
        assert resumed is not None
        assert resumed.resume_of == task_id
    finally:
        verify.close()


def test_a_resumed_chain_releases_the_worktree_to_the_sweep(
    config: SupervisorConfig, repo: Path
) -> None:
    store = RunStore(config.db_path)
    try:
        task_id, workspace = _strand_run(store, config, repo)
        _salvage_checkpoint(config, task_id)
        recover_interrupted_runs(store, config)
        assert workspace.name in _keep_worktree_names(store)
        # A successor exists → the crashed run's tree leaves the keep-list
        # (the successor's own active/pending rules take over from here).
        successor = mint_task(
            workspace=workspace, instructions="x", budget=DEFAULT_BUDGET, resume_of=task_id
        )
        store.create_run(successor, repo=repo, ref=None, execution_mode="sandbox")
        store.transition(successor.task_id, "completed", None)
        assert workspace.name not in {
            Path(w).name for w in store.crashed_run_workspaces()
        }
    finally:
        store.close()
