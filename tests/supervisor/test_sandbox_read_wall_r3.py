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
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import RunStore, sandbox
from skep.supervisor.worktree import (
    create_worktree,
    git_metadata_readable_roots,
    git_metadata_writable_roots,
)

from .conftest import git
from .test_sandbox_read_wall_r1 import _TOOLCHAIN_ENV, needs_bwrap

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


# --- 3I: the common dir is bound live, so the operator's later git shows ----


def _strict_profile(base: Path, run: Path) -> Path:
    skep_home = base / "skep"
    (skep_home / "supervisor").mkdir(parents=True, exist_ok=True)
    RunStore(skep_home / "supervisor" / "supervisor.sqlite3").close()
    return sandbox.write_profile(  # as dispatch builds a strict worker's wall
        base / "p.profile",
        workspace=run,
        extra_writable=git_metadata_writable_roots(run),
        extra_readable=git_metadata_readable_roots(run),
        skep_home=skep_home,
        read_allowlist=(),
    )


def _operator_stages_late(repo: Path) -> str:
    (repo / "late.txt").write_text("operator staged after the run started\n", encoding="utf-8")
    git(repo, "add", "late.txt")  # git writes index.lock, renames it over index
    return git(repo, "rev-parse", ":late.txt").stdout.strip()


def _operator_packs_late(repo: Path) -> str:
    blob = git(repo, "hash-object", "-w", "a.txt").stdout.strip()
    tree = subprocess.run(
        ["git", "-C", str(repo), "mktree"], input=f"100644 blob {blob}\tlate.txt\n",
        capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip
    commit = git(repo, "commit-tree", tree, "-m", "late").stdout.strip()
    git(repo, "update-ref", "refs/tags/late", commit)
    git(repo, "pack-refs", "--all")  # packed-refs appears; the loose refs go
    return commit


@needs_strict_wall
@pytest.mark.parametrize("late", [_operator_stages_late, _operator_packs_late])
def test_a_running_wall_sees_what_the_operator_stages_or_packs_after_it_started(
    late: Any, tmp_path: Path
) -> None:
    """Sol R3-1, Neon: a file bind pinned the dispatch-time ``index`` (git
    replaces it by rename) and missed a ``packed-refs`` made later, so a
    ``git prune`` in a wall started earlier deleted the new blob and commit."""
    repo, _sibling, run, oids = _shared_store_with_others_data(tmp_path)
    profile = _strict_profile(tmp_path, run)
    wall = subprocess.Popen(
        sandbox.wrap_command(
            ["/bin/sh", "-c", "touch .up; while [ ! -e .go ]; do sleep 0.05; done; git prune"],
            profile,
        ),
        cwd=run, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "TMPDIR": str(run)},
    )  # fmt: skip
    try:
        deadline = time.monotonic() + 60
        while not (run / ".up").exists():
            if wall.poll() is not None or time.monotonic() >= deadline:
                wall.kill()  # R4 (Sol): reap before reading, never hang CI
                raise AssertionError(wall.communicate(timeout=10))
            time.sleep(0.05)
        oids["late"] = late(repo)
        (run / ".go").touch()
        _out, err = wall.communicate(timeout=120)
    finally:
        if wall.poll() is None:
            wall.kill()
    assert wall.returncode == 0, err
    gone = [
        name
        for name, oid in oids.items()
        if subprocess.run(["git", "-C", str(repo), "cat-file", "-e", oid]).returncode != 0
    ]
    assert gone == [], f"git prune in a running wall deleted {gone} from the shared objects/"
    fsck = subprocess.run(["git", "-C", str(repo), "fsck"], capture_output=True, text=True)
    assert fsck.returncode == 0, fsck.stdout + fsck.stderr


