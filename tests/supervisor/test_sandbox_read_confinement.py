"""ADR 0068 (Phase 03, 3.2): strict read confinement — reads are an allowlist.

Before: ``bwrap --ro-bind / /`` hid only the secret floor (a denylist), so a
sandboxed worker read ``~/Downloads``, ``~/Documents``, ``/mnt``, ``/srv``,
``/var`` and ``/etc/pve`` — the "Hermes roamed ~/Downloads" failure. A
strict project's runs, and the G10 re-run of their patch, now start from an
empty root: the system dirs, the detected toolchains, the run's own roots and
the operator's ``extra_read_paths`` are bound back, and the secret masks still
land last. A backend that cannot enforce it refuses the run.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import RunStore, SupervisorConfig, sandbox
from skep.supervisor.worktree import git_metadata_readable_roots, git_metadata_writable_roots

from .conftest import git, serve_client

_TOOLCHAIN_ENV = (
    "UV_PYTHON_INSTALL_DIR",
    "UV_TOOL_DIR",
    "CARGO_HOME",
    "RUSTUP_HOME",
    "NVM_DIR",
    "PYENV_ROOT",
)

needs_strict_wall = pytest.mark.skipif(
    sandbox.availability().backend not in ("bubblewrap", "seatbelt"),
    reason="real sandbox proof (runs where bwrap or sandbox-exec is the backend)",
)
_SEATBELT_STRICT = "(deny file-read* file-test-existence (with errno ENOENT))"


def _assert_strict(profile: Path) -> None:
    """The wall that ran was the strict one, on whichever backend ran it."""
    text = profile.read_text(encoding="utf-8")
    if sandbox.availability().backend == "seatbelt":
        assert _SEATBELT_STRICT in text
    else:
        assert _profile_argv(profile)[:3] != ["--ro-bind", "/", "/"]
        assert "--ro-bind" in _profile_argv(profile)


# Prints {path: "read" | the error's name}; a directory counts as read when
# it can be listed. Exits 1 when anything outside ``--expect-read`` was read.
_PROBE = """
import json, os, sys
seen = {}
for path in sys.argv[1:]:
    try:
        os.listdir(path) if os.path.isdir(path) else open(path).read()
        seen[path] = "read"
    except OSError as exc:
        seen[path] = type(exc).__name__
print(json.dumps(seen))
"""


@pytest.fixture(autouse=True)
def clear_sandbox_probe_cache() -> Iterator[None]:
    sandbox.availability.cache_clear()
    yield
    sandbox.availability.cache_clear()


@pytest.fixture(autouse=True)
def no_other_homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from skep.supervisor import secret_floor

    monkeypatch.setattr(secret_floor, "HOME_DIRS", (f"{tmp_path}/no-homes/*",))
    for name in (*sandbox._SOCKET_ENV, *_TOOLCHAIN_ENV):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake HOME holding the plan's decoys and a credential canary."""
    home = tmp_path / "fakehome"
    for rel in (
        "Downloads/canary-downloads.txt",
        "Documents/canary-proxmox.txt",
        ".ssh/id_rsa",
    ):
        path = home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"CANARY {rel}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture()
def skep_home(tmp_path: Path) -> Path:
    home = tmp_path / "skep"
    supervisor = home / "supervisor"
    for rel in ("worktrees/other-run", "results/other-run", "audit/other-run"):
        (supervisor / rel).mkdir(parents=True)
    for name in ("serve-token", "llm-secret"):
        (supervisor / name).write_text(f"CANARY {name}\n", encoding="utf-8")
    RunStore(supervisor / "supervisor.sqlite3").close()
    (home / "repos" / "other-project").mkdir(parents=True)
    (home / "worker_plugins").mkdir()
    (home / "profile.json").write_text("{}\n", encoding="utf-8")
    return home


@pytest.fixture()
def host_canaries(tmp_path: Path) -> Iterator[list[Path]]:
    """Decoys on the host itself, outside every home: /tmp, /var/tmp, and a
    host temp dir no run binds (removed after; never under $HOME)."""
    made: list[Path] = []
    for where in ("/tmp", "/var/tmp"):
        if os.path.isdir(where) and os.access(where, os.W_OK):
            fd, name = tempfile.mkstemp(prefix="skep-canary-", dir=where)
            os.write(fd, b"CANARY host\n")
            os.close(fd)
            made.append(Path(name))
    host_tmp = tmp_path / "host-tmp" / "canary-host-tmp.txt"
    host_tmp.parent.mkdir()
    host_tmp.write_text("CANARY host tmp\n", encoding="utf-8")
    try:
        yield [*made, host_tmp]
    finally:
        for path in made:
            path.unlink(missing_ok=True)


