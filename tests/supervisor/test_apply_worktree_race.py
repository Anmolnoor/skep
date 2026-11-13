"""Landing vs. worktree teardown: the approval-desync race, made deterministic.

CI (PR #50, macOS): ``/approve`` landed right as the run's post-completion
teardown (G10 re-verify tree, the run's own tree, prune) removed the repo's
last sibling worktree. git rmdirs an empty ``.git/worktrees`` on remove/prune,
so the landing's ``git worktree add -b`` died between creating that dir and
its own leaf ("could not create directory of '.git/worktrees/worktree'") —
AFTER git had already created ``skep/<task_id>``. The approval was honestly
denied (v81-F2), but the left-behind branch read as "landed".
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import TracebackType

import pytest

from skep.supervisor import apply as apply_mod
from skep.supervisor import worktree as worktree_mod
from skep.supervisor.apply import apply_patch_on_branch
from skep.supervisor.worktree import create_worktree, remove_worktree

from .conftest import git

PATCH = (
    "diff --git a/notes.txt b/notes.txt\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/notes.txt\n"
    "@@ -0,0 +1 @@\n"
    "+landed\n"
)


def _patch(tmp_path: Path) -> Path:
    path = tmp_path / "task.patch"
    path.write_text(PATCH, encoding="utf-8")
    return path


def test_landing_never_builds_its_worktree_while_a_sibling_is_torn_down(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pause a real sibling teardown mid-``git worktree remove``, then land.

    The lander must wait for the teardown instead of running ``git worktree
    add`` beside it. No sleeps: the lock is spied, so the test knows the
    lander either reached ``worktree add`` (the bug) or is waiting on the lock
    (the fix) before it lets the teardown go.
    """
    sibling = create_worktree(repo, tmp_path / "trees", "sibling")
    patch = _patch(tmp_path)

    tearing_down = threading.Event()
    release = threading.Event()
    lander_progressed = threading.Event()  # reached `worktree add` OR the lock
    overlaps: list[str] = []
    lander: threading.Thread | None = None

    class SpyLock:
        def __init__(self) -> None:
            self._lock = threading.RLock()

        def __enter__(self) -> SpyLock:
            if threading.current_thread() is lander:
                lander_progressed.set()
            self._lock.acquire()
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> None:
            self._lock.release()

    spy = SpyLock()
    monkeypatch.setattr(worktree_mod, "TREE_LOCK", spy)
    monkeypatch.setattr(apply_mod, "TREE_LOCK", spy, raising=False)

    real_worktree_git = worktree_mod._git
    real_apply_git = apply_mod._git

    def paused_teardown_git(cwd: Path, *args: str) -> object:
        if args[:2] == ("worktree", "remove"):
            tearing_down.set()
            assert release.wait(timeout=30), "test never released the teardown"
        return real_worktree_git(cwd, *args)

    def watched_apply_git(cwd: Path, *args: str) -> object:
        if args[:2] == ("worktree", "add"):
            if tearing_down.is_set() and not release.is_set():
                overlaps.append(f"landing ran `git {' '.join(args[:3])}` mid-teardown")
            lander_progressed.set()
        return real_apply_git(cwd, *args)

    monkeypatch.setattr(worktree_mod, "_git", paused_teardown_git)
    monkeypatch.setattr(apply_mod, "_git", watched_apply_git)

    results: list[str | None] = []
    sweeper = threading.Thread(target=remove_worktree, args=(repo, sibling))
    lander = threading.Thread(
        target=lambda: results.append(
            apply_patch_on_branch(repo, "skep/t1", patch, task_id="t1", actor="tester")
        )
    )
    sweeper.start()
    assert tearing_down.wait(timeout=30)
    lander.start()
    assert lander_progressed.wait(timeout=30)
    release.set()
    sweeper.join(timeout=30)
    lander.join(timeout=30)

    assert overlaps == []
    assert results == [None]
    assert "landed" in git(repo, "show", "skep/t1:notes.txt").stdout


def test_a_failed_landing_leaves_no_branch_behind(repo: Path, tmp_path: Path) -> None:
    """git creates the ``-b`` branch before it builds the worktree, so a failed
    add (here: ``.git/worktrees`` is unusable) used to leave ``skep/<task_id>``
    at the baseline — reading as landed, and refusing the retry with "was this
    patch already applied?". A failed landing must change no refs."""
    patch = _patch(tmp_path)
    blocker = repo / ".git" / "worktrees"
    blocker.write_text("not a directory\n", encoding="utf-8")

    error = apply_patch_on_branch(repo, "skep/t1", patch, task_id="t1", actor="tester")

    assert error is not None and "cannot create apply worktree" in error
    assert git(repo, "branch", "--list", "skep/t1").stdout == ""

    blocker.unlink()  # the repo recovers: the retry lands, not "already applied"
    assert apply_patch_on_branch(repo, "skep/t1", patch, task_id="t1", actor="tester") is None
    assert "landed" in git(repo, "show", "skep/t1:notes.txt").stdout