@pytest.mark.parametrize("name", ["refs", "logs", "index", "packed-refs"])
def test_a_strict_run_refuses_a_linked_entry_git_s_walk_reads(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opus O-R3-1: a linked ``logs/`` (or refs, index, ...) leads nowhere in
    the wall, so ``git prune`` would walk without it: refused, never run."""
    repo, _sibling, run, _oids = _shared_store_with_others_data(tmp_path)
    git(repo, "pack-refs", "--all")
    common = (repo / ".git").resolve()
    moved = tmp_path / "elsewhere" / name
    moved.parent.mkdir()
    (common / name).rename(moved)
    (common / name).symlink_to(moved)
    probe = sandbox.SandboxAvailability(True, backend="bubblewrap")
    monkeypatch.setattr(sandbox, "require_usable", lambda backend=None: probe)
    skep_home = tmp_path / "skep"
    (skep_home / "supervisor").mkdir(parents=True)
    kwargs: dict[str, Any] = {"workspace": run, "skep_home": skep_home}
    with pytest.raises(sandbox.SandboxUnavailableError) as refused:
        sandbox.write_profile(tmp_path / "p.profile", read_allowlist=(), **kwargs)
    message = str(refused.value)
    assert f"{common / name} (a link to {moved})" in message
    assert "git prune" in message and "--no-strict-reads" in message
    sandbox.write_profile(tmp_path / "open.profile", **kwargs)  # denylist-only: reads open
    inside = common / f"{name}.real"  # a link that stays in the common dir is bound with it
    moved.rename(inside)
    (common / name).unlink()
    (common / name).symlink_to(inside.name)
    sandbox.write_profile(tmp_path / "p.profile", read_allowlist=(), **kwargs)


# --- 3I: extra paths Seatbelt would mask, or that hold /etc -----------------


@pytest.mark.parametrize(
    "path", ["/opt/homebrew/var", "/opt/homebrew/var/postgresql@16", "/usr/local/var/mysql",
             "/USR/LOCAL/VAR/redis"],
)  # fmt: skip
def test_an_extra_path_in_homebrew_s_var_is_refused_on_macos(
    path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opus O-R3-3: accepted at setup, then masked last under Seatbelt
    (ENOENT). R4 (Sol): only on macOS — bubblewrap honours these paths."""
    monkeypatch.setattr(sys, "platform", "darwin")
    problem = sandbox.extra_read_path_problem(Path(path), tmp_path / "skep")
    assert problem is not None and "Homebrew's var/" in problem, path
    assert sandbox.extra_read_path_problem(Path("/opt/homebrew/share"), tmp_path / "skep") is None
    monkeypatch.setattr(sys, "platform", "linux")
    assert sandbox.extra_read_path_problem(Path(path), tmp_path / "skep") is None


def test_a_recorded_extra_root_that_vanished_refuses_the_wall(tmp_path: Path) -> None:
    """R4 (Sol R3-2): the recorded wall names roots that existed at dispatch;
    one gone by profile time must refuse, not quietly narrow the worker's
    wall while G10 (after it returns) binds it."""
    gone = tmp_path / "data"
    with pytest.raises(sandbox.SandboxUnavailableError, match="vanished after dispatch"):
        sandbox.strict_read_roots(tmp_path / "skep", extra=(gone,))


macos = pytest.mark.skipif(sys.platform != "darwin", reason="/etc is /private/etc on macOS")


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/private", marks=macos),
        pytest.param("/PRIVATE", marks=macos),
        pytest.param("/System/Volumes/Data/private", marks=macos),
    ],
)
def test_a_dir_that_holds_etc_is_refused(path: str, tmp_path: Path) -> None:
    """Neon R3-2: /private, which holds /etc and the host's /tmp and /var."""
    problem = sandbox.extra_read_path_problem(Path(path), tmp_path / "skep")
    assert problem is not None and "/etc comes back without" in problem, path


def test_the_etc_rule_covers_every_dir_that_holds_etc() -> None:
    """Linux has one (``/``, refused as above the home first); the rule is
    the same one that refuses /private on macOS."""
    assert sandbox._etc_reserved(Path("/"))
    assert not sandbox._etc_reserved(Path("/usr/share"))


@needs_bwrap
def test_a_symlinked_git_message_refuses_the_wall(tmp_path: Path) -> None:
    """R5 (Sol): a linked COMMIT_EDITMSG/MERGE_MSG can't be masked (bwrap
    can't mount over a link), so the wall refuses rather than expose it."""
    from .test_sandbox_read_wall_r1 import _repo_with_private_state

    _repo, common, worktree = _repo_with_private_state(tmp_path)
    (common / "MERGE_MSG").unlink()
    (common / "MERGE_MSG").symlink_to(tmp_path / "elsewhere.txt")
    (tmp_path / "elsewhere.txt").write_text("in-progress message\n", encoding="utf-8")
    with pytest.raises(sandbox.SandboxUnavailableError, match="cannot mask it"):
        sandbox.write_profile(
            tmp_path / "p.profile", workspace=worktree, skep_home=tmp_path / "skep",
            read_allowlist=(),
        )  # fmt: skip


