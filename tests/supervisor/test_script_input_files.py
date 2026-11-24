"""Phase 09 R2 (S7): a script's declared inputs are bound as files, never
as roots.

``script_inputs`` checked each path was a regular file, then the wall bound
it by path as a read root — which accepts a directory. A directory (or a
link to one) swapped in between the check and the bind was granted whole.
The wall now takes inputs as ``read_files``: a path reached through a link,
or anything but a regular file, refuses; bubblewrap binds a copy of the
object opened without following links, so a later swap changes nothing;
Seatbelt grants the ``(literal …)`` path, never a ``(subpath …)``."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from skep.supervisor import sandbox, scheduler
from skep.supervisor.scheduler import run_schedule_script

needs_wall = pytest.mark.skipif(
    sandbox.availability().backend not in ("bubblewrap", "seatbelt"),
    reason="real sandbox proof (runs where bwrap or sandbox-exec is the backend)",
)


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    (tmp_path / "skep" / "supervisor").mkdir(parents=True)
    return tmp_path / "skep"


def _usable(monkeypatch: pytest.MonkeyPatch, backend: str) -> None:
    probe = sandbox.SandboxAvailability(True, backend=backend)
    monkeypatch.setattr(sandbox, "require_usable", lambda backend=None: probe)


def _profile(tmp_path: Path, home: Path, files: tuple[Path, ...]) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    return sandbox.write_profile(
        workspace / "p.profile", workspace=workspace, skep_home=home,
        read_allowlist=(), read_files=files,
    )  # fmt: skip


def test_a_regular_file_opens(tmp_path: Path) -> None:
    target = tmp_path / "in.txt"
    target.write_text("ok")
    fd = sandbox.open_read_file(target)
    try:
        assert os.read(fd, 8) == b"ok"
    finally:
        os.close(fd)


@pytest.mark.parametrize("kind", ["dir", "link", "linked_parent", "fifo", "missing"])
def test_anything_but_a_regular_file_by_its_real_path_refuses(tmp_path: Path, kind: str) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "in.txt").write_text("x")
    target = {
        "dir": real,
        "link": tmp_path / "link.txt",
        "linked_parent": tmp_path / "alias" / "in.txt",
        "fifo": tmp_path / "pipe",
        "missing": tmp_path / "gone.txt",
    }[kind]
    if kind == "link":
        target.symlink_to(real / "in.txt")
    if kind == "linked_parent":
        (tmp_path / "alias").symlink_to(real)
    if kind == "fifo":
        os.mkfifo(target)
    with pytest.raises(sandbox.SandboxUnavailableError, match="input"):
        sandbox.open_read_file(target)


def test_bubblewrap_binds_a_copy_not_the_path(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _usable(monkeypatch, "bubblewrap")
    target = tmp_path / "data" / "in.txt"
    target.parent.mkdir()
    target.write_text("INPUT")
    args: list[str] = json.loads(_profile(tmp_path, home, (target,)).read_text().partition("\n")[2])
    binds = [(args[i + 1], args[i + 2]) for i, a in enumerate(args) if a == "--ro-bind"]
    (staged,) = [src for src, dest in binds if dest == str(target)]
    assert staged != str(target) and Path(staged).read_text() == "INPUT"
    assert str(target.parent) not in {dest for _src, dest in binds}
    # a swap after the wall is built moves nothing: the bind names the copy
    target.unlink()
    target.mkdir()
    (target / "secret.txt").write_text("SECRET")
    assert Path(staged).is_file() and Path(staged).read_text() == "INPUT"


def test_seatbelt_grants_the_literal_path_only(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _usable(monkeypatch, "seatbelt")
    target = tmp_path / "data" / "in.txt"
    target.parent.mkdir()
    target.write_text("INPUT")
    text = _profile(tmp_path, home, (target,)).read_text()
    assert f'(literal "{target}")' in text
    assert f'(subpath "{target}")' not in text and f'(subpath "{target.parent}")' not in text


@pytest.mark.parametrize("backend", ["seatbelt", "bubblewrap"])
def test_a_linked_input_refuses_the_wall(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    _usable(monkeypatch, backend)
    (tmp_path / "real").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "real")
    with pytest.raises(sandbox.SandboxUnavailableError, match="link"):
        _profile(tmp_path, home, (tmp_path / "alias",))


def test_an_input_in_the_skep_home_refuses_the_wall(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _usable(monkeypatch, "seatbelt")
    secret = home / "supervisor" / "mail-secret"
    secret.write_text("pw")
    with pytest.raises(sandbox.SandboxUnavailableError, match="skep home"):
        _profile(tmp_path, home, (secret,))


def test_podman_refuses_read_files(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _usable(monkeypatch, "podman")
    target = tmp_path / "in.txt"
    target.write_text("x")
    with pytest.raises(sandbox.SandboxUnavailableError):
        _profile(tmp_path, home, (target,))


@needs_wall
def test_a_dir_swapped_in_after_validation_is_not_readable(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The input passes every check, then becomes a directory before the
    script runs: its files stay out of reach."""
    target = home.parent / "in.txt"
    target.write_text("INPUT")
    real_wrap = sandbox.wrap_command

    def swap_then_wrap(argv: list[str], profile: Path, **kw: object) -> list[str]:
        target.unlink()
        target.mkdir()
        (target / "secret.txt").write_text("SWAPPED-SECRET")
        return real_wrap(argv, profile, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(sandbox, "wrap_command", swap_then_wrap)
    output, _ok = run_schedule_script(
        f"cat {target}; cat {target}/secret.txt; ls {target}", skep_home=home
    )
    assert "SWAPPED-SECRET" not in output and "secret.txt" not in output.split("cat:")[0], output


@needs_wall
def test_a_link_swapped_in_after_the_input_check_refuses(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = home.parent / "in.txt"
    target.write_text("INPUT")
    elsewhere = home.parent / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "secret.txt").write_text("SWAPPED-SECRET")
    declared = scheduler.script_inputs

    def check_then_swap(command: str) -> tuple[Path, ...]:
        found = declared(command)
        target.unlink()
        target.symlink_to(elsewhere)
        return found

    monkeypatch.setattr(scheduler, "script_inputs", check_then_swap)
    output, ok = run_schedule_script(f"cat {target} {target}/secret.txt", skep_home=home)
    assert ok is False and output.startswith("script refused:"), output
    assert "SWAPPED-SECRET" not in output


@needs_wall
def test_a_declared_input_is_still_readable(home: Path) -> None:
    target = home.parent / "in.txt"
    target.write_text("INPUT-OK")
    output, ok = run_schedule_script(
        f"{sys.executable} -c 'print(1)'; cat {target}", skep_home=home
    )
    assert ok and "INPUT-OK" in output, output
