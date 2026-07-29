from __future__ import annotations

from pathlib import Path

from skep.supervisor import RunStore, mint_task
from skep.supervisor.permission_profile import derive_permission_profile


def _completed_run(store: RunStore, repo: Path, instructions: str) -> str:
    task = mint_task(workspace=repo, instructions=instructions)
    store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
    store.transition(task.task_id, "completed")
    return task.task_id


def test_permission_profile_uses_successful_remembered_matches(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    other_repo = tmp_path / "other"
    other_repo.mkdir()
    store = RunStore(tmp_path / "supervisor.sqlite3")
    try:
        install_task = _completed_run(store, repo, "Add a login page with JWT support")
        shell_id = store.record_approval_ledger(
            task_id=install_task,
            action="shell.run",
            resource="python -m pytest",
            reason="tests need to run",
            approved_by="tester",
            remembered=True,
        )
        network_id = store.record_approval_ledger(
            task_id=install_task,
            action="network.fetch",
            resource="https://pypi.org/simple/pyjwt/",
            reason="install PyJWT",
            approved_by="tester",
            remembered=True,
        )
        git_id = store.record_approval_ledger(
            task_id=install_task,
            action="git.commit",
            resource="git commit",
            reason="commit the verified patch",
            approved_by="tester",
            remembered=True,
        )

        unremembered = _completed_run(store, repo, "Add a login page with JWT support")
        store.record_approval_ledger(
            task_id=unremembered,
            action="network.fetch",
            resource="example.com",
            reason="not remembered",
            approved_by="tester",
            remembered=False,
        )

        failed = mint_task(workspace=repo, instructions="Add a login page with JWT support")
        store.create_run(failed, repo=repo, ref=None, execution_mode="workspace")
        store.transition(failed.task_id, "failed")
        store.record_approval_ledger(
            task_id=failed.task_id,
            action="shell.run",
            resource="npm test",
            reason="failed run",
            approved_by="tester",
            remembered=True,
        )

        unrelated = _completed_run(store, repo, "Rotate deployment credentials")
        store.record_approval_ledger(
            task_id=unrelated,
            action="network.fetch",
            resource="vault.example.com",
            reason="unrelated task",
            approved_by="tester",
            remembered=True,
        )

        other = _completed_run(store, other_repo, "Add a signup page")
        store.record_approval_ledger(
            task_id=other,
            action="network.fetch",
            resource="accounts.example.com",
            reason="other repo",
            approved_by="tester",
            remembered=True,
        )

        profile = derive_permission_profile(store, repo=repo, instructions="Add a signup page")

        assert profile.network == ("pypi.org",)
        assert profile.shell_allowlist == (("python", "-m", "pytest"),)
        assert profile.allow_git_mutation is True
        assert profile.source_entry_ids == (shell_id, network_id, git_id)
    finally:
        store.close()
