"""Phase 09 R3 (S1/N1, N2), R4 (SD-1): how bubblewrap takes a script's inputs.

R2 staged every run — inputs or not — into ``<profile parent>/.skep-inputs``
inside the mounted workspace, and crashed when that parent did not exist yet
(33 Linux failures). R3 moved the copies to a private temp dir; R4 found the
supervisor reading files (``/proc``) on the script's behalf to make them, so
scope decision SD-1 removed staging: each input is bound read-only in place,
a masked input refuses before anything is granted, and nothing is written
for inputs anywhere.

The Mac has no bwrap: these drive the profile builder with the backend
faked and read the argv it would hand bwrap."""

from __future__ import annotations

import json
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


def test_walled_writes_nothing_for_its_inputs(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4 (SD-1): no stage under the skep home or the temp dir — the bind
    names the operator's file itself."""
    _bwrap(monkeypatch)
    made: list[str] = []
    real = tempfile.mkdtemp

    def spy(*a: object, **kw: object) -> str:
        made.append(str(kw.get("prefix")))
        return real(*a, **kw)  # type: ignore[call-overload,no-any-return]

    monkeypatch.setattr(tempfile, "mkdtemp", spy)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    data = _input(tmp_path)
    before = set(home.rglob("*"))
    with walled(
        ["/bin/true"], skep_home=home, hosts=(), workspace=workspace, read_files=(data,)
    ) as (argv, _env):
        assert _binds(argv)[str(data)] == str(data)
        # write_profile may mint relay-secret, and the (empty) MCP secrets dir
        new = set(home.rglob("*")) - before - {home / "supervisor" / "mcp-secret"}
        assert not [p for p in new if p.is_dir() or p.name.endswith("in.txt")], new
    assert made == []
    assert sorted(p.name for p in data.parent.iterdir()) == ["in.txt"]


@pytest.mark.parametrize("backend", ["bubblewrap", "seatbelt"])
def test_a_masked_input_refuses_before_anything_is_granted(
    tmp_path: Path, home: Path, user_home: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    """S2/N2: Neon read a masked key back through its staged copy."""
    _bwrap(monkeypatch)
    if backend == "seatbelt":
        probe = sandbox.SandboxAvailability(True, backend="seatbelt")
        monkeypatch.setattr(sandbox, "require_usable", lambda backend=None: probe)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(sandbox.SandboxUnavailableError, match="secret") as caught:
        sandbox.write_profile(
            workspace / "p.profile", workspace=workspace, skep_home=home,
            read_allowlist=(), read_files=(user_home / ".ssh" / "id_rsa",),
        )  # fmt: skip
    assert CANARY not in str(caught.value)
    assert not (workspace / "p.profile").exists()


def test_the_input_check_names_the_mask(tmp_path: Path, home: Path, user_home: Path) -> None:
    problem = sandbox.masked_input_problem(user_home / ".ssh" / "id_rsa", skep_home=home)
    assert problem is not None and ".ssh" in problem and "secret" in problem
    assert sandbox.masked_input_problem(_input(tmp_path), skep_home=home) is None


def test_a_mask_under_tmp_lands_over_an_input_bound_there() -> None:
    """The /tmp,/run skip counts read_files destinations as bound roots."""
    dest = Path("/tmp/inputs/secret-dir/in.txt")
    args = sandbox._bubblewrap_args(
        writable_roots=[Path("/work")],
        network=sandbox.DENY_ALL_NETWORK,
        proxy_port=None,
        read_denied=[Path("/tmp/inputs/secret-dir")],
        read_files=[dest],
    )
    assert "/tmp/inputs/secret-dir" in args[args.index(str(dest)) + 1 :]
