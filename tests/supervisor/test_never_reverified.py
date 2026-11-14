"""ES-F291 (L1, I1 I2 I8): a completed run with a patch and no
re-verification record never lands — every landing face refuses it and
teaches the re-verify verb; the approval card and the Queen's guidance say
so before anyone clicks."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from skep.cli import main
from skep.supervisor import RunStore, SupervisorConfig, mint_task
from skep.supervisor.contracts_io import write_task_file
from skep.supervisor.serve import actions
from skep.supervisor.serve.tools import _get_run_guidance

from .conftest import git


def _unverified_run(store: RunStore, repo: Path, audit_dir: Path, tmp_path: Path) -> str:
    """The L1 state: completed, patch on disk, re-verification never ran."""
    task = mint_task(workspace=repo, instructions="land me")
    store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
    store.transition(task.task_id, "completed")
    patch = tmp_path / f"{task.task_id}.patch"
    patch.write_text(
        "diff --git a/new.txt b/new.txt\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+hi\n",
        encoding="utf-8",
    )
    store.add_artifact(task.task_id, kind="patch", audit_path=patch, sha256="0" * 64)
    write_task_file(task, audit_dir / task.task_id / "task.json")
    return task.task_id


def test_land_run_and_approve_refuse_a_run_never_reverified(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    store = RunStore(config.db_path)
    try:
        task_id = _unverified_run(store, repo, config.audit_dir, tmp_path)
        run = actions.require_run(store, task_id)
        with pytest.raises(HTTPException) as land:
            actions.land_run(store, run, "tester")
        assert land.value.status_code == 409
        assert "never re-verified" in str(land.value.detail)
        assert f"skep review {task_id} --reverify" in str(land.value.detail)
        assert store.approvals_for(task_id) == []  # no orphan review left behind

        review_id = store.enqueue_approval(task_id, action="apply_patch", reason="land")
        view = actions.approval_view(store, store.get_approval(review_id), events=[])
        assert "never re-verified" in view["reverification_warning"]
        with pytest.raises(HTTPException) as approve:
            actions.apply_patch(store, run, review_id, "tester", None)
        assert "--reverify" in str(approve.value.detail)
        assert store.get_approval(review_id).status == "pending"  # type: ignore[union-attr]
        assert git(repo, "branch", "--list", f"skep/{task_id}").stdout == ""  # nothing landed
    finally:
        store.close()


def test_queen_guidance_never_calls_an_unverified_run_ready_to_land() -> None:
    guidance = _get_run_guidance({"state": "completed", "task_id": "t1"}, None, has_patch=True)
    assert guidance is not None
    assert "never re-verified" in guidance.lower()
    assert "skep review t1 --reverify" in guidance
    assert "ready to land" not in guidance


def test_cli_approve_refuses_and_reverify_writes_the_record(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    supervisor_home = home / "supervisor"
    supervisor_home.mkdir(parents=True)
    store = RunStore(supervisor_home / "supervisor.sqlite3")
    try:
        task_id = _unverified_run(store, repo, supervisor_home / "audit", tmp_path)
    finally:
        store.close()

    assert main(["--home", str(home), "review", task_id, "--approve", "--actor", "t"]) == 2
    err = capsys.readouterr().err
    assert "never re-verified" in err and f"skep review {task_id} --reverify" in err

    assert main(["--home", str(home), "review", task_id, "--reverify"]) == 0
    assert "re-verified" in capsys.readouterr().out
    store = RunStore(supervisor_home / "supervisor.sqlite3")
    try:
        assert store.reverification_for(task_id) is not None
    finally:
        store.close()
    # One record per run: --reverify is for the never-re-verified state only.
    assert main(["--home", str(home), "review", task_id, "--reverify"]) == 2
    assert "already re-verified" in capsys.readouterr().err


def test_a_land_during_live_g10_says_wait_not_reverify(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    """Ingest commits ``completed`` before G10 runs: while the supervisor is
    alive and re-verifying, a land is refused with "still running", and
    await_runs does not call the run settled yet."""
    from skep.supervisor.serve.settings import ConfigHolder
    from skep.supervisor.serve.tools import execute_read_tool
    from skep.supervisor.store import process_owner

    store = RunStore(config.db_path)
    try:
        task_id = _unverified_run(store, repo, config.audit_dir, tmp_path)
        assert store.claim_run_supervisor(task_id, expected=None, owner=process_owner())
        with pytest.raises(HTTPException) as land:
            actions.land_run(store, actions.require_run(store, task_id), "tester")
        assert "still running" in str(land.value.detail)
        waited = execute_read_tool(
            "await_runs",
            {"task_ids": [task_id], "timeout_seconds": 1},
            store=store,
            holder=ConfigHolder(config, store),
        )
        assert waited["settled"] is False
    finally:
        store.close()


def test_reverify_refuses_with_the_reason(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    """`--reverify` answers each state it will not touch in words."""
    from skep.supervisor.dispatch import reverify_unverified

    store = RunStore(config.db_path)
    try:
        task_id = _unverified_run(store, repo, config.audit_dir, tmp_path)
        (config.audit_dir / task_id / "task.json").unlink()
        record = store.get_run(task_id)
        assert record is not None
        assert "no task evidence" in reverify_unverified(store, config, record)
        store.transition(task_id, "failed")
        record = store.get_run(task_id)
        assert record is not None
        assert "not completed" in reverify_unverified(store, config, record)
    finally:
        store.close()


def test_one_reverify_per_run_across_serve_and_cli(
    repo: Path, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ES-F290 follow-up (I2): serve's startup sweep runs in the background,
    so it and a CLI `--reverify` may reach one run together. Each takes the
    run in a compare-and-set first; the loser re-verifies nothing. A live
    owner (this serve, finishing a re-adopted worker) is never doubled, and
    a re-verify that blows up hands the run back."""
    import json

    from skep.supervisor import dispatch
    from skep.supervisor.store import process_owner

    calls: list[str] = []
    monkeypatch.setattr(dispatch, "_reverify_after_restart", lambda *a, **k: calls.append("g10"))
    store = RunStore(config.db_path)
    try:
        task_id = _unverified_run(store, repo, config.audit_dir, tmp_path)
        record = store.get_run(task_id)
        assert record is not None
        dead = json.dumps({"pid": 1, "started": "long ago"})
        assert store.claim_run_supervisor(task_id, expected=None, owner=dead)

        # Another process claims it between our read and our claim.
        real_claim = store.claim_run_supervisor
        monkeypatch.setattr(store, "claim_run_supervisor", lambda *a, **k: False)
        assert "still running" in dispatch.reverify_unverified(store, config, record)
        monkeypatch.setattr(store, "claim_run_supervisor", real_claim)
        assert calls == []

        # A live owner (this very process) re-verifies it itself.
        assert store.claim_run_supervisor(task_id, expected=dead, owner=process_owner())
        assert "still running" in dispatch.reverify_unverified(store, config, record)
        assert dispatch.reverify_unverified_runs(store, config) == []
        assert calls == []

        # A failed re-verify hands the run back to its (dead) previous owner.
        assert store.claim_run_supervisor(task_id, expected=process_owner(), owner=dead)

        def boom(*_: object, **__: object) -> None:
            raise RuntimeError("verify blew up")

        monkeypatch.setattr(dispatch, "_reverify_after_restart", boom)
        assert dispatch.reverify_unverified_runs(store, config) == []  # logged, not raised
        assert store.run_supervisor(task_id) == dead
        assert "never re-verified" in str(actions.never_reverified_error(store, task_id))
    finally:
        store.close()
