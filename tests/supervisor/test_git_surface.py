"""v57: the Queen's git surface — reads free, mutations carded, remote ops
on operator credentials only. Workers stay fully denied (v19-F3/F5, v22-F2);
everything here is supervisor-side.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.serve.actions import git_log_view
from skep.supervisor.serve.settings import ConfigHolder

from .conftest import git, serve_client


def test_git_log_reads_any_ref_and_teaches_refresh(repo: Path, config: SupervisorConfig) -> None:
    client = serve_client(config)
    assert client.post("/api/repos", json={"url": str(repo), "name": "fixture"}).status_code == 201
    holder = ConfigHolder(config, RunStore(config.db_path))

    view = git_log_view(holder, "fixture")
    assert view["commits"] and "seed" in view["commits"][0]

    # A branch that exists only upstream is readable after refresh.
    git(repo, "branch", "feature-log")
    assert client.post("/api/repos/fixture/refresh").status_code == 200
    upstream = git_log_view(holder, "fixture", ref="origin/feature-log", count=5)
    assert upstream["ref"] == "origin/feature-log"
    assert upstream["commits"]

    # Unknown refs teach the refresh path instead of asserting absence.
    with pytest.raises(HTTPException) as missing:
        git_log_view(holder, "fixture", ref="never-pushed")
    assert missing.value.status_code == 400
    assert "refresh_repo" in missing.value.detail


def test_git_log_is_a_read_tool() -> None:
    from skep.supervisor.serve.tools import READ_TOOL_NAMES, tool_description

    assert "git_log" in READ_TOOL_NAMES
    assert "refresh_repo" in tool_description("git_log")


def test_git_diff_reviews_a_branch_against_default(repo: Path, config: SupervisorConfig) -> None:
    """v57-F2: 'what would this branch change?' answered read-only, capped."""
    from skep.supervisor.serve.actions import git_diff_view

    client = serve_client(config)
    assert client.post("/api/repos", json={"url": str(repo), "name": "fixture"}).status_code == 201
    holder = ConfigHolder(config, RunStore(config.db_path))
    clone = config.home.parent / "repos" / "fixture"
    git(clone, "config", "user.email", "t@e.com")
    git(clone, "config", "user.name", "T")
    default = git(clone, "symbolic-ref", "--short", "HEAD").stdout.strip()
    git(clone, "checkout", "-qb", "skep/review-me")
    (clone / "change.py").write_text("value = 42\n")
    git(clone, "add", "change.py")
    git(clone, "commit", "-qm", "the change under review")
    git(clone, "checkout", "-q", default)

    view = git_diff_view(holder, "fixture", base=default, head="skep/review-me")
    assert any("change.py" in line for line in view["stat"])
    assert "+value = 42" in view["patch"]
    assert view["truncated"] is False

    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as missing:
        git_diff_view(holder, "fixture", head="skep/never-made")
    assert missing.value.status_code == 400
    assert "refresh_repo" in missing.value.detail


def test_git_diff_is_a_read_tool() -> None:
    from skep.supervisor.serve.tools import READ_TOOL_NAMES, tool_description

    assert "git_diff" in READ_TOOL_NAMES
    assert "before" in tool_description("git_diff")


def test_list_worktrees_joins_git_state_with_runs(repo: Path, config: SupervisorConfig) -> None:
    """v57-F3: 'what is skep working on right now' — worktrees + run states."""
    from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task
    from skep.supervisor.serve.actions import list_worktrees_view
    from skep.supervisor.worktree import create_worktree

    client = serve_client(config)
    assert client.post("/api/repos", json={"url": str(repo), "name": "fixture"}).status_code == 201
    store = RunStore(config.db_path)
    holder = ConfigHolder(config, store)
    clone = config.home.parent / "repos" / "fixture"
    try:
        task = mint_task(workspace=clone, instructions="x", budget=DEFAULT_BUDGET)
        store.create_run(task, repo=clone, ref=None, execution_mode="sandbox")
        create_worktree(clone, config.worktrees_root, task.task_id, None)

        view = list_worktrees_view(holder, store, "fixture")
        by_path = {Path(w["path"]).name: w for w in view["worktrees"]}
        assert by_path[task.task_id]["run_state"] == "created"
        assert by_path[task.task_id]["task_id"] == task.task_id
        assert by_path["fixture"]["purpose"] == "main clone"
    finally:
        store.close()


def test_list_worktrees_is_a_read_tool() -> None:
    from skep.supervisor.serve.tools import READ_TOOL_NAMES, tool_description

    assert "list_worktrees" in READ_TOOL_NAMES
    assert "physically working on" in tool_description("list_worktrees")


def test_list_prs_is_a_read_tool() -> None:
    from skep.supervisor.serve.tools import READ_TOOL_NAMES, tool_description

    assert "list_prs" in READ_TOOL_NAMES
    description = tool_description("list_prs")
    assert "operator's own" in description
    assert "before open_pr" in description


def test_create_branch_carded_verb_and_refusals(repo: Path, config: SupervisorConfig) -> None:
    """v57-F5: 'start a branch for me' — slug rules, never the default,
    never an existing name (appending is landing's job)."""
    from skep.supervisor.serve.actions import create_branch

    client = serve_client(config)
    assert client.post("/api/repos", json={"url": str(repo), "name": "fixture"}).status_code == 201
    holder = ConfigHolder(config, RunStore(config.db_path))
    clone = config.home.parent / "repos" / "fixture"
    default = git(clone, "symbolic-ref", "--short", "HEAD").stdout.strip()

    made = create_branch(holder, "fixture", name="skep/feature-a")
    assert made["branch"] == "skep/feature-a" and made["from"] == default
    assert git(clone, "rev-parse", "--verify", "refs/heads/skep/feature-a").returncode == 0

    with pytest.raises(HTTPException) as existing:
        create_branch(holder, "fixture", name="skep/feature-a")
    assert existing.value.status_code == 409
    assert "landing appends" in existing.value.detail

    with pytest.raises(HTTPException) as default_refused:
        create_branch(holder, "fixture", name=default)
    assert default_refused.value.status_code == 400

    with pytest.raises(HTTPException) as bad_base:
        create_branch(holder, "fixture", name="skep/feature-b", from_ref="nope")
    assert bad_base.value.status_code == 400
    assert "refresh_repo" in bad_base.value.detail


def test_create_branch_is_a_carded_mutation() -> None:
    from skep.supervisor.serve.tools import MUTATING_TOOL_NAMES, tool_description

    assert "create_branch" in MUTATING_TOOL_NAMES
    assert "Workers can never create branches" in tool_description("create_branch")


def test_delete_branch_refuses_default_and_unmerged_work(
    repo: Path, config: SupervisorConfig
) -> None:
    """v57-F6: skep never destroys work that hasn't landed; remote deletion
    rides the operator-credential boundary like open_pr."""
    from skep.supervisor.serve.actions import delete_branch

    client = serve_client(config)
    assert client.post("/api/repos", json={"url": str(repo), "name": "fixture"}).status_code == 201
    holder = ConfigHolder(config, RunStore(config.db_path))
    clone = config.home.parent / "repos" / "fixture"
    git(clone, "config", "user.email", "t@e.com")
    git(clone, "config", "user.name", "T")
    default = git(clone, "symbolic-ref", "--short", "HEAD").stdout.strip()

    # A merged (tip-of-default) branch deletes cleanly, locally and upstream.
    git(clone, "branch", "skep/done")
    git(clone, "push", "-q", "origin", "skep/done")
    result = delete_branch(holder, "fixture", name="skep/done", remote=True)
    assert result["deleted"] is True and result["remote_deleted"] is True
    assert git(repo, "branch", "--list", "skep/done").stdout.strip() == ""

    # Unmerged work is never destroyed.
    git(clone, "checkout", "-qb", "skep/wip")
    (clone / "wip.py").write_text("x = 1\n")
    git(clone, "add", "wip.py")
    git(clone, "commit", "-qm", "unlanded work")
    git(clone, "checkout", "-q", default)
    with pytest.raises(HTTPException) as unmerged:
        delete_branch(holder, "fixture", name="skep/wip")
    assert unmerged.value.status_code == 409
    assert "never deletes" in unmerged.value.detail

    with pytest.raises(HTTPException) as default_refused:
        delete_branch(holder, "fixture", name=default)
    assert default_refused.value.status_code == 400


def test_delete_branch_is_a_carded_mutation() -> None:
    from skep.supervisor.serve.tools import MUTATING_TOOL_NAMES, tool_description

    assert "delete_branch" in MUTATING_TOOL_NAMES
    assert "never destroys" in tool_description("delete_branch")


def test_push_branch_updates_origin_but_never_the_default(
    repo: Path, config: SupervisorConfig
) -> None:
    """v57-F7: re-push a landing branch to update its PR; main moves only
    through merge_pr."""
    from skep.supervisor.serve.actions import push_branch

    client = serve_client(config)
    assert client.post("/api/repos", json={"url": str(repo), "name": "fixture"}).status_code == 201
    holder = ConfigHolder(config, RunStore(config.db_path))
    clone = config.home.parent / "repos" / "fixture"
    git(clone, "config", "user.email", "t@e.com")
    git(clone, "config", "user.name", "T")
    default = git(clone, "symbolic-ref", "--short", "HEAD").stdout.strip()
    git(clone, "checkout", "-qb", "skep/pr-branch")
    (clone / "more.py").write_text("y = 2\n")
    git(clone, "add", "more.py")
    git(clone, "commit", "-qm", "another landing")
    git(clone, "checkout", "-q", default)

    result = push_branch(holder, "fixture", name="skep/pr-branch")
    assert result["pushed"] is True
    assert "another landing" in git(repo, "log", "--oneline", "skep/pr-branch").stdout

    with pytest.raises(HTTPException) as default_refused:
        push_branch(holder, "fixture", name=default)
    assert default_refused.value.status_code == 400
    assert "merge_pr" in default_refused.value.detail

    with pytest.raises(HTTPException) as missing:
        push_branch(holder, "fixture", name="skep/never-made")
    assert missing.value.status_code == 404


def test_push_branch_is_a_carded_mutation() -> None:
    from skep.supervisor.serve.tools import MUTATING_TOOL_NAMES, tool_description

    assert "push_branch" in MUTATING_TOOL_NAMES
    description = tool_description("push_branch")
    assert "default branch is always refused" in description
    assert "force-push stays a human decision" in description


def test_repo_state_refuses_a_name_that_resolves_to_nothing(
    config: SupervisorConfig,
) -> None:
    """v58-F7 field case: 'skep-docs' resolved to a nonexistent CWD-relative
    path and repo_state returned empty state — which reads as a real repo
    with no branches and feeds confabulated reports. 404 with the teach."""
    from skep.supervisor.serve.actions import repo_state_view

    holder = ConfigHolder(config, RunStore(config.db_path))
    with pytest.raises(HTTPException) as missing:
        repo_state_view(holder, "skep-docs")
    assert missing.value.status_code == 404
    assert "register_repo" in missing.value.detail
    assert "workon" in missing.value.detail


def test_close_pr_is_a_carded_mutation() -> None:
    """v58-F1: the un-merge verb — reversible, so a standard card suffices."""
    from skep.supervisor.serve.tools import MUTATING_TOOL_NAMES, tool_description

    assert "close_pr" in MUTATING_TOOL_NAMES
    description = tool_description("close_pr")
    assert "without merging" in description
    assert "reopened" in description


def test_unregister_repo_refuses_while_runs_are_in_flight(
    repo: Path, config: SupervisorConfig
) -> None:
    """v57-F8: rmtree under a live worker's feet is never an option — the
    guard now protects the HTTP route and the carded chat verb alike."""
    from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task
    from skep.supervisor.serve.registry import remove_registered_repo, repos_root

    client = serve_client(config)
    assert client.post("/api/repos", json={"url": str(repo), "name": "fixture"}).status_code == 201
    store = RunStore(config.db_path)
    holder = ConfigHolder(config, store)
    clone = config.home.parent / "repos" / "fixture"
    try:
        task = mint_task(workspace=clone, instructions="x", budget=DEFAULT_BUDGET)
        store.create_run(task, repo=clone, ref=None, execution_mode="sandbox")

        with pytest.raises(HTTPException) as busy:
            remove_registered_repo(store, repos_root(holder), "fixture")
        assert busy.value.status_code == 409
        assert "in-flight" in busy.value.detail
        assert client.delete("/api/repos/fixture").status_code == 409  # route shares the guard

        store.transition(task.task_id, "completed", None)
        assert remove_registered_repo(store, repos_root(holder), "fixture") == {"removed": True}
        assert not clone.exists()
    finally:
        store.close()


def test_unregister_repo_is_a_carded_mutation() -> None:
    from skep.supervisor.serve.tools import MUTATING_TOOL_NAMES, tool_description

    assert "unregister_repo" in MUTATING_TOOL_NAMES
    description = tool_description("unregister_repo")
    assert "never the remote" in description
    assert "in-flight" in description


def test_push_baseline_creates_missing_remote_base_then_refuses(
    tmp_path: Path, config: SupervisorConfig
) -> None:
    """v79-F1: the empty-remote repair — creates the missing default branch on
    origin exactly once; an existing remote base is refused (I1: no existing
    remote ref is ever updated through this verb)."""
    from skep.supervisor.serve.actions import push_baseline

    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "-q", "--bare")
    clone = tmp_path / "stuck-clone"
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    git(clone, "config", "user.email", "t@e.com")
    git(clone, "config", "user.name", "T")
    git(clone, "commit", "-q", "--allow-empty", "-m", "Initialize repository for skep")

    holder = ConfigHolder(config, RunStore(config.db_path))
    result = push_baseline(holder, str(clone))

    assert result["pushed"] is True and result["created_remote_base"] is True
    assert git(origin, "for-each-ref", "refs/heads").stdout.strip()

    with pytest.raises(HTTPException) as taken:
        push_baseline(holder, str(clone))
    assert taken.value.status_code == 400
    assert "merge_pr" in taken.value.detail

    with pytest.raises(HTTPException) as missing:
        push_baseline(holder, str(clone), base="never-made")
    assert missing.value.status_code == 404


