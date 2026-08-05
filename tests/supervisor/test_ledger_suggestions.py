"""v109-F8: the ledger closes the loop — asked N times, offer to remember.

The field store held 340 confirmed cards against 13 denies, the same install
command approved twice in one workspace, and remembered=0 on all 44 ledger
rows: skep never noticed the recurrence. Suggestions are DERIVED from the
ledger on read — deterministic engine code notices; only the operator's
confirm changes policy (I6). Remembering through any door marks every
matching ledger row (I13), which removes the key from the suggestion list by
construction.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.contracts_io import mint_task
from skep.supervisor.serve import actions
from skep.supervisor.serve.settings import ConfigHolder

from .conftest import serve_client

PIP = "python3 -m pip install youtube-transcript-api"


def _seed_approvals(
    store: RunStore,
    repo: Path,
    *,
    count: int,
    action: str = "shell.run",
    resource: str = PIP,
) -> list[str]:
    """N approved ledger rows for one key, each on its own run."""
    review_ids: list[str] = []
    for index in range(count):
        task = mint_task(workspace=repo, instructions=f"install deps (round {index})")
        store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
        review_id = store.enqueue_approval(
            task.task_id,
            action=action,
            reason=f"{action} requires approval for command: {resource}",
        )
        store.record_approval_ledger(
            task_id=task.task_id,
            action=action,
            resource=resource,
            reason=f"{action} requires approval for command: {resource}",
            approved_by="tester",
            approved_at=f"2026-08-0{index + 1}T00:00:00Z",
            review_id=review_id,
        )
        review_ids.append(review_id)
    return review_ids


def test_three_approvals_of_one_key_become_a_candidate(
    repo: Path, config: SupervisorConfig
) -> None:
    store = RunStore(config.db_path)
    try:
        _seed_approvals(store, repo, count=3)
        _seed_approvals(store, repo, count=1, resource="pytest -q")  # below threshold
        (candidate,) = store.ledger_remember_candidates()
        assert candidate["action"] == "shell.run"
        assert candidate["resource"] == PIP
        assert candidate["repo"] == str(repo)
        assert candidate["count"] == 3
    finally:
        store.close()


def test_remembered_rows_and_bare_action_resources_never_suggest(
    repo: Path, config: SupervisorConfig
) -> None:
    store = RunStore(config.db_path)
    try:
        _seed_approvals(store, repo, count=3)
        store.mark_ledger_remembered(action="shell.run", resource=PIP, repo_path=str(repo))
        assert store.ledger_remember_candidates() == []
        # A row whose resource fell back to the bare action name identifies
        # nothing rememberable.
        _seed_approvals(store, repo, count=3, action="network.fetch", resource="network.fetch")
        assert store.ledger_remember_candidates() == []
    finally:
        store.close()


def test_a_later_deny_of_the_same_command_resets_the_streak(
    repo: Path, config: SupervisorConfig
) -> None:
    store = RunStore(config.db_path)
    try:
        _seed_approvals(store, repo, count=3)
        task = mint_task(workspace=repo, instructions="one more try")
        store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
        denied_review = store.enqueue_approval(
            task.task_id,
            action="shell.run",
            reason=f"shell.run requires approval for command: {PIP}",
        )
        store.resolve_approval(denied_review, approved=False, actor="tester")
        assert store.ledger_remember_candidates() == []
    finally:
        store.close()


def test_floor_forbidden_keys_are_never_suggested(repo: Path, config: SupervisorConfig) -> None:
    """However often it was approved, the floor wins (I4's pattern)."""
    store = RunStore(config.db_path)
    try:
        _seed_approvals(store, repo, count=3, resource="git push origin main")
        assert store.ledger_remember_candidates()  # the ledger sees it...
        assert actions.ledger_remember_suggestions(store) == []  # ...the view refuses it
    finally:
        store.close()


def test_remember_ledger_entry_persists_and_marks(repo: Path, config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="proj-sug",
            name="suggestions",
            strategy="trusted_local_dev",
            phase="build",
            policy={},
        )
        store.add_project_binding(
            project_id="proj-sug", binding_kind="repo_path", binding_value=str(repo)
        )
        holder = ConfigHolder(config, store)
        _seed_approvals(store, repo, count=3)
        result = actions.remember_ledger_entry(
            store, holder, action="shell.run", resource=PIP, repo=str(repo)
        )
        assert result["remembered"] is True
        project = store.project_for_binding("repo_path", str(repo))
        assert project is not None
        assert PIP.split() in project.policy["allowed_shell_commands"]
        # Every historical row now says so, and the suggestion is gone.
        assert all(entry.remembered for entry in store.ledger_for_repo(repo))
        assert actions.ledger_remember_suggestions(store) == []
    finally:
        store.close()


def test_remember_ledger_entry_refuses_the_floor_and_unknown_actions(
    repo: Path, config: SupervisorConfig
) -> None:
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        with pytest.raises(HTTPException) as floor:
            actions.remember_ledger_entry(
                store, holder, action="shell.run", resource="sudo rm -rf /", repo=str(repo)
            )
        assert floor.value.status_code == 409
        with pytest.raises(HTTPException) as unknown:
            actions.remember_ledger_entry(
                store, holder, action="apply_patch", resource="apply_patch", repo=str(repo)
            )
        assert unknown.value.status_code == 409
    finally:
        store.close()


def test_the_nth_approval_carries_the_nudge(repo: Path, config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        review_ids = _seed_approvals(store, repo, count=3)
        suggestion = actions.remember_suggestion_for_review(store, review_ids[-1])
        assert suggestion is not None and "approval #3" in suggestion
        # Below the threshold there is no nudge.
        other = _seed_approvals(store, repo, count=1, resource="pytest -q")
        assert actions.remember_suggestion_for_review(store, other[0]) is None
    finally:
        store.close()


def test_suggestions_and_remember_ride_the_rest_face(repo: Path, config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="proj-sug",
            name="suggestions",
            strategy="trusted_local_dev",
            phase="build",
            policy={},
        )
        store.add_project_binding(
            project_id="proj-sug", binding_kind="repo_path", binding_value=str(repo)
        )
        _seed_approvals(store, repo, count=3)
    finally:
        store.close()
    client = serve_client(config)
    listed = client.get("/api/ledger/suggestions").json()["suggestions"]
    assert len(listed) == 1 and listed[0]["resource"] == PIP
    remembered = client.post(
        "/api/ledger/remember",
        json={"action": "shell.run", "resource": PIP, "repo": str(repo)},
    )
    assert remembered.status_code == 200
    assert client.get("/api/ledger/suggestions").json()["suggestions"] == []