@needs_bwrap
def test_the_interpreter_s_whole_link_chain_is_inside_the_wall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CI's `uv run --with` overlay python links to an activated project
    .venv's python, which links to the base interpreter; an unbound middle
    hop dangled (`bwrap: execvp …/bin/python: No such file or directory`)."""
    real = Path(os.path.realpath(sys.executable))
    venvs: list[Path] = []
    for name, target in (("proj-venv", real), ("overlay", None)):
        env = tmp_path / name
        (env / "bin").mkdir(parents=True)
        (env / "pyvenv.cfg").write_text(f"home = {real.parent}\n", encoding="utf-8")
        (env / "bin" / "python").symlink_to(target or venvs[-1] / "bin" / "python")
        venvs.append(env)
    overlay_python = venvs[-1] / "bin" / "python"
    # like uv's overlay: skep reaches the env through a .pth (the wall's own
    # pre-exec step, `python -m skep.landlock`, runs on this interpreter)
    site = (
        venvs[-1] / "lib" / f"python{sys.version_info[0]}.{sys.version_info[1]}" / "site-packages"
    )
    site.mkdir(parents=True)
    import skep

    (site / "skep.pth").write_text(str(Path(skep.__file__).resolve().parents[1]) + "\n")
    monkeypatch.setattr(sys, "executable", str(overlay_python))
    work = tmp_path / "work"
    work.mkdir()
    profile = sandbox.write_profile(
        tmp_path / "p.profile", workspace=work, skep_home=tmp_path / "skep", read_allowlist=()
    )
    # every hop resolves inside the wall, so the overlay interpreter is there
    probe = f"test -x {overlay_python} && {overlay_python} -S -c 'print(1+1)'"
    proc = subprocess.run(
        sandbox.wrap_command(["/bin/sh", "-c", probe], profile),
        cwd=work, capture_output=True, text=True, check=False, timeout=60,
    )  # fmt: skip
    assert proc.returncode == 0 and proc.stdout.strip() == "2", proc.stderr


@needs_bwrap
def test_an_interpreter_reached_through_a_linked_dir_runs_in_the_wall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CI (Ubuntu): uv's overlay python points at
    ``~/.local/share/uv/python/cpython-3.12-…/bin/python3.12``, where
    ``cpython-3.12-…`` is uv's minor-version LINK to the patch dir. With HOME
    faked (tests do) that toolchain dir is not bound, so only the resolved
    dir was — the link itself was missing and bwrap's execvp hit ENOENT."""
    real = Path(os.path.realpath(sys.executable))
    pythons = tmp_path / "pythons"
    (pythons / "cpython-3.12.99" / "bin").mkdir(parents=True)
    (pythons / "cpython-3.12.99" / "bin" / "python3.12").symlink_to(real)
    (pythons / "cpython-3.12").symlink_to(pythons / "cpython-3.12.99")
    overlay = tmp_path / "overlay"
    (overlay / "bin").mkdir(parents=True)
    (overlay / "pyvenv.cfg").write_text(f"home = {real.parent}\n", encoding="utf-8")
    overlay_python = overlay / "bin" / "python"
    overlay_python.symlink_to(pythons / "cpython-3.12" / "bin" / "python3.12")
    site = overlay / "lib" / f"python{sys.version_info[0]}.{sys.version_info[1]}" / "site-packages"
    site.mkdir(parents=True)
    import skep

    (site / "skep.pth").write_text(str(Path(skep.__file__).resolve().parents[1]) + "\n")
    monkeypatch.setattr(sys, "executable", str(overlay_python))
    # as on CI: the link's target (the patch dir) is the base prefix, already
    # bound; the real stdlib stays bound through exec_prefix
    monkeypatch.setattr(sys, "exec_prefix", sys.base_prefix)
    monkeypatch.setattr(sys, "base_prefix", str(pythons / "cpython-3.12.99"))
    work = tmp_path / "work"
    work.mkdir()
    profile = sandbox.write_profile(
        tmp_path / "p.profile", workspace=work, skep_home=tmp_path / "skep", read_allowlist=()
    )
    probe = f"{overlay_python} -S -c 'print(1+1)'"
    proc = subprocess.run(
        sandbox.wrap_command(["/bin/sh", "-c", probe], profile),
        cwd=work, capture_output=True, text=True, check=False, timeout=60,
    )  # fmt: skip
    assert proc.returncode == 0 and proc.stdout.strip() == "2", proc.stderr


