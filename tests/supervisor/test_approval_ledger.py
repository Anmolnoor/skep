from __future__ import annotations

from pathlib import Path

from skep.supervisor import RunStore, mint_task
from skep.worker_contract import CONTRACT_VERSION, Event


def test_approved_approval_is_recorded_in_ledger_with_context(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = RunStore(tmp_path / "supervisor.sqlite3")
    try:
        task = mint_task(
            workspace=repo,
            instructions="Run a non-verify shell command that needs approval." * 10,
        )
        store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
        store.ingest_events(
            [
                Event.model_validate(
                    {
                        "contract_version": CONTRACT_VERSION,
                        "event_id": "approval-requested-1",
                        "seq": 1,
                        "task_id": task.task_id,
                        "trace_id": task.trace_id,
                        "ts": "2026-06-26T00:00:00Z",
                        "type": "approval.requested",
                        "payload": {
                            "action": "shell.run",
                            "reason": "shell.run requires approval for command: python write.py",
                            "decision": {
                                "verdict": "require_approval",
                                "reason": (
                                    "capability.require_approval.shell_nonverify_not_allowlisted"
                                ),
                                "detail": "python write.py",
                            },
                        },
                    }
                )
            ]
        )
        review_id = store.enqueue_approval(
            task.task_id,
            action="shell.run",
            reason="shell.run requires approval for command: python write.py",
        )
        store.resolve_approval(review_id, approved=True, actor="tester")
        store.transition(task.task_id, "completed")

        entries = store.ledger_for_repo(repo)

        assert len(entries) == 1
        entry = entries[0]
        assert entry.review_id == review_id
        assert entry.task_id == task.task_id
        assert entry.action == "shell.run"
        assert entry.resource == "python write.py"
        assert entry.reason == "shell.run requires approval for command: python write.py"
        assert entry.repo_path == str(repo)
        assert entry.instructions_snippet == task.instructions[:200]
        assert entry.approved_by == "tester"
        assert entry.task_outcome == "completed"
        assert entry.remembered is False
    finally:
        store.close()


def test_deny_transitions_a_gated_run_to_rejected(tmp_path: Path) -> None:
    """v48-F3: every deny path routes through resolve_approval; a denied gate
    must not leave the run in pending_approval forever (doctor kept flagging
    already-denied runs as stale in the field)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    store = RunStore(tmp_path / "supervisor.sqlite3")
    try:
        task = mint_task(workspace=repo, instructions="Do a gated thing.")
        store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
        store.transition(task.task_id, "pending_approval", "gate")
        review_id = store.enqueue_approval(task.task_id, action="shell.run", reason="gate")

        store.resolve_approval(review_id, approved=False, actor="tester")

        run = store.get_run(task.task_id)
        assert run is not None and run.state == "rejected"
        transitions = [(state, detail) for state, detail, _ in store.transitions_for(task.task_id)]
        assert ("rejected", "gate denied by tester") in transitions
    finally:
        store.close()


def test_deny_of_a_landing_review_leaves_a_completed_run_completed(tmp_path: Path) -> None:
    """Denying an apply_patch review refuses the LANDING, not the run."""
    repo = tmp_path / "repo"
    repo.mkdir()
    store = RunStore(tmp_path / "supervisor.sqlite3")
    try:
        task = mint_task(workspace=repo, instructions="Finish, then refuse the landing.")
        store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
        store.transition(task.task_id, "completed")
        review_id = store.enqueue_approval(
            task.task_id, action="apply_patch", reason="patch application review"
        )

        store.resolve_approval(review_id, approved=False, actor="tester")

        run = store.get_run(task.task_id)
        assert run is not None and run.state == "completed"
    finally:
        store.close()


def test_deny_leaves_the_run_gated_while_another_approval_is_pending(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = RunStore(tmp_path / "supervisor.sqlite3")
    try:
        task = mint_task(workspace=repo, instructions="Two gates, one verdict.")
        store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
        store.transition(task.task_id, "pending_approval", "gate")
        first = store.enqueue_approval(task.task_id, action="shell.run", reason="gate one")
        store.enqueue_approval(task.task_id, action="shell.run", reason="gate two")

        store.resolve_approval(first, approved=False, actor="tester")

        run = store.get_run(task.task_id)
        assert run is not None and run.state == "pending_approval"
    finally:
        store.close()


def test_denied_approval_is_not_recorded_in_ledger(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = RunStore(tmp_path / "supervisor.sqlite3")
    try:
        task = mint_task(workspace=repo, instructions="Do a gated thing.")
        store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
        review_id = store.enqueue_approval(task.task_id, action="git.commit", reason="need git")
        store.resolve_approval(review_id, approved=False, actor="tester")

        assert store.ledger_for_repo(repo) == []
    finally:
        store.close()
