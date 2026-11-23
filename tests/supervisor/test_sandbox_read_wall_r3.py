"""ADR 0068, Phase 03 review round 3: git in the wall cannot prune what it cannot see.

3H: the wall hid the operator's stash and index and the sibling runs' git
dirs, but the shared ``objects/`` stays writable. So ``git prune`` (loose
objects) or ``git repack -a -d`` (packed ones) in a run's worktree found the
stash, the operator's staged blob and a sibling run's staged blob and HEAD
commit unreachable, and deleted them from the store every checkout shares.
(``git gc`` itself dies first, on its lock in the read-only common dir.)
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.supervisor import RunStore, sandbox
from skep.supervisor.worktree import (
    create_worktree,
    git_metadata_readable_roots,
    git_metadata_writable_roots,
)

from .conftest import git
from .test_sandbox_read_wall_r1 import _TOOLCHAIN_ENV

needs_strict_wall = pytest.mark.skipif(
    sandbox.availability().backend not in ("bubblewrap", "seatbelt"),
    reason="real sandbox proof (runs where bwrap or sandbox-exec is the backend)",
)


@pytest.fixture(autouse=True)
def _clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from skep.supervisor import secret_floor

    monkeypatch.setattr(secret_floor, "HOME_DIRS", (f"{tmp_path}/no-homes/*",))
    for name in (*sandbox._SOCKET_ENV, *_TOOLCHAIN_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    sandbox.availability.cache_clear()
    yield
    sandbox.availability.cache_clear()


def _shared_store_with_others_data(base: Path) -> tuple[Path, Path, Path, dict[str, str]]:
    """A repo whose shared objects/ holds the operator's two stashes and staged
    blob, and a sibling run's staged blob and HEAD commit; and this run's worktree."""
    repo = base / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-qm", "a")
    for n in ("one", "two"):  # stash@{1} lives only in the stash's reflog
        (repo / "a.txt").write_text(f"operator stash {n}\n", encoding="utf-8")
        git(repo, "stash", "-q")
    (repo / "b.txt").write_text("operator staged\n", encoding="utf-8")
    git(repo, "add", "b.txt")
    trees = base / "trees"
    sibling = create_worktree(repo, trees, "sibling")
    (sibling / "d.txt").write_text("sibling committed\n", encoding="utf-8")
    git(sibling, "add", "d.txt")
    git(sibling, "commit", "-qm", "d")
    (sibling / "c.txt").write_text("sibling staged\n", encoding="utf-8")
    git(sibling, "add", "c.txt")
    run = create_worktree(repo, trees, "run")
    oids = {
        "stash@{0}": git(repo, "rev-parse", "stash@{0}").stdout.strip(),
        "stash@{1}": git(repo, "rev-parse", "stash@{1}").stdout.strip(),
        "operator staged": git(repo, "rev-parse", ":b.txt").stdout.strip(),
        "sibling staged": git(sibling, "rev-parse", ":c.txt").stdout.strip(),
        "sibling HEAD": git(sibling, "rev-parse", "HEAD").stdout.strip(),
    }
    return repo, sibling, run, oids


@needs_strict_wall
@pytest.mark.parametrize("argv", [["git", "prune"], ["git", "repack", "-a", "-d", "-q"]])
def test_git_prune_in_the_wall_keeps_the_stash_and_a_sibling_run_s_objects(
    argv: list[str], tmp_path: Path
) -> None:
    repo, sibling, run, oids = _shared_store_with_others_data(tmp_path)
    if "repack" in argv:  # the host packs everything first; the wall repacks
        git(repo, "repack", "-a", "-d", "-q")
    skep_home = tmp_path / "skep"
    (skep_home / "supervisor").mkdir(parents=True)
    RunStore(skep_home / "supervisor" / "supervisor.sqlite3").close()
    profile = sandbox.write_profile(  # as dispatch builds a strict worker's wall
        tmp_path / "p.profile",
        workspace=run,
        extra_writable=git_metadata_writable_roots(run),
        extra_readable=git_metadata_readable_roots(run),
        skep_home=skep_home,
        read_allowlist=(),
    )
    proc = subprocess.run(
        sandbox.wrap_command(argv, profile), cwd=run, capture_output=True, text=True,
        check=False, timeout=120,
        env={"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "TMPDIR": str(run)},
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    gone = [
        name
        for name, oid in oids.items()
        if subprocess.run(["git", "-C", str(repo), "cat-file", "-e", oid]).returncode != 0
    ]
    assert gone == [], f"{argv} deleted {gone} from the shared objects/"
    for n, stash in (("two", "stash@{0}"), ("one", "stash@{1}")):
        assert f"+operator stash {n}" in git(repo, "stash", "show", "-p", stash).stdout
    assert git(sibling, "show", ":c.txt").stdout == "sibling staged\n"
    assert git(sibling, "show", "HEAD:d.txt").stdout == "sibling committed\n"
    log = subprocess.run(  # the run's own git still works; the sibling's metadata holds
        sandbox.wrap_command(
            ["/bin/sh", "-c", "git worktree prune; git status -s && git diff && git log --all"],
            profile,
        ),
        cwd=run, capture_output=True, text=True, check=False, timeout=60,
        env={"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "TMPDIR": str(run)},
    )  # fmt: skip
    assert log.returncode == 0, log.stderr
    assert "failed to delete" in log.stderr  # it sees the sibling, cannot remove it
    assert str(sibling) in git(repo, "worktree", "list", "--porcelain").stdout
