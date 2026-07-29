"""v55-F1: supervisor-side repo freshness — the fetch workers can never run.

A registered repo's clone was frozen at registration day (the only network
git op was the one `git clone`). refresh_repo is the supervisor manning the
`remote_git_managed_by_supervisor` station: fetch --prune, then fast-forward
the default branch so dispatch baselines and repo_state reflect upstream.
"""

from __future__ import annotations

from pathlib import Path

from skep.supervisor import SupervisorConfig

from .conftest import git, serve_client


def _default_branch(repo: Path) -> str:
    return git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip()


def _advance_upstream(repo: Path, filename: str, message: str) -> None:
    (repo / filename).write_text("value = 1\n")
    git(repo, "add", filename)
    git(repo, "commit", "-qm", message)


def test_refresh_picks_up_upstream_commits_and_branches(
    repo: Path, config: SupervisorConfig
) -> None:
    client = serve_client(config)
    assert client.post("/api/repos", json={"url": str(repo), "name": "fixture"}).status_code == 201
    branch = _default_branch(repo)
    # Upstream moves AFTER registration: a commit on the default branch plus a
    # brand-new branch — the exact field-test scenario.
    _advance_upstream(repo, "new.py", "after clone")
    git(repo, "branch", "feature-x")

    refreshed = client.post("/api/repos/fixture/refresh")
    assert refreshed.status_code == 200
    body = refreshed.json()
    assert body["fetched"] is True
    assert body["default_branch"] == branch
    assert body["behind_before"] == 1
    assert body["behind_after"] == 0
    assert body["fast_forwarded"] is True

    clone = config.home.parent / "repos" / "fixture"
    # The new upstream branch is now visible as a remote-tracking ref...
    assert git(clone, "rev-parse", "--verify", "origin/feature-x").returncode == 0
    # ...and the default branch mirrors upstream (worktrees baseline from it).
    assert (
        git(clone, "rev-parse", branch).stdout.strip()
        == git(repo, "rev-parse", branch).stdout.strip()
    )


def test_refresh_reports_diverged_default_branch_honestly(
    repo: Path, config: SupervisorConfig
) -> None:
    client = serve_client(config)
    assert client.post("/api/repos", json={"url": str(repo), "name": "fixture"}).status_code == 201
    clone = config.home.parent / "repos" / "fixture"
    git(clone, "config", "user.email", "clone@example.com")
    git(clone, "config", "user.name", "Clone")
    _advance_upstream(clone, "local.py", "operator hand-commit in the clone")
    _advance_upstream(repo, "up.py", "upstream moves too")

    body = client.post("/api/repos/fixture/refresh").json()
    # Remote refs refreshed, but the diverged default branch is never forced.
    assert body["fetched"] is True
    assert body["fast_forwarded"] is False
    assert "cannot fast-forward" in body["detail"]
    assert body["behind_before"] == 1
    assert git(clone, "log", "-1", "--format=%s").stdout.strip() == (
        "operator hand-commit in the clone"
    )


def test_refresh_without_origin_is_a_400(repo: Path, config: SupervisorConfig) -> None:
    client = serve_client(config)
    response = client.post(f"/api/repos/{repo}/refresh")
    assert response.status_code == 400
    assert "no origin remote" in response.json()["detail"]


def test_refresh_unknown_repo_is_a_404(config: SupervisorConfig) -> None:
    client = serve_client(config)
    response = client.post("/api/repos/nope/refresh")
    assert response.status_code == 404


def test_refresh_repo_is_a_carded_chat_tool() -> None:
    from skep.supervisor.serve.tools import MUTATING_TOOL_NAMES, tool_description

    assert "refresh_repo" in MUTATING_TOOL_NAMES
    description = tool_description("refresh_repo")
    assert "workers can never fetch" in description

def test_dispatch_auto_fetches_a_managed_clone(repo: Path, config: SupervisorConfig) -> None:
    """v55-F2: a branch pushed AFTER registration is dispatchable with no
    manual fetch — the 'is it on the latest code?' checklist step, automated."""
    from .conftest import wait_terminal

    client = serve_client(config)
    assert client.post("/api/repos", json={"url": str(repo), "name": "fixture"}).status_code == 201
    branch = _default_branch(repo)
    git(repo, "branch", "feature-y")
    _advance_upstream(repo, "later.py", "pushed after registration")

    task_id = client.post(
        "/api/runs",
        json={
            "repo": "fixture",
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "workspace",
            "ref": "feature-y",
        },
    ).json()["task_id"]
    run = wait_terminal(client, task_id)
    assert run["state"] == "completed"
    assert run["ref"] == "feature-y"

    # The auto-refresh also mirrored the default branch to upstream.
    clone = config.home.parent / "repos" / "fixture"
    assert (
        git(clone, "rev-parse", branch).stdout.strip()
        == git(repo, "rev-parse", branch).stdout.strip()
    )


def test_repo_state_shows_remote_branches_and_freshness(
    repo: Path, config: SupervisorConfig
) -> None:
    """v55-F5: the Queen can SEE whether the clone is on the latest remote code."""
    client = serve_client(config)
    assert client.post("/api/repos", json={"url": str(repo), "name": "fixture"}).status_code == 201
    branch = _default_branch(repo)
    git(repo, "branch", "remote-only")
    _advance_upstream(repo, "late.py", "upstream after clone")

    stale = client.get("/api/repos/fixture/state").json()
    assert all(entry["name"] != "origin/remote-only" for entry in stale["remote_branches"])

    assert client.post("/api/repos/fixture/refresh").status_code == 200
    fresh = client.get("/api/repos/fixture/state").json()
    assert any(entry["name"] == "origin/remote-only" for entry in fresh["remote_branches"])
    assert all(not entry["name"].endswith("/HEAD") for entry in fresh["remote_branches"])
    assert fresh["last_fetched"] is not None
    assert fresh["behind_origin"] == 0
    assert fresh["default_branch"] == branch


def test_repo_state_without_origin_reports_null_freshness(
    repo: Path, config: SupervisorConfig
) -> None:
    client = serve_client(config)
    state = client.get(f"/api/repos/{repo}/state").json()
    assert state["remote_branches"] == []
    assert state["behind_origin"] is None


def test_worktree_error_teaches_refresh(repo: Path, tmp_path: Path) -> None:
    import pytest

    from skep.supervisor.worktree import WorktreeError, create_worktree

    with pytest.raises(WorktreeError, match="refresh_repo"):
        create_worktree(repo, tmp_path / "wt", "task-x", "no-such-ref")


def test_dispatch_survives_an_unreachable_origin(repo: Path, config: SupervisorConfig) -> None:
    """v55-F2: offline dispatch keeps working from the clone as-is."""
    from .conftest import wait_terminal

    client = serve_client(config)
    assert client.post("/api/repos", json={"url": str(repo), "name": "fixture"}).status_code == 201
    clone = config.home.parent / "repos" / "fixture"
    git(clone, "remote", "set-url", "origin", str(clone.parent / "gone"))

    task_id = client.post(
        "/api/runs",
        json={
            "repo": "fixture",
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "workspace",
        },
    ).json()["task_id"]
    run = wait_terminal(client, task_id)
    assert run["state"] == "completed"
