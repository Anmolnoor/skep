"""ADR 0068, Phase 03 review round 2: the macOS half of the read wall.

M1 /etc and the home are judged by every spelling macOS opens them by (the
/private/etc target, APFS case, the data-volume firmlink). M3 git lists the
shared refs/ under Seatbelt. M4 /System/Volumes, M8 Homebrew's var and M10
the xcode-select link. M9 no Seatbelt run starts a process outside its wall
through LaunchServices, launchd or Apple events, or asks Spotlight for names.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.supervisor import RunStore, sandbox
from skep.supervisor.worktree import git_metadata_readable_roots, git_metadata_writable_roots

from .conftest import git
from .test_sandbox_read_wall_r1 import _TOOLCHAIN_ENV, _repo_with_private_state

macos = pytest.mark.skipif(sys.platform != "darwin", reason="a macOS spelling")
needs_seatbelt = pytest.mark.skipif(
    sys.platform != "darwin" or sandbox.availability().backend != "seatbelt",
    reason="real sandbox-exec proof",
)
needs_strict_wall = pytest.mark.skipif(
    sandbox.availability().backend not in ("bubblewrap", "seatbelt"),
    reason="real sandbox proof (runs where bwrap or sandbox-exec is the backend)",
)

# {path: "read" | the error's name}; a directory counts as read when listable,
# a link as read when its target can be read by name (readlink).
_PROBE = """
import json, os, sys
seen = {}
for path in sys.argv[1:]:
    try:
        if os.path.islink(path):
            os.readlink(path)
        else:
            os.listdir(path) if os.path.isdir(path) else open(path, 'rb').read()
        seen[path] = "read"
    except OSError as exc:
        seen[path] = type(exc).__name__
print(json.dumps(seen))
"""


@pytest.fixture(autouse=True)
def _clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from skep.supervisor import secret_floor

    monkeypatch.setattr(secret_floor, "HOME_DIRS", (f"{tmp_path}/no-homes/*",))
    for name in (*sandbox._SOCKET_ENV, *_TOOLCHAIN_ENV):
        monkeypatch.delenv(name, raising=False)
    sandbox.availability.cache_clear()
    yield
    sandbox.availability.cache_clear()


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "fakehome"
    for rel in ("Downloads/canary-downloads.txt", ".ssh/id_rsa"):
        path = home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"CANARY {rel}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture()
def skep_home(tmp_path: Path) -> Path:
    home = tmp_path / "skep"
    (home / "supervisor").mkdir(parents=True)
    RunStore(home / "supervisor" / "supervisor.sqlite3").close()
    return home


def _run(profile: Path, argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        sandbox.wrap_command(argv, profile), cwd=cwd, capture_output=True, text=True,
        check=False, timeout=60,
        env={"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "TMPDIR": str(cwd)},
    )  # fmt: skip


def _probe(profile: Path, paths: list[Path], cwd: Path) -> dict[str, str]:
    proc = _run(profile, [sys.executable, "-c", _PROBE, *map(str, paths)], cwd)
    assert proc.returncode == 0, proc.stderr
    seen: dict[str, str] = json.loads(proc.stdout)
    return seen


# --- M1: /etc and the home by every spelling macOS opens them by ------------


@pytest.mark.parametrize(
    "path",
    [
        "/etc",
        "/etc/ssh",
        "/etc/sudoers",
        "/etc/sudoers.d",
        "/etc/master.passwd",
        "/etc/kcpassword",
        "/ETC/SSH",
        pytest.param("/private/etc", marks=macos),
        pytest.param("/private/etc/master.passwd", marks=macos),
        pytest.param("/private/etc/kcpassword", marks=macos),
        pytest.param("/private/etc/sudoers.d/x", marks=macos),
        pytest.param("/System/Volumes/Data/private/etc/sudoers", marks=macos),
    ],
)
def test_etc_s_reserved_entries_are_refused_by_every_spelling(
    path: str, skep_home: Path, tmp_path: Path
) -> None:
    link = tmp_path / "link"
    link.symlink_to(path)
    for spelling in (Path(path), link):
        problem = sandbox.extra_read_path_problem(spelling, skep_home)
        assert problem is not None and "/etc comes back without" in problem, spelling
        assert "master.passwd" in problem and "shadow" in problem


@macos
def test_detected_roots_on_private_etc_are_dropped(
    fake_home: Path, skep_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NVM_DIR", "/etc")
    monkeypatch.setenv("UV_TOOL_DIR", "/private/etc")
    monkeypatch.setenv("PYENV_ROOT", "/private/etc/ssh")
    roots = sandbox.strict_read_roots(skep_home)
    assert not any(root.is_relative_to("/private/etc") for root in roots), roots


def _spellings(path: Path) -> list[Path]:
    """``path`` upper-cased and, on macOS, by its data-volume firmlink."""
    real = path.resolve()
    spellings = [Path(str(real).upper())]
    if sys.platform == "darwin":
        spellings.append(Path("/System/Volumes/Data", *real.parts[1:]))
    return spellings


def test_a_case_or_firmlink_spelling_of_the_home_or_skep_home_is_refused(
    fake_home: Path, skep_home: Path
) -> None:
    for spelling in _spellings(fake_home):
        problem = sandbox.extra_read_path_problem(spelling, skep_home)
        assert problem is not None and "at or above the home" in problem, spelling
    for spelling in [*_spellings(skep_home), *_spellings(skep_home / "supervisor")]:
        problem = sandbox.extra_read_path_problem(spelling, skep_home)
        assert problem is not None and "skep home" in problem, spelling
    assert sandbox.extra_read_path_problem(fake_home / "datasets", skep_home) is None


def test_a_detected_root_spelling_the_home_another_way_is_dropped(
    fake_home: Path, skep_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for spelling in _spellings(fake_home):
        monkeypatch.setenv("NVM_DIR", str(spelling))
        roots = sandbox.strict_read_roots(skep_home)
        assert not any(str(root).casefold().endswith("fakehome") for root in roots), roots


@needs_seatbelt
def test_an_upper_case_home_never_reopens_downloads_for_real(
    fake_home: Path, skep_home: Path, tmp_path: Path
) -> None:
    """Seatbelt matches APFS paths without case: before M1 this extra path
    passed the check and its allow reopened the whole home."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(sandbox.SandboxUnavailableError, match="at or above the home"):
        sandbox.write_profile(
            tmp_path / "p.sb", workspace=workspace, skep_home=skep_home,
            read_allowlist=(Path(str(fake_home.resolve()).upper()),),
        )  # fmt: skip
    assert not (tmp_path / "p.sb").exists()