def test_push_baseline_is_a_carded_mutation() -> None:
    from skep.supervisor.serve.tools import MUTATING_TOOL_NAMES, tool_description

    assert "push_baseline" in MUTATING_TOOL_NAMES
    description = tool_description("push_baseline")
    assert "PROPOSE" in description
    assert "empty-remote repair" in description
    assert "merge_pr" in description


# ---------------------------------------------------------------------------
# v103-F2/F3 — the local merge the field test found missing, and the worker
# deny that had to land with it.
#
# The shape: thirteen skep/<task_id> branches on one repo, each one commit off
# the same baseline, and no way to combine them. The Queen kept reaching for
# `git merge` through shell.run, got the deny it was right to get, and had
# nothing to reach for instead — there was no local merge verb on ANY surface.
# ---------------------------------------------------------------------------


def _commit_on(clone: Path, branch: str, filename: str, body: str) -> None:
    """One commit on `branch` without moving the caller's checkout."""
    git(clone, "branch", branch)
    work = clone.parent / f"wt-{branch.replace('/', '-')}"
    git(clone, "worktree", "add", str(work), branch)
    (work / filename).write_text(body, encoding="utf-8")
    git(work, "add", "-A")
    git(work, "-c", "user.email=t@e", "-c", "user.name=T", "commit", "-m", f"work on {branch}")
    git(clone, "worktree", "remove", "--force", str(work))


