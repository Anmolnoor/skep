"""v54-F4 (ADR 0034): several related runs → ONE shared branch, ONE PR.

Presentation, not governance: each run still lands through its own approval
(patch-as-approval, ADR 0002) and keeps its evidence line in the PR body.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from skep.supervisor import RunStore, SupervisorConfig, github
from skep.supervisor.dispatch import run_task
from skep.supervisor.serve.chat import SYSTEM_PROMPT
from skep.supervisor.serve.jobs import Dispatcher
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import TOOL_SPECS, execute_mutation

from .conftest import git


@pytest.fixture()
def two_file_repo(repo: Path) -> Path:
    (repo / "other.py").write_text("value = 0\n")
    git(repo, "add", "other.py")
    git(repo, "commit", "-qm", "second file")
    return repo


def _open_pr(
    config: SupervisorConfig, args: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    def fake_open_pull_request(**kwargs: Any) -> github.PullRequestResult:
        calls.append(kwargs)
        return github.PullRequestResult(True, "https://github.com/x/y/pull/9", "opened PR")

    monkeypatch.setattr(github, "open_pull_request", fake_open_pull_request)
    store = RunStore(config.db_path)
    try:
        result = execute_mutation(
            "open_pr",
            args,
            store=store,
            holder=ConfigHolder(config, store),
            runner=Dispatcher(ConfigHolder(config, store), store),
            actor="tester",
        )
    finally:
        store.close()
    return result, calls


def test_grouped_pr_lands_both_runs_on_one_branch(
    two_file_repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = run_task(two_file_repo, "Fix cards. MODE:happy", config=config)
    second = run_task(two_file_repo, "Fix more. MODE:happy FILE:other.py", config=config)
    assert first.record.state == "completed" and second.record.state == "completed"

    result, calls = _open_pr(
        config,
        {
            "task_ids": [first.record.task_id, second.record.task_id],
            "title": "Fix the confirmation cards",
        },
        monkeypatch,
    )

    branch = "skep/fix-the-confirmation-cards"
    assert result["opened"] is True and result["branch"] == branch
    assert result["task_ids"] == [first.record.task_id, second.record.task_id]
    # Both patches are commits on the ONE branch; main never moved.
    assert git(two_file_repo, "rev-parse", "--verify", f"refs/heads/{branch}").returncode == 0
    log = git(two_file_repo, "log", "--oneline", branch).stdout
    assert first.record.task_id in log and second.record.task_id in log
    assert git(two_file_repo, "show", f"{branch}:existing.py").stdout == "value = 1\n"
    assert git(two_file_repo, "show", f"{branch}:other.py").stdout == "value = 1\n"
    assert (two_file_repo / "existing.py").read_text() == "value = 0\n"
    # One PR, titled by the topic, body carries every run + the file union.
    assert calls[0]["branch"] == branch and calls[0]["title"] == "Fix the confirmation cards"
    body = str(calls[0]["body"])
    assert first.record.task_id in body and second.record.task_id in body
    assert "existing.py" in body and "other.py" in body


def test_grouped_pr_skips_patchless_members_with_an_honest_note(
    two_file_repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v60-F3: a script run (artifact only, no patch) must not fail the whole
    approved card — it is skipped and NAMED; the patch runs still land.
    Field test 2026-07-18: one grouped card burned its approval on
    'nothing to land', then the retry card auto-denied at the timeout."""
    from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task

    patched = run_task(two_file_repo, "Fix cards. MODE:happy", config=config)
    assert patched.record.state == "completed"
    store = RunStore(config.db_path)
    try:
        task = mint_task(
            workspace=two_file_repo, instructions="script only", budget=DEFAULT_BUDGET
        )
        store.create_run(
            task, repo=two_file_repo.resolve(), ref=None, execution_mode="sandbox"
        )
        store.transition(task.task_id, "completed", None)
        script_id = task.task_id
    finally:
        store.close()

    result, calls = _open_pr(
        config,
        {"task_ids": [patched.record.task_id, script_id], "title": "Docs and build output"},
        monkeypatch,
    )

    assert result["opened"] is True
    assert result["task_ids"] == [patched.record.task_id]
    assert result["skipped_no_patch"] == [script_id]
    # The skipped run contributes no evidence line to the PR body.
    assert script_id not in str(calls[0]["body"])

    # An all-patch-less group fails at once, naming every member.
    with pytest.raises(HTTPException) as exc:
        _open_pr(config, {"task_ids": [script_id]}, monkeypatch)
    assert exc.value.status_code == 409
    assert script_id in str(exc.value.detail)