# --- M3: git enumerates the shared refs (3H: the stash too) ----------------


@needs_strict_wall
def test_git_lists_loose_branches_tags_and_the_stash(
    fake_home: Path, skep_home: Path, tmp_path: Path
) -> None:
    repo, common, worktree = _repo_with_private_state(tmp_path)
    git(repo, "branch", "feature")
    git(repo, "tag", "v1")
    assert (common / "refs" / "heads" / "feature").is_file()  # loose, not packed
    profile = sandbox.write_profile(
        tmp_path / "p.profile",
        workspace=worktree,
        extra_writable=git_metadata_writable_roots(worktree),
        extra_readable=git_metadata_readable_roots(worktree),
        skep_home=skep_home,
        read_allowlist=(),
    )
    proc = _run(profile, ["git", "for-each-ref", "--format=%(refname)"], worktree)
    assert proc.returncode == 0, proc.stderr
    refs = proc.stdout.split()
    assert {"refs/heads/feature", "refs/tags/v1", "refs/stash"} <= set(refs), proc.stdout
    # 3H: no broken (unreadable) refs/stash any more. Apple's /usr/bin/git
    # shim (xcrun) may warn that it cannot write its cache in the host's
    # temp dir — the wall denies that on purpose (a run must never seed a
    # host tool cache); git itself still answers.
    noise = [line for line in proc.stderr.splitlines() if "couldn't create cache file" not in line]
    assert noise == [], proc.stderr
    branches = _run(profile, ["git", "branch", "--format=%(refname:short)"], worktree)
    assert "feature" in branches.stdout.split(), branches.stdout + branches.stderr
    log = _run(profile, ["git", "log", "--all", "--oneline"], worktree)  # stopped on it before
    assert log.returncode == 0 and "WIP on" in log.stdout, log.stderr


# --- M4, M8, M10: the system half of the Seatbelt wall ----------------------


@pytest.fixture()
def homebrew_var_canaries() -> Iterator[list[Path]]:
    """A canary in each Homebrew var dir this host has and lets us write
    (removed after; the operator's databases are never touched)."""
    made: list[Path] = []
    for var in ("/opt/homebrew/var", "/usr/local/var"):
        if os.path.isdir(var) and os.access(var, os.W_OK):
            fd, name = tempfile.mkstemp(prefix="skep-canary-", dir=var)
            os.write(fd, b"CANARY homebrew var\n")
            os.close(fd)
            made.append(Path(name))
    try:
        yield made
    finally:
        for path in made:
            path.unlink(missing_ok=True)


