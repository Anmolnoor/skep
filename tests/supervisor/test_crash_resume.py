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
from datetime import UTC, datetime
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
        with pytest.raises(ValueError, match="dispatch a fresh run"):
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
        # v107-F1: a completed successor holds the tree until ITS OWN
        # re-verification confirms — the keep answer needs the confirmed bit.
        assert workspace.name in {Path(w).name for w in store.preserved_run_workspaces()}
        store.record_reverification(
            successor.task_id,
            outcome="passed",
            worker_outcome="passed",
            confirmed=True,
            commands=["true"],
            exit_codes=[0],
            detail="test",
        )
        assert workspace.name not in {Path(w).name for w in store.preserved_run_workspaces()}
    finally:
        store.close()


def test_failed_run_keeps_its_worktree_and_resumes_in_place(
    repo: Path, config: SupervisorConfig
) -> None:
    """v107-F1: a failed run's warm tree IS the resume value (five cold yarn
    installs across the 2026-08-03 acceptance arc) — no checkpoint needed."""
    store = RunStore(config.db_path)
    try:
        task_id, workspace = _strand_run(store, config, repo)
        store.transition(task_id, "failed", "agent exited 1")
        assert workspace.name in _keep_worktree_names(store)
        assert salvaged_checkpoint_version(config, task_id) < 2  # no checkpoint

        class _Runner:
            def submit(self, *args: Any, **kwargs: Any) -> str:
                self.kwargs = kwargs
                return "resumed-1"

        runner = _Runner()
        out = resume_crashed_run(store, config, runner, task_id, "tester")  # type: ignore[arg-type]
        assert out["resumed_as"] == "resumed-1"
        assert "warm worktree" in out["worktree"]
        assert runner.kwargs["resume_of"] == task_id
    finally:
        store.close()


def test_dispatch_surfaces_hint_at_a_kept_resumable_worktree(
    repo: Path, config: SupervisorConfig
) -> None:
    """v109-F6: the field fix-chain became three fresh dispatches for one task
    because nothing at the dispatch surface mentioned the kept tree. One shared
    hint line (actions.preserved_resumable_hint) rides both faces — the chat
    tool result and POST /api/runs — and never blocks the dispatch."""
    from skep.supervisor.serve.actions import preserved_resumable_hint
    from skep.supervisor.serve.settings import ConfigHolder
    from skep.supervisor.serve.tools import execute_mutation

    store = RunStore(config.db_path)
    try:
        task_id, _workspace = _strand_run(store, config, repo)
        store.transition(task_id, "failed", "agent exited 1")
        holder = ConfigHolder(config, store)

        hint = preserved_resumable_hint(holder, store, repo=str(repo))
        assert hint is not None
        assert task_id[:12] in hint
        assert "failed" in hint and "worktree kept" in hint
        assert "resume_run" in hint and "diagnose_run" in hint
        # A ref filter is exact: the stranded run has no ref, so a dispatch
        # extending another branch does not hint about it.
        assert preserved_resumable_hint(holder, store, repo=str(repo), ref="other") is None

        # The chat face carries the same line on the dispatch result.
        class FakeRunner:
            def submit(self, *args: Any, **kwargs: Any) -> str:
                return "fresh-1"

        result = execute_mutation(
            "dispatch_run",
            {"repo": str(repo), "instructions": "try again", "execution_mode": "sandbox"},
            store=store,
            holder=holder,
            runner=FakeRunner(),  # type: ignore[arg-type]
            actor="chat-user",
        )
        assert result["task_id"] == "fresh-1"  # the dispatch still proceeded
        assert result["hint"] == hint
    finally:
        store.close()


def test_dispatch_hint_absent_without_a_live_resumable_sibling(
    repo: Path, config: SupervisorConfig
) -> None:
    """The hint's absence cases: no prior run, a non-resumable state, a swept
    tree, a TTL-expired row, and a run whose chain already moved on."""
    import shutil

    from skep.supervisor.serve.actions import preserved_resumable_hint
    from skep.supervisor.serve.settings import ConfigHolder

    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        assert preserved_resumable_hint(holder, store, repo=str(repo)) is None  # no runs

        # Completed-unconfirmed keeps its tree for diagnose_run, but it is not
        # resumable — the state filter must keep it out of the hint.
        done_id, _done_workspace = _strand_run(store, config, repo)
        store.transition(done_id, "completed", None)
        assert preserved_resumable_hint(holder, store, repo=str(repo)) is None

        task_id, workspace = _strand_run(store, config, repo)
        store.transition(task_id, "failed", "agent exited 1")
        assert preserved_resumable_hint(holder, store, repo=str(repo)) is not None

        # TTL-expired row: the sweep owns it now, the hint must not name it.
        store._conn.execute(
            "UPDATE runs SET updated_at = '2020-01-01T00:00:00Z' WHERE task_id = ?",
            (task_id,),
        )
        store._conn.commit()
        assert preserved_resumable_hint(holder, store, repo=str(repo)) is None
        store._conn.execute(
            "UPDATE runs SET updated_at = ? WHERE task_id = ?",
            (datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), task_id),
        )
        store._conn.commit()

        # Swept tree: the row remains, the warm workspace does not.
        shutil.rmtree(workspace)
        assert preserved_resumable_hint(holder, store, repo=str(repo)) is None
        workspace.mkdir()  # tree back → hint back (the disk check, isolated)
        assert preserved_resumable_hint(holder, store, repo=str(repo)) is not None

        # A successor releases the run from the hint exactly as from the keep set.
        successor = mint_task(
            workspace=workspace, instructions="x", budget=DEFAULT_BUDGET, resume_of=task_id
        )
        store.create_run(successor, repo=repo, ref=None, execution_mode="sandbox")
        assert preserved_resumable_hint(holder, store, repo=str(repo)) is None
    finally:
        store.close()


def test_rest_dispatch_response_carries_the_hint(repo: Path, config: SupervisorConfig) -> None:
    """POST /api/runs — the operator face — attaches the same hint line."""
    from .conftest import serve_client, wait_terminal

    store = RunStore(config.db_path)
    try:
        task_id, _workspace = _strand_run(store, config, repo)
        store.transition(task_id, "failed", "agent exited 1")
    finally:
        store.close()

    client = serve_client(config)
    response = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "sandbox",
        },
    )
    assert response.status_code == 202
    body = response.json()
    assert task_id[:12] in body["hint"]
    assert "resume_run" in body["hint"]
    wait_terminal(client, body["task_id"])


def test_preserved_worktree_ttl_expires_and_sweeps(repo: Path, config: SupervisorConfig) -> None:
    """v107-F1: preserved trees are not tenure — past the TTL the keep set
    releases them and the ticker sweep collects them."""
    from skep.supervisor.dispatch import sweep_expired_preserved_worktrees

    store = RunStore(config.db_path)
    try:
        task_id, workspace = _strand_run(store, config, repo)
        store.transition(task_id, "failed", "agent exited 1")
        assert workspace.name in _keep_worktree_names(store)
        # Age the run past the TTL directly (the sweep compares updated_at).
        store._conn.execute(
            "UPDATE runs SET updated_at = '2020-01-01T00:00:00Z' WHERE task_id = ?",
            (task_id,),
        )
        store._conn.commit()
        assert workspace.name not in _keep_worktree_names(store)
        pairs = store.expired_preserved_worktrees(max_age_seconds=86_400.0)
        assert (str(repo), str(workspace)) in pairs
        assert sweep_expired_preserved_worktrees(store, config) == 1
        assert not workspace.exists()
    finally:
        store.close()