def test_merge_branch_consolidates_task_branches_for_one_pr(
    repo: Path, config: SupervisorConfig
) -> None:
    """The field-test acceptance: several per-task branches onto one
    integration branch, so they go up as a SINGLE pull request."""
    from skep.supervisor.serve.actions import create_branch, merge_branch

    client = serve_client(config)
    assert client.post("/api/repos", json={"url": str(repo), "name": "fixture"}).status_code == 201
    holder = ConfigHolder(config, RunStore(config.db_path))
    clone = config.home.parent / "repos" / "fixture"
    default = git(clone, "symbolic-ref", "--short", "HEAD").stdout.strip()

    _commit_on(clone, "skep/task-a", "a.txt", "from a\n")
    _commit_on(clone, "skep/task-b", "b.txt", "from b\n")
    create_branch(holder, "fixture", name="skep/integration")

    merge_branch(holder, "fixture", source="skep/task-a", into="skep/integration")
    result = merge_branch(holder, "fixture", source="skep/task-b", into="skep/integration")

    assert result["into"] == "skep/integration" and result["merged"] == "skep/task-b"
    tree = git(clone, "ls-tree", "--name-only", "skep/integration").stdout.split()
    assert "a.txt" in tree and "b.txt" in tree

    # The operator's checkout never moved, and no temp worktree survived —
    # a repo somebody is standing in must not shift underneath them.
    assert git(clone, "symbolic-ref", "--short", "HEAD").stdout.strip() == default
    assert "skep-merge-" not in git(clone, "worktree", "list").stdout