@needs_seatbelt
def test_a_strict_seatbelt_run_reads_no_system_volume_or_homebrew_data(
    skep_home: Path, tmp_path: Path, homebrew_var_canaries: list[Path]
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    profile = sandbox.write_profile(
        tmp_path / "p.sb", workspace=workspace, skep_home=skep_home, read_allowlist=()
    )
    volumes = [Path("/System/Volumes", name) for name in ("Preboot", "VM", "Update")]
    data = Path("/System/Volumes/Data")
    # the data volume's own entries no firmlink maps back to the root
    unmapped = [data / name for name in ("mnt", "sw", ".PreviousSystemInformation")]
    denied = [p for p in [*volumes, data, *unmapped] if os.path.lexists(p)]
    seen = _probe(profile, [*denied, *homebrew_var_canaries], workspace)
    assert "read" not in seen.values(), seen
    links = [Path(p) for p in sandbox._XCODE_SELECT_LINKS if os.path.islink(p)]
    if links:  # M10: xcrun reads the selection by the link itself
        assert set(_probe(profile, links, workspace).values()) == {"read"}


# --- M9: nothing starts outside the wall ------------------------------------


def _probe_app(where: Path, canary: Path, out: Path) -> Path:
    """A background app that copies ``canary`` into ``out`` when launched."""
    app = where / "SkepProbe.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    exe = app / "Contents" / "MacOS" / "SkepProbe"
    exe.write_text(f"#!/bin/sh\n/bin/cat '{canary}' > '{out}'\n", encoding="utf-8")
    exe.chmod(0o755)
    (app / "Contents" / "Info.plist").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>'
        "<key>CFBundleExecutable</key><string>SkepProbe</string>"
        f"<key>CFBundleIdentifier</key><string>com.skep.probe.{uuid.uuid4().hex}</string>"
        "<key>CFBundlePackageType</key><string>APPL</string>"
        "<key>LSBackgroundOnly</key><true/></dict></plist>\n",
        encoding="utf-8",
    )
    return app


def _settle(path: Path) -> str:
    """What ``path`` holds once a launched job had time to write it."""
    for _ in range(20):
        if path.exists() and path.stat().st_size:
            break
        subprocess.run(["/bin/sleep", "0.2"], check=False)
    return path.read_text(encoding="utf-8") if path.exists() else ""


@needs_seatbelt
@pytest.mark.parametrize("strict", [True, False], ids=["strict", "denylist"])
def test_no_seatbelt_run_starts_a_process_outside_its_wall(
    strict: bool, fake_home: Path, skep_home: Path, tmp_path: Path
) -> None:
    """`open` hands LaunchServices an app that runs unsandboxed — it read a
    fake-HOME canary into the worktree under both profiles before M9."""
    canary = fake_home / "Downloads" / "canary-downloads.txt"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    profile = sandbox.write_profile(
        tmp_path / "p.sb", workspace=workspace, skep_home=skep_home,
        read_allowlist=() if strict else None,
    )  # fmt: skip
    opened = workspace / "opened.txt"
    app = _probe_app(workspace, canary, opened)
    proc = _run(profile, ["/usr/bin/open", "-g", "-j", str(app)], workspace)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "CANARY" not in _settle(opened)
    launch = f'tell application "{app}" to launch'
    script = _run(profile, ["/usr/bin/osascript", "-e", launch], workspace)
    assert script.returncode != 0 and "CANARY" not in _settle(opened)

    label = f"com.skep.probe.{uuid.uuid4().hex}"
    submitted = workspace / "submitted.txt"
    try:
        _run(profile, ["/bin/launchctl", "submit", "-l", label, "-o", str(submitted), "--",
                       "/bin/cat", str(canary)], workspace)  # fmt: skip
        assert "CANARY" not in _settle(submitted)
    finally:
        subprocess.run(["/bin/launchctl", "remove", label], capture_output=True, check=False)

    found = _run(profile, ["/usr/bin/mdfind", "-name", "Terminal.app"], workspace)
    assert "Terminal.app" not in found.stdout, found.stdout
    assert "(deny lsopen)" in profile.read_text(encoding="utf-8")
