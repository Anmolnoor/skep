"""Phase 09 R3 (S1/N1, S2/N2, S9, S10): how bubblewrap stages a script's inputs.

R2 staged every run — inputs or not — into ``<profile parent>/.skep-inputs``
inside the mounted workspace, and crashed when that parent did not exist yet
(33 Linux failures). It also copied a masked file (``~/.ssh/id_rsa``) the
masks could no longer hide: the copy sat at a new path. Now: no inputs, no
staging; a masked input refuses before anything is copied; copies go to a
private temp dir outside the workspace that ``walled`` removes, 0600 (+ the
owner's execute bit), never setuid; and a byte cap holds per file and in all.

The Mac has no bwrap: these drive the profile builder with the backend
faked and read the argv it would hand bwrap."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

import pytest

from skep.supervisor import sandbox
from skep.supervisor.wall import walled

CANARY = "CANARY-INPUT-NOT-A-CREDENTIAL"


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    (tmp_path / "skep" / "supervisor").mkdir(parents=True)
    return tmp_path / "skep"


@pytest.fixture()
def user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake = tmp_path / "user"
    (fake / ".ssh").mkdir(parents=True)
    (fake / ".ssh" / "id_rsa").write_text(CANARY)
    monkeypatch.setenv("HOME", str(fake))
    return fake


def _bwrap(monkeypatch: pytest.MonkeyPatch) -> None:
    probe = sandbox.SandboxAvailability(True, backend="bubblewrap")
    monkeypatch.setattr(sandbox, "require_usable", lambda backend=None: probe)
    monkeypatch.setattr(sandbox, "availability", lambda backend=None: probe)
    monkeypatch.setattr(sandbox, "_bubblewrap_binary", lambda: "/usr/bin/bwrap")


def _args(profile: Path) -> list[str]:
    return list(json.loads(profile.read_text().partition("\n")[2]))


def _binds(args: list[str]) -> dict[str, str]:
    return {args[i + 2]: args[i + 1] for i, a in enumerate(args) if a == "--ro-bind"}


def _input(tmp_path: Path, name: str = "in.txt", body: bytes = b"INPUT") -> Path:
    path = tmp_path / "data" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(body)
    return path


def test_no_inputs_stage_nothing_even_when_the_profile_dir_is_new(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1/N1: the Linux gate's 33 failures — a profile whose parent dir does
    not exist yet, and no inputs at all."""
    _bwrap(monkeypatch)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    profile = tmp_path / "not-yet" / "audit" / "sandbox.profile"
    for allowlist in (None, ()):
        sandbox.write_profile(
            profile, workspace=workspace, skep_home=home, read_allowlist=allowlist
        )
        assert profile.is_file()
    assert not list(tmp_path.rglob(".skep-inputs"))