def test_merge_branch_catches_a_stale_branch_up_to_the_default(
    repo: Path, config: SupervisorConfig
) -> None:
    """The other half of the field test: a task branch 2 commits behind. This
    is what the Queen was denied when it tried `git merge origin/main`."""
    from skep.supervisor.serve.actions import merge_branch

    client = serve_client(config)
    assert client.post("/api/repos", json={"url": str(repo), "name": "fixture"}).status_code == 201
    holder = ConfigHolder(config, RunStore(config.db_path))
    clone = config.home.parent / "repos" / "fixture"
    default = git(clone, "symbolic-ref", "--short", "HEAD").stdout.strip()

    _commit_on(clone, "skep/stale", "task.txt", "task work\n")
    # The default branch moves on, exactly as origin/main had.
    (clone / "moved-on.txt").write_text("later\n", encoding="utf-8")
    git(clone, "add", "-A")
    git(clone, "-c", "user.email=t@e", "-c", "user.name=T", "commit", "-m", "main moves on")

    behind = git(clone, "rev-list", "--count", f"skep/stale..{default}").stdout.strip()
    assert behind == "1"

    merge_branch(holder, "fixture", source=default, into="skep/stale")
    assert git(clone, "rev-list", "--count", f"skep/stale..{default}").stdout.strip() == "0"
    assert "task.txt" in git(clone, "ls-tree", "--name-only", "skep/stale").stdout