def test_a_linked_dir_on_the_interpreter_s_path_never_opens_its_whole_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W2: the link is recreated only when its target is already bound — a
    linked dir whose target holds more than the interpreter stays closed."""
    real = Path(os.path.realpath(sys.executable))
    broad = tmp_path / "broad"
    (broad / "bin").mkdir(parents=True)
    (broad / "bin" / "python3.12").symlink_to(real)
    (broad / "notes.txt").write_text("private\n", encoding="utf-8")
    (tmp_path / "linked").symlink_to(broad)
    overlay = tmp_path / "overlay"
    (overlay / "bin").mkdir(parents=True)
    (overlay / "pyvenv.cfg").write_text(f"home = {real.parent}\n", encoding="utf-8")
    (overlay / "bin" / "python").symlink_to(tmp_path / "linked" / "bin" / "python3.12")
    monkeypatch.setattr(sys, "executable", str(overlay / "bin" / "python"))
    roots = sandbox.strict_read_roots(tmp_path / "skep")
    # nothing approved covers the link's target, so neither it nor the hop is bound
    assert broad.resolve() not in roots and (broad / "bin").resolve() not in roots


def test_a_linked_interpreter_dir_never_opens_its_whole_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sol C-1 (W2): a hop that is itself a linked dir is no bind root of its
    own — its target (here a dir holding more than the interpreter) stays closed."""
    real = Path(os.path.realpath(sys.executable))
    broad = tmp_path / "broad"
    broad.mkdir()
    (broad / "python3.12").symlink_to(real)
    (broad / "notes.txt").write_text("private\n", encoding="utf-8")
    (tmp_path / "linkbin").symlink_to(broad)
    overlay = tmp_path / "overlay"
    (overlay / "bin").mkdir(parents=True)
    (overlay / "pyvenv.cfg").write_text(f"home = {real.parent}\n", encoding="utf-8")
    (overlay / "bin" / "python").symlink_to(tmp_path / "linkbin" / "python3.12")
    monkeypatch.setattr(sys, "executable", str(overlay / "bin" / "python"))
    assert broad.resolve() not in sandbox.strict_read_roots(tmp_path / "skep")


def test_a_plain_interpreter_hop_binds_the_file_not_its_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sol C2-1: a hop outside a verified env is the interpreter file alone —
    its bin dir's siblings stay closed."""
    real = Path(os.path.realpath(sys.executable))
    plain = tmp_path / "plainbin"
    plain.mkdir()
    (plain / "notes.txt").write_text("private\n", encoding="utf-8")
    shim = plain / "python3"  # a real file, not a link: the chain ends here
    shim.write_text("#!/bin/sh\n", encoding="utf-8")
    overlay = tmp_path / "overlay"
    (overlay / "bin").mkdir(parents=True)
    (overlay / "pyvenv.cfg").write_text(f"home = {real.parent}\n", encoding="utf-8")
    (overlay / "bin" / "python").symlink_to(shim)
    monkeypatch.setattr(sys, "executable", str(overlay / "bin" / "python"))
    roots = sandbox.strict_read_roots(tmp_path / "skep")
    assert shim.resolve() in roots and plain.resolve() not in roots