def test_bwrap_inputs_need_a_stage_outside_the_writable_roots(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bwrap(monkeypatch)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = _input(tmp_path)
    kw = {"workspace": workspace, "skep_home": home, "read_allowlist": (), "read_files": (target,)}
    with pytest.raises(sandbox.SandboxUnavailableError, match="stage"):
        sandbox.write_profile(workspace / "p.profile", **kw)  # type: ignore[arg-type]
    (workspace / "stage").mkdir()
    with pytest.raises(sandbox.SandboxUnavailableError, match="outside"):
        sandbox.write_profile(
            workspace / "p.profile",
            read_files_stage=workspace / "stage",
            **kw,  # type: ignore[arg-type]
        )
    assert not list((workspace / "stage").iterdir())


def test_walled_stages_privately_outside_the_workspace_and_cleans_up(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S2/S9: a 0700 temp dir outside the workspace, 0600 copies (+u+x when
    the source has it, never setuid), bound read-only, gone after the run."""
    _bwrap(monkeypatch)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    data = _input(tmp_path)
    tool = _input(tmp_path, "tool.sh", b"#!/bin/sh\necho hi\n")
    os.chmod(tool, 0o4755)  # setuid + rwx: only the owner's x survives
    with walled(
        ["/bin/true"], skep_home=home, hosts=(), workspace=workspace, read_files=(data, tool)
    ) as (argv, _env):
        binds = _binds(argv)
        staged_data, staged_tool = Path(binds[str(data)]), Path(binds[str(tool)])
        stage = staged_data.parent
        assert stage == staged_tool.parent
        assert not stage.is_relative_to(workspace) and not stage.is_relative_to(data.parent)
        assert stat.S_IMODE(stage.stat().st_mode) == 0o700
        assert stat.S_IMODE(staged_data.stat().st_mode) == 0o600
        assert stat.S_IMODE(staged_tool.stat().st_mode) == 0o700
        assert staged_data.read_bytes() == b"INPUT"
        assert not list(workspace.rglob(".skep-inputs"))
    assert not stage.exists()


def test_walled_with_no_inputs_makes_no_stage(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bwrap(monkeypatch)
    made: list[str] = []
    real = tempfile.mkdtemp

    def spy(*a: object, **kw: object) -> str:
        made.append(str(kw.get("prefix")))
        return real(*a, **kw)  # type: ignore[call-overload,no-any-return]

    monkeypatch.setattr(tempfile, "mkdtemp", spy)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with walled(["/bin/true"], skep_home=home, hosts=(), workspace=workspace):
        pass
    assert not [p for p in made if "input" in p]


@pytest.mark.parametrize("backend", ["bubblewrap", "seatbelt"])
def test_a_masked_input_refuses_before_anything_is_copied(
    tmp_path: Path, home: Path, user_home: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    """S2/N2: Neon read a masked key back through its staged copy."""
    _bwrap(monkeypatch)
    if backend == "seatbelt":
        probe = sandbox.SandboxAvailability(True, backend="seatbelt")
        monkeypatch.setattr(sandbox, "require_usable", lambda backend=None: probe)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    stage = tmp_path / "stage"
    stage.mkdir()
    with pytest.raises(sandbox.SandboxUnavailableError, match="secret") as caught:
        sandbox.write_profile(
            workspace / "p.profile", workspace=workspace, skep_home=home,
            read_allowlist=(), read_files=(user_home / ".ssh" / "id_rsa",),
            read_files_stage=stage,
        )  # fmt: skip
    assert CANARY not in str(caught.value)
    assert not list(stage.iterdir())


def test_the_input_check_names_the_mask(tmp_path: Path, home: Path, user_home: Path) -> None:
    problem = sandbox.masked_input_problem(user_home / ".ssh" / "id_rsa", skep_home=home)
    assert problem is not None and ".ssh" in problem and "secret" in problem
    assert sandbox.masked_input_problem(_input(tmp_path), skep_home=home) is None


def test_an_oversize_input_refuses_naming_the_file_and_cap(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bwrap(monkeypatch)
    monkeypatch.setattr(sandbox, "READ_FILE_MAX_BYTES", 4)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    big = _input(tmp_path, "big.bin", b"0123456789")
    with (
        pytest.raises(sandbox.SandboxUnavailableError, match=r"big\.bin.*4 bytes"),
        walled(["/bin/true"], skep_home=home, hosts=(), workspace=workspace, read_files=(big,)),
    ):
        pass  # pragma: no cover - never entered


def test_inputs_over_the_total_cap_refuse(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bwrap(monkeypatch)
    monkeypatch.setattr(sandbox, "READ_FILES_TOTAL_MAX_BYTES", 5)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    one, two = _input(tmp_path, "a.txt", b"abc"), _input(tmp_path, "b.txt", b"def")
    stage = tmp_path / "stage"
    stage.mkdir()
    with pytest.raises(sandbox.SandboxUnavailableError, match=r"b\.txt.*5 bytes in all"):
        sandbox.write_profile(
            workspace / "p.profile", workspace=workspace, skep_home=home,
            read_allowlist=(), read_files=(one, two), read_files_stage=stage,
        )  # fmt: skip


def test_a_file_that_grows_past_its_fstat_size_is_still_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap holds while copying, not only from fstat: a file that grows
    between the size check and the copy stops at the cap."""
    monkeypatch.setattr(sandbox, "READ_FILE_MAX_BYTES", 4)
    grown = _input(tmp_path, "grown.log", b"0123456789")
    real_fstat = os.fstat

    def small(fd: int) -> os.stat_result:
        found = real_fstat(fd)
        fields = list(found)
        fields[stat.ST_SIZE] = 2
        return os.stat_result(fields)

    monkeypatch.setattr(os, "fstat", small)
    stage = tmp_path / "stage"
    stage.mkdir()
    with pytest.raises(sandbox.SandboxUnavailableError, match=r"grown\.log.*grew"):
        sandbox._staged_read_files([grown], stage)


def test_a_mask_under_tmp_lands_over_an_input_bound_there() -> None:
    """The /tmp,/run skip counts read_files destinations as bound roots."""
    dest = Path("/tmp/inputs/secret-dir/in.txt")
    args = sandbox._bubblewrap_args(
        writable_roots=[Path("/work")],
        network=sandbox.DENY_ALL_NETWORK,
        proxy_port=None,
        read_denied=[Path("/tmp/inputs/secret-dir")],
        read_files=[(Path("/stage/0-in.txt"), dest)],
    )
    assert "/tmp/inputs/secret-dir" in args[args.index(str(dest)) + 1 :]