def test_merge_branch_never_writes_the_default_branch(repo: Path, config: SupervisorConfig) -> None:
    """I1: main moves through open_pr + merge_pr and a human review. A local
    merge into it would be a landing with no approval — the one substitution
    skep exists to prevent."""
    from skep.supervisor.serve.actions import merge_branch

    client = serve_client(config)
    assert client.post("/api/repos", json={"url": str(repo), "name": "fixture"}).status_code == 201
    holder = ConfigHolder(config, RunStore(config.db_path))
    clone = config.home.parent / "repos" / "fixture"
    default = git(clone, "symbolic-ref", "--short", "HEAD").stdout.strip()
    _commit_on(clone, "skep/task-a", "a.txt", "from a\n")

    with pytest.raises(HTTPException) as refused:
        merge_branch(holder, "fixture", source="skep/task-a", into=default)
    assert refused.value.status_code == 400
    assert "merge_pr" in refused.value.detail


def test_a_conflicting_merge_aborts_and_changes_nothing(
    repo: Path, config: SupervisorConfig
) -> None:
    """A half-merged tree is a half-applied mutation nobody asked for, which the
    operator would then have to repair by hand. It refuses cleanly, names the
    conflicting paths, and leaves the branch exactly where it was (I8)."""
    from skep.supervisor.serve.actions import merge_branch

    client = serve_client(config)
    assert client.post("/api/repos", json={"url": str(repo), "name": "fixture"}).status_code == 201
    holder = ConfigHolder(config, RunStore(config.db_path))
    clone = config.home.parent / "repos" / "fixture"

    _commit_on(clone, "skep/left", "same.txt", "left version\n")
    _commit_on(clone, "skep/right", "same.txt", "right version\n")
    before = git(clone, "rev-parse", "skep/right").stdout.strip()

    with pytest.raises(HTTPException) as conflict:
        merge_branch(holder, "fixture", source="skep/left", into="skep/right")
    assert conflict.value.status_code == 409
    assert "same.txt" in conflict.value.detail
    assert "aborted" in conflict.value.detail

    # Nothing moved, and nothing was left checked out mid-merge.
    assert git(clone, "rev-parse", "skep/right").stdout.strip() == before
    assert "skep-merge-" not in git(clone, "worktree", "list").stdout