def _binds(args: list[str]) -> list[tuple[str, str, str]]:
    return [
        (flag, args[i + 1], args[i + 2])
        for i, flag in enumerate(args)
        if flag in ("--ro-bind", "--bind", "--symlink") and i + 2 < len(args)
    ]


def _usable(monkeypatch: pytest.MonkeyPatch, backend: str) -> None:
    probe = sandbox.SandboxAvailability(True, backend=backend)
    monkeypatch.setattr(sandbox, "require_usable", lambda backend=None: probe)


def _profile_argv(path: Path) -> list[str]:
    argv: list[str] = json.loads(path.read_text(encoding="utf-8").partition("\n")[2])
    return argv


# --- the wall, as argv -------------------------------------------------------


def test_a_strict_bwrap_root_binds_the_system_dirs_and_never_the_host_root(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    args = sandbox._bubblewrap_args(
        writable_roots=[workspace],
        network=sandbox.DENY_ALL_NETWORK,
        proxy_port=None,
        strict=True,
    )
    binds = _binds(args)
    assert ("--ro-bind", "/", "/") not in binds
    assert ("--ro-bind", "/usr", "/usr") in binds
    dests = {dest for _flag, _src, dest in binds}
    for host_dir in ("/", "/var", "/mnt", "/media", "/srv", "/home", "/root", "/boot", "/etc"):
        assert host_dir not in dests, host_dir
    # /etc comes back entry by entry, minus the auth secrets and Proxmox's tree
    for hidden in ("pve", "shadow", "gshadow", "sudoers", "sudoers.d", "ssh"):
        assert f"/etc/{hidden}" not in dests
    if os.path.exists("/etc/passwd"):
        assert "/etc/passwd" in dests
    # the empty root itself is read-only after every bind is in place
    remount = args.index("--remount-ro")
    assert args[remount + 1] == "/"
    assert remount > max(i for i, flag in enumerate(args) if flag in ("--ro-bind", "--bind"))


def test_denylist_mode_keeps_the_host_root_unchanged(tmp_path: Path) -> None:
    """Existing projects (and every non-strict profile) are untouched."""
    args = sandbox._bubblewrap_args(
        writable_roots=[tmp_path], network=sandbox.DENY_ALL_NETWORK, proxy_port=None
    )
    assert args[:3] == ["--ro-bind", "/", "/"]
    assert "--remount-ro" not in args


def test_the_secret_masks_land_after_every_extra_read_path(
    fake_home: Path, skep_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Masks LAST: an operator who lists a dir holding a credential still
    never reads it (the whole home is refused outright, W2)."""
    _usable(monkeypatch, "bubblewrap")
    cargo = fake_home / ".cargo"
    cargo.mkdir()
    (cargo / "credentials.toml").write_text("CANARY\n", encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    profile = sandbox.write_profile(
        tmp_path / "p.profile",
        workspace=workspace,
        skep_home=skep_home,
        read_allowlist=(cargo,),
    )
    args = _profile_argv(profile)
    cargo_bind = args.index(str(cargo))
    mask = max(i for i, arg in enumerate(args) if arg == str(cargo / "credentials.toml"))
    assert mask > cargo_bind
    assert args[mask - 2 : mask] == ["--ro-bind", "/dev/null"]
    token_mask = args.index(str(skep_home / "supervisor" / "serve-token"))
    assert args[token_mask - 2 : token_mask] == ["--ro-bind", "/dev/null"]


def test_the_strict_roots_are_the_toolchains_never_the_home(
    fake_home: Path, skep_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    toolchains = (
        ".local/bin",
        ".local/share/uv/python",
        ".local/share/uv/tools",
        ".local/share/claude",
        ".cargo",
        ".rustup",
        ".nvm",
        ".pyenv",
    )
    for rel in (*toolchains, ".local/share/keyrings", "Projects"):
        (fake_home / rel).mkdir(parents=True)
    (skep_home / "tools" / ".venvs").mkdir(parents=True)
    roots = sandbox.strict_read_roots(skep_home)
    for rel in toolchains:
        assert (fake_home / rel).resolve() in roots, rel
    assert (skep_home / "tools" / ".venvs").resolve() in roots
    assert Path(sys.prefix).resolve() in roots  # skep's own interpreter
    assert Path(sandbox.__file__).resolve().parents[2] in roots  # and its package
    for never in ("", "Downloads", "Documents", ".ssh", ".local/share/keyrings", "Projects"):
        assert (fake_home / never).resolve() not in roots, never
    # a toolchain moved by its own variable is found there instead
    moved = tmp_path / "cargo-elsewhere"
    moved.mkdir()
    monkeypatch.setenv("CARGO_HOME", str(moved))
    roots = sandbox.strict_read_roots(skep_home)
    assert moved.resolve() in roots
    assert (fake_home / ".cargo").resolve() not in roots


def test_a_detected_root_at_or_above_the_home_is_never_bound(
    fake_home: Path, skep_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYENV_ROOT", str(fake_home))
    monkeypatch.setenv("NVM_DIR", "/")
    roots = sandbox.strict_read_roots(skep_home)
    assert fake_home.resolve() not in roots
    assert Path("/") not in roots


def test_the_worker_reads_only_its_own_skep_home_files(fake_home: Path, skep_home: Path) -> None:
    sup = skep_home / "supervisor"
    roots = sandbox.strict_read_roots(skep_home)
    assert (skep_home / "profile.json").resolve() in roots
    assert (skep_home / "worker_plugins").resolve() in roots
    assert not any(root.is_relative_to(sup.resolve()) for root in roots)
    with_key = sandbox.strict_read_roots(skep_home, worker_llm_key=True)
    assert (sup / "supervisor.sqlite3").resolve() in with_key
    assert (sup / "llm-secret").resolve() in with_key
    for never in ("serve-token", "worktrees", "results", "audit"):
        assert (sup / never).resolve() not in with_key, never
    assert not any(root.is_relative_to((skep_home / "repos").resolve()) for root in with_key)
    assert skep_home.resolve() not in with_key


def test_an_extra_read_path_that_resolves_to_the_root_is_refused(
    fake_home: Path, skep_home: Path, tmp_path: Path
) -> None:
    link = tmp_path / "everything"
    link.symlink_to("/")
    with pytest.raises(sandbox.SandboxUnavailableError, match="extra_read_paths"):
        sandbox.strict_read_roots(skep_home, extra=(link,))


@pytest.mark.parametrize("backend", ["podman"])
def test_a_backend_that_cannot_enforce_strict_reads_refuses_the_run(
    backend: str, skep_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed (I12): never a quiet fall back to open reads."""
    _usable(monkeypatch, backend)
    with pytest.raises(sandbox.SandboxUnavailableError, match="strict read confinement"):
        sandbox.write_profile(
            tmp_path / "p.profile", workspace=tmp_path, skep_home=skep_home, read_allowlist=()
        )
    assert not (tmp_path / "p.profile").exists()


def test_the_seatbelt_profile_is_unchanged_without_strict_reads(
    skep_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A project not on strict reads keeps macOS's denylist profile."""
    _usable(monkeypatch, "seatbelt")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    profile = sandbox.write_profile(tmp_path / "p.sb", workspace=workspace, skep_home=skep_home)
    assert "(allow default)" in profile.read_text(encoding="utf-8")
    assert "deny file-read* file-test-existence" not in profile.read_text(encoding="utf-8")


def _rule(lines: list[str], text: str) -> int:
    return next(i for i, line in enumerate(lines) if text in line)


def test_a_strict_seatbelt_profile_denies_every_read_then_reopens_the_allowlist(
    fake_home: Path, skep_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A GLOBAL read deny (not allow-default plus a /Users deny, which left
    host /tmp and /var open), the allowlist after it, the masks last."""
    _usable(monkeypatch, "seatbelt")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    git_root = tmp_path / "common" / "config"
    git_root.parent.mkdir()
    git_root.write_text("[core]\n", encoding="utf-8")
    cargo = fake_home / ".cargo"  # a legal extra path holding a credential
    cargo.mkdir()
    (cargo / "credentials.toml").write_text("CANARY\n", encoding="utf-8")
    profile = sandbox.write_profile(
        tmp_path / "p.sb",
        workspace=workspace,
        extra_readable=[git_root],
        skep_home=skep_home,
        read_allowlist=(cargo,),
    )
    lines = profile.read_text(encoding="utf-8").splitlines()
    deny = lines.index(_SEATBELT_STRICT)
    assert deny > lines.index("(allow default)")
    allows = [i for i, line in enumerate(lines) if line.startswith("(allow file-read")]
    assert allows and min(allows) > deny
    reads = "\n".join(lines[i] for i in allows)
    for system in ("/usr", "/System", "/Library", "/bin", "/sbin", "/dev", "/private/etc"):
        assert f'(subpath "{system}")' in reads, system
    for own in (workspace, git_root, cargo, Path(sys.prefix)):
        assert f'(subpath "{own.resolve()}")' in reads, own
    for host in ("/", "/Users", "/private/tmp", "/private/var", "/private/var/folders", "/tmp"):
        assert f'(subpath "{host}")' not in reads, host
    # the ancestors: metadata only, so getcwd/realpath work but nothing lists
    metadata = lines[_rule(lines, "(allow file-read-metadata file-test-existence")]
    top = workspace.resolve().parents[-2]  # /Users on a Mac, /tmp or /home on CI
    for ancestor in ("/private", str(top), str(workspace.resolve().parent)):
        assert f'(literal "{ancestor}")' in metadata, ancestor
    assert "subpath" not in metadata
    # last match wins: the masks follow every allow, the extra path's own included
    ssh = _rule(lines, f'(deny file-read* (subpath "{(fake_home / ".ssh").resolve()}"))')
    cred = _rule(lines, f'(deny file-read* (literal "{(cargo / "credentials.toml").resolve()}"))')
    token = _rule(lines, str((skep_home / "supervisor" / "serve-token").resolve()))
    sudoers = _rule(lines, '"/private/etc/sudoers")')
    assert lines[sudoers].startswith("(deny file-read* (literal")
    assert min(ssh, cred, token, sudoers) > max(allows)


def test_a_strict_seatbelt_profile_refuses_an_extra_path_that_is_the_root(
    skep_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed (I12): no profile, no run — never open reads."""
    _usable(monkeypatch, "seatbelt")
    link = tmp_path / "everything"
    link.symlink_to("/")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(sandbox.SandboxUnavailableError, match="extra_read_paths"):
        sandbox.write_profile(
            tmp_path / "p.sb", workspace=workspace, skep_home=skep_home, read_allowlist=(link,)
        )
    assert not (tmp_path / "p.sb").exists()


def test_read_allowlist_follows_the_project_policy() -> None:
    assert sandbox.read_allowlist_for({}) is None
    assert sandbox.read_allowlist_for({"strict_read_confinement": False}) is None
    assert sandbox.read_allowlist_for({"strict_read_confinement": True}) == ()
    assert sandbox.read_allowlist_for(
        {"strict_read_confinement": True, "extra_read_paths": ["/srv/data"]}
    ) == (Path("/srv/data"),)


def test_a_linked_worktree_reads_its_common_dir_but_not_its_siblings(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    git(repo, "add", "a.txt")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "a")
    for name in ("mine", "sibling"):
        git(repo, "worktree", "add", "-q", "--detach", str(tmp_path / name))
    roots = git_metadata_readable_roots(tmp_path / "mine")
    common = (repo / ".git").resolve()
    assert common / "config" in roots
    assert common / "refs" / "heads" in roots  # refs/ minus the stash (W4)
    assert all(root.is_relative_to(common) for root in roots)
    assert common / "worktrees" not in roots  # the siblings' HEAD/index stay out
    assert common / "objects" not in roots  # bound writable, never walked for sockets


# --- the wall, for real (bubblewrap or Seatbelt) ----------------------------


@needs_strict_wall
def test_a_strict_worker_reads_no_canary(
    fake_home: Path, skep_home: Path, host_canaries: list[Path], tmp_path: Path
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "own.txt").write_text("mine\n", encoding="utf-8")
    profile = sandbox.write_profile(
        tmp_path / "p.profile", workspace=workspace, skep_home=skep_home, read_allowlist=()
    )
    sup = skep_home / "supervisor"
    canaries = [
        fake_home / "Downloads" / "canary-downloads.txt",
        fake_home / "Downloads",
        fake_home / "Documents" / "canary-proxmox.txt",
        fake_home / ".ssh" / "id_rsa",
        *host_canaries,
        sup / "serve-token",
        sup / "llm-secret",
        sup / "worktrees" / "other-run",
        skep_home / "repos" / "other-project",
        Path("/etc/shadow"),
        Path("/etc/pve"),
        Path("/var"),
        Path("/mnt"),
        Path("/srv"),
        Path("/media"),
        Path("/root"),
        # macOS: the same canary by its firmlink and volume names, the host's
        # per-user temp dirs, and /etc's auth entries (absent on Linux: not read)
        Path("/System/Volumes/Data", *fake_home.resolve().parts[1:], "Downloads"),
        Path("/Volumes/Macintosh HD", *fake_home.resolve().parts[1:], "Downloads"),
        Path("/private/var/folders"),
        Path("/private/etc/master.passwd"),
        Path("/etc/master.passwd"),  # its /etc link spelling, read through metadata
        Path("/private/etc/sudoers"),
        Path("/Users/Shared"),
    ]
    proc = subprocess.run(
        sandbox.wrap_command(
            [sys.executable, "-c", _PROBE, str(workspace / "own.txt"), *map(str, canaries)],
            profile,
        ),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    seen = json.loads(proc.stdout)
    assert seen.pop(str(workspace / "own.txt")) == "read"  # the probe itself works
    assert [path for path, verdict in seen.items() if verdict == "read"] == [], seen
    _assert_strict(profile)


@needs_strict_wall
def test_a_strict_run_still_reads_its_own_unmasked_file(skep_home: Path, tmp_path: Path) -> None:
    """ES-F122: a codex run's own login copy, inside the masked audit dir,
    stays readable to that run alone under the strict wall too."""
    audit = skep_home / "supervisor" / "audit"
    (audit / "this-run").mkdir(parents=True)
    own, other = audit / "this-run" / "auth.json", audit / "other-run" / "auth.json"
    for path in (own, other):
        path.parent.mkdir(exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    profile = sandbox.write_profile(
        tmp_path / "p.profile",
        workspace=workspace,
        skep_home=skep_home,
        unmasked=[own],
        read_allowlist=(),
    )
    proc = subprocess.run(
        sandbox.wrap_command([sys.executable, "-c", _PROBE, str(own), str(other)], profile),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    seen = json.loads(proc.stdout)
    assert seen[str(own)] == "read" and seen[str(other)] != "read", seen
    _assert_strict(profile)


@needs_strict_wall
def test_a_strict_worker_still_runs_python_git_uv_and_pytest(
    skep_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real toolchain, found by the real detection (HOME is only READ:
    the root is read-only, and the skep home is a temp one)."""
    monkeypatch.setenv("HOME", os.path.expanduser("~"))
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "test_tiny.py").write_text("def test_tiny():\n    assert 1 + 1 == 2\n")
    git(repo, "add", "test_tiny.py")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "tiny")
    worktree = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "--detach", str(worktree))
    profile = sandbox.write_profile(
        tmp_path / "p.profile",
        workspace=worktree,
        extra_writable=git_metadata_writable_roots(worktree),
        extra_readable=git_metadata_readable_roots(worktree),
        skep_home=skep_home,
        read_allowlist=(),
    )
    steps = [
        # getcwd/realpath walk the ancestors (Seatbelt: metadata only)
        f"{shlex.quote(sys.executable)} -c 'import os, skep; "
        'print(os.path.realpath(os.getcwd()), "python ok")\'',
        "git status --short && git log --oneline -1 >/dev/null && echo 'git ok'",
    ]
    if shutil.which("uv"):
        steps.append(
            f"uv run --offline --no-project --python {shlex.quote(sys.executable)} "
            "python -m pytest -q -p no:cacheprovider test_tiny.py && echo 'uv pytest ok'"
        )
    if shutil.which("node"):
        steps.append("node --version >/dev/null && echo 'node ok'")
    # libc finds the zone through /etc/localtime's link spellings
    steps.append('echo "tz $(date +%Z)"')
    tmp = worktree / ".tmp"
    tmp.mkdir()
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ["HOME"],
        "UV_CACHE_DIR": str(tmp / "uv"),
        **sandbox.in_wall_tmp_env(tmp),
    }
    proc = subprocess.run(
        sandbox.wrap_command(["/bin/sh", "-c", " && ".join(steps)], profile),
        cwd=worktree,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "python ok" in proc.stdout and "git ok" in proc.stdout
    assert f"{worktree.resolve()} python ok" in proc.stdout
    host = subprocess.run(["date", "+%Z"], env=env, capture_output=True, text=True, check=True)
    assert f"tz {host.stdout.strip()}" in proc.stdout
    if shutil.which("uv"):
        assert "uv pytest ok" in proc.stdout and "1 passed" in proc.stdout
    if shutil.which("node"):
        assert "node ok" in proc.stdout
    _assert_strict(profile)


@needs_strict_wall
def test_the_g10_rerun_runs_inside_the_same_strict_wall(
    repo: Path, config: SupervisorConfig, fake_home: Path, host_canaries: list[Path],
    tmp_path: Path,
) -> None:  # fmt: skip
    from skep.supervisor.reverify import reverify

    patch = tmp_path / "marker.patch"
    patch.write_text(
        "diff --git a/patched_marker.txt b/patched_marker.txt\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/patched_marker.txt\n@@ -0,0 +1 @@\n+patched\n",
        encoding="utf-8",
    )
    canaries = [
        fake_home / "Downloads" / "canary-downloads.txt",
        fake_home / "Documents" / "canary-proxmox.txt",
        fake_home / ".ssh" / "id_rsa",
        *host_canaries,
        repo / "existing.py",  # the operator's own checkout, outside the worktree
        Path("/etc/shadow"),
        Path("/var"),
        Path("/mnt"),
    ]
    leak_check = (
        f"{shlex.quote(sys.executable)} -c {shlex.quote(_PROBE)} "
        + " ".join(shlex.quote(str(path)) for path in canaries)
        + " | grep -v '\"read\"'"
    )
    profile = tmp_path / "reverify.profile.sb"
    outcome = reverify(
        repo=repo,
        ref=None,
        patch_path=patch,
        commands=[leak_check, "git status --short && cat patched_marker.txt"],
        config=config,
        profile_path=profile,
        env={"PATH": os.environ["PATH"], "HOME": str(fake_home)},
        read_allowlist=(),
    )
    assert outcome.outcome == "passed", outcome.detail
    assert outcome.exit_codes == [0, 0]
    _assert_strict(profile)


@needs_strict_wall
@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not on PATH")
def test_g10_still_runs_a_uv_pytest_pin_inside_the_strict_wall(
    repo: Path, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real toolchain, found by the real detection (HOME only READ)."""
    from skep.supervisor.reverify import reverify

    monkeypatch.setenv("HOME", os.path.expanduser("~"))
    (repo / "test_tiny.py").write_text("def test_tiny():\n    assert 1 + 1 == 2\n")
    git(repo, "add", "test_tiny.py")
    git(repo, "commit", "-qm", "tiny")
    patch = tmp_path / "marker.patch"
    patch.write_text(
        "diff --git a/patched_marker.txt b/patched_marker.txt\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/patched_marker.txt\n@@ -0,0 +1 @@\n+patched\n",
        encoding="utf-8",
    )
    outcome = reverify(
        repo=repo,
        ref=None,
        patch_path=patch,
        commands=[
            'UV_CACHE_DIR="$TMPDIR/uv" uv run --offline --no-project --python '
            f"{shlex.quote(sys.executable)} python -m pytest -q -p no:cacheprovider test_tiny.py",
            "git status --short",
        ],
        config=config,
        profile_path=tmp_path / "reverify.profile.sb",
        env={"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]},
        read_allowlist=(),
    )
    assert outcome.outcome == "passed", outcome.detail
    assert outcome.exit_codes == [0, 0]
    _assert_strict(tmp_path / "reverify.profile.sb")


# --- plumbing: dispatch and G10 take the wall from the project --------------


def _bind(config: SupervisorConfig, repo: Path, policy: dict[str, Any]) -> None:
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="strict", name="strict", strategy="trusted_local_dev",
            phase="build", policy=policy,
        )  # fmt: skip
        store.add_project_binding(
            project_id="strict", binding_kind="repo_path", binding_value=str(repo)
        )
    finally:
        store.close()


class _Stop(Exception):
    pass


@pytest.mark.parametrize(
    ("recorded", "expected"),
    [({"read_allowlist": ["/srv/data"]}, (Path("/srv/data"),)), ({}, None)],
)
def test_g10_takes_its_wall_from_the_dispatched_record(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch,
    recorded: dict[str, Any], expected: Any,
) -> None:  # fmt: skip
    """W1: the wall the worker ran in, as dispatch recorded it — the project
    policy (strict here) is never read again."""
    import importlib

    # the package exports the function under the module's name
    reverify_module = importlib.import_module("skep.supervisor.reverify")
    _bind(config, repo, {"strict_read_confinement": True})
    store = RunStore(config.db_path)
    try:
        store.transition("t-strict", "dispatched", json.dumps(recorded))
    finally:
        store.close()
    seen: dict[str, Any] = {}

    def spy(**kwargs: Any) -> Any:
        seen.update(kwargs)
        raise _Stop

    monkeypatch.setattr(reverify_module, "reverify", spy)
    monkeypatch.setattr(reverify_module.sandbox, "available", lambda: True)
    store = RunStore(config.db_path)
    try:
        with pytest.raises(_Stop):
            reverify_module.reverify_run(
                store=store, task_id="t-strict", worker_outcome="completed", repo=repo,
                ref=None, config=replace(config, sandbox=True), verify_command="true",
            )  # fmt: skip
    finally:
        store.close()
    assert seen["read_allowlist"] == expected


@pytest.mark.parametrize(
    ("policy", "expected"),
    [({"strict_read_confinement": True}, ()), ({"strict_read_confinement": False}, None)],
)
def test_dispatch_hands_the_worker_its_projects_wall(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch,
    policy: dict[str, Any], expected: Any,
) -> None:  # fmt: skip
    from skep.supervisor import dispatch, run_task

    _bind(config, repo, {"default_execution_mode": "sandbox", **policy})
    seen: dict[str, Any] = {}

    def spy(*_args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise OSError("spy: no worker")

    monkeypatch.setattr(dispatch, "spawn_worker", spy)
    monkeypatch.setattr(
        dispatch,
        "sandbox_availability",
        lambda backend=None: sandbox.SandboxAvailability(True, backend="bubblewrap"),
    )
    run_task(repo, "x", config=replace(config, sandbox=True), execution_mode="sandbox")
    assert seen["read_allowlist"] == expected


def test_the_spawner_passes_the_wall_to_the_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import spawner
    from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task

    seen: dict[str, object] = {}
    _usable(monkeypatch, "bubblewrap")
    monkeypatch.setattr(sandbox, "write_profile", lambda path, **kw: seen.update(kw) or path)
    monkeypatch.setattr(sandbox, "wrap_command", lambda argv, profile, backend=None: argv)
    monkeypatch.setattr(spawner, "git_metadata_writable_roots", lambda _ws: ())
    monkeypatch.setattr(spawner, "git_metadata_readable_roots", lambda _ws: ())
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: type("P", (), {"pid": 1})())
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config = SupervisorConfig(home=tmp_path / "skep" / "supervisor", worker_command=("w",))
    task = mint_task(workspace=workspace, instructions="x", budget=DEFAULT_BUDGET)
    spawner.spawn_worker(
        config,
        task,
        tmp_path / "t.json",
        tmp_path / "o.json",
        log_path=tmp_path / "w.log",
        read_allowlist=(Path("/srv/data"),),
    )
    assert seen["read_allowlist"] == (Path("/srv/data"),)


# --- the Queen: a bare name is a root-boundary refusal, never a home scan ----


def test_a_bare_file_name_gets_a_root_boundary_refusal(
    config: SupervisorConfig, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor.serve.fileio import queen_filesystem_decision
    from skep.supervisor.serve.settings import ConfigHolder

    # The operator's cwd is the home that holds the file: never resolved there.
    (fake_home / "canary-downloads.txt").write_text("CANARY cwd\n", encoding="utf-8")
    monkeypatch.chdir(fake_home)
    store = RunStore(config.db_path)
    try:
        decision = queen_filesystem_decision(
            store, ConfigHolder(config, store), action="read", path="canary-downloads.txt"
        )
    finally:
        store.close()
    assert decision.verdict == "deny"
    assert decision.reason == "filesystem.deny.outside_root_lookup"
    assert decision.decided_by == "default/root-boundary"
    detail = decision.detail or ""
    assert str(config.home) in detail  # names the roots it does read
    assert "absolute path" in detail and "~/Downloads" in detail
    assert "CANARY" not in detail
    # the secret floor still answers first: the audit trail names the floor
    store = RunStore(config.db_path)
    try:
        floor = queen_filesystem_decision(
            store, ConfigHolder(config, store), action="read", path=".ssh/id_rsa"
        )
    finally:
        store.close()
    assert floor.reason == "filesystem.deny.secret_floor"


# --- rollout: new projects strict, existing ones migrate on purpose ----------


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        (["/srv/data", "/opt/sdk"], True),
        (["relative/path"], False),
        (["/"], False),
        ("/srv/data", False),
    ],
)
def test_extra_read_paths_are_absolute_and_never_the_root(value: Any, ok: bool) -> None:
    from skep.supervisor.projects import validate_project_policy

    if ok:
        assert validate_project_policy({"extra_read_paths": value})["extra_read_paths"] == value
    else:
        with pytest.raises(ValueError, match="extra_read_paths"):
            validate_project_policy({"extra_read_paths": value})
    with pytest.raises(ValueError, match="strict_read_confinement"):
        validate_project_policy({"strict_read_confinement": "yes"})


def _host(monkeypatch: pytest.MonkeyPatch, backend: str | None) -> None:
    import skep.supervisor.serve.registry as registry

    probe = sandbox.SandboxAvailability(backend is not None, backend=backend)
    monkeypatch.setattr(registry, "new_project_sandbox_probe", lambda backend=None: probe)


def _setup(config: SupervisorConfig, repo: Path, route: str = "setup", **body: Any) -> Any:
    payload = {"project_id": "sbx", "name": "sbx", "strategy": "trusted_local_dev",
               "repo_path": str(repo), **body}  # fmt: skip
    response = serve_client(config).post(f"/api/projects/{route}", json=payload)
    assert response.status_code in (200, 201), response.text
    return (response.json().get("project") or response.json())["policy"]


@pytest.mark.parametrize("backend", ["bubblewrap", "seatbelt"])
@pytest.mark.parametrize("route", ["setup", "preview"])
def test_a_new_sandboxed_project_reads_strictly_where_the_backend_enforces_it(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch, route: str,
    backend: str,
) -> None:  # fmt: skip
    _host(monkeypatch, backend)
    assert _setup(config, repo, route)["strict_read_confinement"] is True


@pytest.mark.parametrize("backend", ["podman", None])
def test_no_strict_default_where_the_backend_cannot_enforce_it(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch, backend: str | None
) -> None:
    _host(monkeypatch, backend)
    assert "strict_read_confinement" not in _setup(config, repo)


def test_an_existing_project_keeps_its_reads_until_the_operator_migrates_it(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host(monkeypatch, None)  # set up before the host could sandbox
    _setup(config, repo, policy_overrides={"default_execution_mode": "sandbox"})
    _host(monkeypatch, "bubblewrap")
    assert "strict_read_confinement" not in _setup(config, repo, phase="maintain")
    migrated = _setup(config, repo, policy_overrides={"strict_read_confinement": True})
    assert migrated["strict_read_confinement"] is True
    assert _setup(config, repo, phase="build")["strict_read_confinement"] is True  # carried


def test_an_explicit_no_wins_on_a_new_project(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host(monkeypatch, "bubblewrap")
    policy = _setup(config, repo, policy_overrides={"strict_read_confinement": False})
    assert policy["strict_read_confinement"] is False


def test_the_cli_migrates_a_project_with_strict_reads() -> None:
    from skep.cli import _parser
    from skep.supervisor.cli_cmds import _project_policy_overrides

    parser = _parser()
    on = parser.parse_args(["project", "setup", "/r", "--strict-reads"])
    off = parser.parse_args(["project", "setup", "/r", "--no-strict-reads"])
    unset = parser.parse_args(["project", "setup", "/r"])
    assert _project_policy_overrides(on)["strict_read_confinement"] is True
    assert _project_policy_overrides(off)["strict_read_confinement"] is False
    assert "strict_read_confinement" not in _project_policy_overrides(unset)


@pytest.mark.parametrize(
    ("backend", "confinement"),
    [
        ("bubblewrap", "strict-allowlist"),
        ("seatbelt", "strict-allowlist"),
        ("podman", "denylist-only"),
    ],
)
def test_doctor_reports_the_read_confinement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str, confinement: str
) -> None:
    from skep import status

    monkeypatch.setattr(
        status, "sandbox_availability", lambda: sandbox.SandboxAvailability(True, backend=backend)
    )
    check = status._sandbox_check(tmp_path)
    assert check["read_confinement"] == confinement
    assert f"read confinement: {confinement}" in check["detail"]


def test_doctor_names_the_sandboxed_projects_still_reading_the_whole_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep import status

    monkeypatch.setattr(
        status,
        "sandbox_availability",
        lambda: sandbox.SandboxAvailability(True, backend="bubblewrap"),
    )
    (tmp_path / "supervisor").mkdir()
    store = RunStore(tmp_path / "supervisor" / "supervisor.sqlite3")
    try:
        for project_id, policy in (
            ("old", {"default_execution_mode": "sandbox"}),
            ("new", {"default_execution_mode": "sandbox", "strict_read_confinement": True}),
            ("loose", {"default_execution_mode": "workspace"}),
        ):
            store.add_project_policy(
                project_id=project_id, name=project_id, strategy="trusted_local_dev",
                phase="build", policy=policy,
            )  # fmt: skip
    finally:
        store.close()
    check = status._sandbox_check(tmp_path)
    assert "old" in check["next_step"] and "new" not in check["next_step"]
    assert "loose" not in check["next_step"]  # unsandboxed: the workspace advisory's job
    assert "--project-id old --strict-reads" in check["next_step"]


def test_a_socket_that_is_a_root_or_in_an_extra_path_is_still_masked(
    skep_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5 kept: git's fsmonitor socket is now a root of its own (the common
    dir's entries are bound one by one), and an operator's extra path is
    walked too — a socket bound read-only stays connectable."""
    import socket

    _usable(monkeypatch, "bubblewrap")
    base = Path(tempfile.mkdtemp(prefix="skr-"))  # short: AF_UNIX paths cap at 108
    try:
        repo, extra = base / "r", base / "x"
        repo.mkdir()
        extra.mkdir()
        git(repo, "init", "-q")
        git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty",
            "-m", "a")  # fmt: skip
        git(repo, "worktree", "add", "-q", "--detach", str(base / "w"))
        sockets = [repo / ".git" / "fsm.ipc", extra / "agent.sock"]
        for path in sockets:
            sock = socket.socket(socket.AF_UNIX)
            sock.bind(str(path))
            sock.close()
        roots = git_metadata_readable_roots(base / "w")
        assert sockets[0].resolve() in roots
        profile = sandbox.write_profile(
            base / "p.profile",
            workspace=base / "w",
            extra_readable=roots,
            skep_home=skep_home,
            read_allowlist=(extra,),
        )
        args = _profile_argv(profile)
        for path in sockets:
            at = max(i for i, arg in enumerate(args) if arg == str(path.resolve()))
            assert args[at - 2 : at] == ["--ro-bind", "/dev/null"], path
    finally:
        shutil.rmtree(base, ignore_errors=True)