def test_grouped_pr_rejects_mixed_repos(
    two_file_repo: Path,
    tmp_path: Path,
    config: SupervisorConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other_repo = tmp_path / "elsewhere"
    other_repo.mkdir()
    git(other_repo, "init", "-q")
    git(other_repo, "config", "user.email", "test@example.com")
    git(other_repo, "config", "user.name", "Test")
    (other_repo / "existing.py").write_text("value = 0\n")
    git(other_repo, "add", "existing.py")
    git(other_repo, "commit", "-qm", "seed")

    here = run_task(two_file_repo, "Fix. MODE:happy", config=config)
    there = run_task(other_repo, "Fix. MODE:happy", config=config)

    with pytest.raises(HTTPException) as exc:
        _open_pr(config, {"task_ids": [here.record.task_id, there.record.task_id]}, monkeypatch)
    assert exc.value.status_code == 400
    assert "same repo" in str(exc.value.detail)


def test_grouped_pr_with_one_task_uses_its_default_branch_name(
    two_file_repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = run_task(two_file_repo, "Fix. MODE:happy", config=config)

    result, calls = _open_pr(config, {"task_ids": [outcome.record.task_id]}, monkeypatch)

    # No title → the skep/<first_task_id> fallback, same name as a single PR.
    assert result["branch"] == f"skep/{outcome.record.task_id}"
    # And the derived single-run title (no title arg given).
    task_id = outcome.record.task_id
    assert calls[0]["title"] == github.default_pr_title("fixed and verified", task_id)


def test_grouped_pr_rejects_a_run_landed_elsewhere(
    two_file_repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    landed = run_task(two_file_repo, "Fix. MODE:happy", config=config)
    fresh = run_task(two_file_repo, "Fix more. MODE:happy FILE:other.py", config=config)

    store = RunStore(config.db_path)
    try:
        from skep.supervisor.serve import actions

        run = actions.require_run(store, landed.record.task_id)
        actions.land_run(store, run, "tester", branch="elsewhere")
    finally:
        store.close()

    with pytest.raises(HTTPException) as exc:
        _open_pr(
            config,
            {"task_ids": [landed.record.task_id, fresh.record.task_id], "title": "topic"},
            monkeypatch,
        )
    assert exc.value.status_code == 409
    assert "already landed on 'elsewhere'" in str(exc.value.detail)


def test_grouped_pr_conflict_leaves_first_commit_and_fails_cleanly(
    two_file_repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two runs patching the SAME file the same way cannot stack: the second
    apply fails, the first commit stays on the branch, the second run stays
    un-landed — the Queen reports, the human decides."""
    first = run_task(two_file_repo, "Fix. MODE:happy", config=config)
    second = run_task(two_file_repo, "Fix again. MODE:happy", config=config)

    with pytest.raises(HTTPException) as exc:
        _open_pr(
            config,
            {"task_ids": [first.record.task_id, second.record.task_id], "title": "collide"},
            monkeypatch,
        )
    assert exc.value.status_code == 409
    assert "git apply" in str(exc.value.detail)
    log = git(two_file_repo, "log", "--oneline", "skep/collide").stdout
    assert first.record.task_id in log and second.record.task_id not in log


def test_single_run_open_pr_path_is_unchanged(
    two_file_repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = run_task(two_file_repo, "Fix. MODE:happy", config=config)

    result, calls = _open_pr(config, {"task_id": outcome.record.task_id}, monkeypatch)

    assert result["branch"] == f"skep/{outcome.record.task_id}"
    assert calls[0]["branch"] == f"skep/{outcome.record.task_id}"


def test_the_queen_is_taught_when_to_group() -> None:
    assert "task_ids" in SYSTEM_PROMPT and "ONE PR" in SYSTEM_PROMPT
    spec = next(t for t in TOOL_SPECS if t["function"]["name"] == "open_pr")
    properties = spec["function"]["parameters"]["properties"]
    assert "task_ids" in properties and "title" in properties