def test_merge_branch_names_an_unknown_ref(repo: Path, config: SupervisorConfig) -> None:
    from skep.supervisor.serve.actions import merge_branch

    client = serve_client(config)
    assert client.post("/api/repos", json={"url": str(repo), "name": "fixture"}).status_code == 201
    holder = ConfigHolder(config, RunStore(config.db_path))

    with pytest.raises(HTTPException) as missing:
        merge_branch(holder, "fixture", source="no-such-ref", into="skep/nope")
    assert missing.value.status_code == 404
    assert "refresh_repo" in missing.value.detail


def test_merge_branch_is_a_carded_mutation() -> None:
    from skep.supervisor.serve.tools import MUTATING_TOOL_NAMES, tool_description

    assert "merge_branch" in MUTATING_TOOL_NAMES
    description = tool_description("merge_branch")
    # The description is load-bearing code for a small model (CLAUDE.md): it has
    # to say when to reach for this, and that a worker never can.
    assert "fallen behind" in description and "consolidate" in description
    assert "Workers can never merge" in description


# ---------------------------------------------------------------------------
# v103-F3 — the mirror image: what a WORKER may never do with git.
#
# merge/rebase/cherry-pick/revert/reset --hard were never on a deny list. They
# were only kept off the verify fast-path (is_git_mutation_argv), so a broad
# `git` allowlist entry or one remembered grant ran them.
#
# They belong with git add/commit (v22-F2) and branch switching (v20-F6), not
# in a lesser class, because the patch is a working-tree diff against the
# STARTUP BASELINE: a worker that merges another branch produces a patch
# carrying that branch's work, and it lands under THIS task's approval. The
# operator approves the task they asked for and gets somebody else's commits.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "merge", "origin/main"],
        ["git", "rebase", "main"],
        ["git", "cherry-pick", "abc1234"],
        ["git", "revert", "HEAD"],
        ["git", "reset", "--hard", "HEAD~1"],
        ["git", "-C", "/some/worktree", "merge", "main"],  # the chdir form
    ],
)
def test_a_worker_can_never_rewrite_history(argv: list[str]) -> None:
    """Denied even when explicitly allowlisted AND approved — that is what a
    hard deny means (the v19-F3 precedent)."""
    from skep.workers.runtime_plugins import ShellExecPlugin

    decision = ShellExecPlugin().decision(
        purpose="edit",
        argv=argv,
        command=" ".join(argv),
        approved_shell_commands=[argv],
        shell_allowlist=[["git"]],
    )
    assert decision.verdict == "deny"
    assert decision.reason == "capability.deny.git_history_rewrite_managed_by_supervisor"
    # I9: the deny names the sanctioned path instead of just refusing.
    detail = decision.detail or ""
    assert "merge_branch" in detail
    assert "baseline" in detail


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "status"],
        ["git", "diff", "--stat"],
        # Bare and --soft reset only move the index; staging already governs
        # that, and denying them would break legitimate plan steps for nothing.
        ["git", "reset"],
        ["git", "reset", "--soft", "HEAD~1"],
    ],
)
def test_the_deny_does_not_overreach(argv: list[str]) -> None:
    from skep.supervisor.shell_prefixes import is_history_rewrite_command

    assert not is_history_rewrite_command(argv)