def test_interpreter_links_are_aliases_never_bind_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sol C2-2: uv's minor-version dir link is recreated as a link only — it
    never enters the bind roots, so a retarget cannot add an unchecked root."""
    real = Path(os.path.realpath(sys.executable))
    pythons = tmp_path / "pythons"
    (pythons / "cpython-3.12.99" / "bin").mkdir(parents=True)
    (pythons / "cpython-3.12.99" / "bin" / "python3.12").symlink_to(real)
    (pythons / "cpython-3.12").symlink_to(pythons / "cpython-3.12.99")
    overlay = tmp_path / "overlay"
    (overlay / "bin").mkdir(parents=True)
    (overlay / "pyvenv.cfg").write_text(f"home = {real.parent}\n", encoding="utf-8")
    (overlay / "bin" / "python").symlink_to(pythons / "cpython-3.12" / "bin" / "python3.12")
    monkeypatch.setattr(sys, "executable", str(overlay / "bin" / "python"))
    candidates = sandbox._strict_candidates(tmp_path / "skep", worker_llm_key=False, extra=())
    assert pythons / "cpython-3.12" not in candidates
    assert sandbox._interpreter_links([pythons / "cpython-3.12.99"]) == [pythons / "cpython-3.12"]
    assert sandbox._interpreter_links([tmp_path / "elsewhere"]) == []


def test_a_dotdot_after_a_linked_dir_never_binds_the_lexical_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sol C3-1: ``linkdir/../decoy/python3`` walks the link first, so the
    file really run is ``deep/decoy/python3``; collapsing ``..`` first named
    an unrelated ``decoy/python3`` that the wall then bound."""
    real = Path(os.path.realpath(sys.executable))
    (tmp_path / "deep" / "sub").mkdir(parents=True)
    (tmp_path / "deep" / "decoy").mkdir()
    (tmp_path / "deep" / "decoy" / "python3").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "decoy").mkdir()
    (tmp_path / "decoy" / "python3").write_text("unrelated\n", encoding="utf-8")
    (tmp_path / "linkdir").symlink_to(tmp_path / "deep" / "sub")
    overlay = tmp_path / "overlay"
    (overlay / "bin").mkdir(parents=True)
    (overlay / "pyvenv.cfg").write_text(f"home = {real.parent}\n", encoding="utf-8")
    (overlay / "bin" / "python").symlink_to(tmp_path / "linkdir" / ".." / "decoy" / "python3")
    monkeypatch.setattr(sys, "executable", str(overlay / "bin" / "python"))
    roots = sandbox.strict_read_roots(tmp_path / "skep")
    assert (tmp_path / "decoy" / "python3").resolve() not in roots
    assert all(".." not in link.parts for link in sandbox._interpreter_links([tmp_path]))


@pytest.mark.skipif(not Path("/bin").is_symlink(), reason="needs a merged-/usr host")
def test_an_interpreter_through_bin_adds_no_second_bin_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sol C3-2: the strict root already links /bin (relative); an absolute
    second one makes bwrap refuse the wall."""
    monkeypatch.setattr(sys, "executable", "/bin/sh")
    links = sandbox._interpreter_links([Path("/usr")])
    assert all(not link.is_relative_to(root) for link in links for root in ("/bin", "/usr"))


@pytest.mark.skipif(not Path("/usr/bin/python3").is_symlink(), reason="needs a /usr link")
def test_no_alias_lands_under_a_system_mount() -> None:
    """Sol C4-1: the /usr mount already holds a link like /usr/local/bin/uv;
    a second, absolute one there makes bwrap refuse the wall."""
    assert sandbox._alias_links([Path("/usr/bin/python3")], []) == []


def test_a_dotdot_path_entry_binds_the_uv_really_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sol C5-1: PATH ``linkdir/../bin`` finds ``deep/bin/uv`` (the link is
    walked first); collapsing ``..`` named ``bin/uv`` — here an unrelated dir."""
    (tmp_path / "deep" / "sub").mkdir(parents=True)
    (tmp_path / "deep" / "bin").mkdir()
    uv = tmp_path / "deep" / "bin" / "uv"
    uv.write_text("#!/bin/sh\n", encoding="utf-8")
    uv.chmod(0o755)
    (tmp_path / "bin" / "uv").mkdir(parents=True)  # the decoy
    (tmp_path / "bin" / "uv" / "notes.txt").write_text("private\n", encoding="utf-8")
    (tmp_path / "linkdir").symlink_to(tmp_path / "deep" / "sub")
    monkeypatch.setenv("PATH", str(tmp_path / "linkdir" / ".." / "bin"))
    roots = sandbox.strict_read_roots(tmp_path / "skep")
    assert uv.resolve() in roots
    assert (tmp_path / "bin" / "uv").resolve() not in roots


def test_a_linked_uv_binds_its_checked_file_and_aliases_the_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sol C6-1: ~/.local/bin/uv linked to a versioned file — the bind root is
    the file that passed the check, never the link (a retarget during the
    build could not admit a dir); the link name comes back as an alias only."""
    (tmp_path / "versions").mkdir()
    real_uv = tmp_path / "versions" / "uv-1.0"
    real_uv.write_text("#!/bin/sh\n", encoding="utf-8")
    real_uv.chmod(0o755)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "uv").symlink_to(real_uv)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    candidates = sandbox._strict_candidates(tmp_path / "skep", worker_llm_key=False, extra=())
    assert real_uv.resolve() in candidates and tmp_path / "bin" / "uv" not in candidates
    assert sandbox._found_link(str(tmp_path / "bin" / "uv")) == [tmp_path / "bin" / "uv"]
    assert sandbox._found_link(str(real_uv)) == []