def test_a_rewrite_mislabelled_verify_still_denies() -> None:
    """v20-F1's lesson: a git mutation wearing `purpose: verify` must never take
    the fast path. The deny fires before it, so the label buys nothing."""
    from skep.workers.runtime_plugins import ShellExecPlugin

    decision = ShellExecPlugin().decision(
        purpose="verify",
        argv=["git", "rebase", "main"],
        command="git rebase main",
        approved_shell_commands=[],
        shell_allowlist=[],
    )
    assert decision.verdict == "deny"


def test_the_queen_is_bound_by_the_same_list() -> None:
    """v83-F9: no chat lane may become the git-writing path workers are denied.
    The Queen's own run_shell refuses it and points at the carded verb."""
    from skep.supervisor.shell_prefixes import queen_shell_refusal

    refusal = queen_shell_refusal(["git", "merge", "origin/main"])
    assert refusal is not None and "merge_branch" in refusal
    assert queen_shell_refusal(["git", "status"]) is None


def test_a_stored_merge_grant_is_swept_not_grandfathered() -> None:
    """v84-F4's rule applied here: a store already holding a remembered
    `git merge` would keep auto-allowing it, making the new deny decorative for
    exactly the operators who hit the bug."""
    from skep.supervisor.shell_prefixes import filter_forbidden_shell_commands

    kept, removed = filter_forbidden_shell_commands(
        [["git", "merge", "origin/main"], ["uv", "run", "pytest"], ["git", "status"]]
    )
    assert kept == [["uv", "run", "pytest"], ["git", "status"]]
    assert removed == [["git", "merge", "origin/main"]]


# v109-F1: the 2026-08-03 field test ran `cd <repo> && git checkout <branch>`
# from chat with {"ok": true, "exit_code": 0} — argv[0] was `cd`, so the v83-F9
# guard never saw the git. Guards judge segments now, on both lanes.


@pytest.mark.parametrize(
    "command",
    [
        # The two store-observed bypasses (chat_actions 3160babe / a5f5a8d1),
        # branch and sed tail representative of the originals; the operator
        # home dir is anonymized for the public tree (release hygiene) —
        # the command SHAPE is what the guard must read.
        "cd /Users/operator/.skep/repos/my-portfolio && "
        "git checkout skep/019fc896-c90e-7fcc-9594-1013be153b24 && "
        "sed -i '' \"s/today/this week/\" src/content/blog/skep-is-live.mdx",
        "cd /Users/operator/.skep/repos/my-portfolio && git stash && "
        "git checkout skep/019fc896-c90e-7fcc-9594-1013be153b24 && "
        'grep -n "^## " src/content/blog/skep-is-live.mdx',
        "true; git push",
        "echo hi | git fetch",
        "sh -c 'git fetch origin'",
        "env A=1 git switch main",
        "cd /x&&git push",
    ],
)
def test_the_queen_reads_compound_command_lines(command: str) -> None:
    """v109-F1: no segment of a chat command line may be a denied git command —
    `cd` in front of a checkout stops laundering it."""
    from skep.supervisor.shell_prefixes import queen_command_line_refusal

    assert queen_command_line_refusal(command) is not None


def test_the_branch_switch_refusal_teaches_git_show() -> None:
    """I9: the Queen switched branches to READ branch content; the refusal
    names the read-only way to do that."""
    from skep.supervisor.shell_prefixes import queen_command_line_refusal

    refusal = queen_command_line_refusal("cd /x && git checkout feature")
    assert refusal is not None
    assert "git show" in refusal


@pytest.mark.parametrize(
    "command",
    [
        "cd /x && git status",
        "git show main:README.md",
        "echo use git push to publish",  # `push` as data after echo, one segment
        "echo 'a && git push'",  # quoted operator is data, not a separator
        "git checkout -- file.txt",
        "grep -rn pattern src | head -5",
    ],
)
def test_the_compound_guard_does_not_overreach(command: str) -> None:
    from skep.supervisor.shell_prefixes import queen_command_line_refusal

    assert queen_command_line_refusal(command) is None


def test_a_malformed_line_falls_to_the_card() -> None:
    """Unjudgeable from chat still cards — a human reads the raw string before
    anything runs (the lane's long-standing malformed behavior, kept)."""
    from skep.supervisor.shell_prefixes import queen_command_line_refusal

    assert queen_command_line_refusal("echo 'unbalanced") is None


def test_a_wrapped_git_command_is_denied_worker_side() -> None:
    """v109-F1: `bash -c 'git push …'` dodged every worker deny (argv[0] was
    `bash`) and the verify label would have fast-pathed it. Neither survives
    segment judgment — before the fast-path, before any grant."""
    from skep.workers.runtime_plugins import ShellExecPlugin

    argv = ["bash", "-c", "git push origin main"]
    decision = ShellExecPlugin().decision(
        purpose="verify",
        argv=argv,
        command="bash -c 'git push origin main'",
        approved_shell_commands=[argv],
        shell_allowlist=[["bash"]],
    )
    assert decision.verdict == "deny"
    assert decision.reason == "capability.deny.remote_git_managed_by_supervisor"


def test_a_compound_argv_is_denied_worker_side() -> None:
    from skep.workers.runtime_plugins import ShellExecPlugin

    decision = ShellExecPlugin().decision(
        purpose="edit",
        argv=["cd", "/x", "&&", "git", "checkout", "main"],
        command="cd /x && git checkout main",
        approved_shell_commands=[],
        shell_allowlist=[],
    )
    assert decision.verdict == "deny"
    assert decision.reason == "capability.deny.git_branch_ops_managed_by_supervisor"


def test_an_unreadable_wrapper_payload_fails_closed() -> None:
    """A payload the gate cannot statically read is denied, not waved through —
    and the deny says how to proceed (I9)."""
    from skep.workers.runtime_plugins import ShellExecPlugin

    decision = ShellExecPlugin().decision(
        purpose="edit",
        argv=["bash", "-c", "echo `git push`"],
        command="bash -c 'echo `git push`'",
        approved_shell_commands=[],
        shell_allowlist=[],
    )
    assert decision.verdict == "deny"
    assert decision.reason == "capability.deny.shell_wrapper_unparseable"
    assert "direct command" in (decision.detail or "")


def test_python_dash_c_keeps_the_verify_fast_path() -> None:
    """A python -c payload is Python, not shell — never decomposed, so the
    fast-path behavior for real verify commands is unchanged."""
    from skep.workers.runtime_plugins import ShellExecPlugin

    decision = ShellExecPlugin().decision(
        purpose="verify",
        argv=["python3", "-c", "print('git push')"],
        command="python3 -c \"print('git push')\"",
        approved_shell_commands=[],
        shell_allowlist=[],
    )
    assert decision.verdict == "allow"
    assert decision.reason == "capability.allow.shell_verify"
