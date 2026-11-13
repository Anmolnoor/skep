"""R5b: the Landlock step a sandboxed worker runs first (docs/sandboxing.md).

The syscalls are faked here; tests/supervisor/test_sandbox_bwrap_sockets.py
proves them against the real kernel.
"""

from __future__ import annotations

import ctypes
import os
import sys
from typing import Any

import pytest

from skep import landlock


class FakeLibc:
    def __init__(self, results: dict[int, int] | None = None) -> None:
        self.results = results or {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def syscall(self, number: int, *args: Any) -> int:
        self.calls.append(("syscall", (number, *args)))
        if number in self.results:
            return self.results[number]
        # a ruleset is a real fd restrict() closes: hand it a throwaway one
        return os.open(os.devnull, os.O_RDONLY) if number == landlock._CREATE else 0

    def prctl(self, *args: Any) -> int:
        self.calls.append(("prctl", args))
        return self.results.get(-1, 0)


class _Uname:
    def __init__(self, sysname: str) -> None:
        self.sysname = sysname


@pytest.fixture()
def libc(monkeypatch: pytest.MonkeyPatch) -> FakeLibc:
    fake = FakeLibc()
    monkeypatch.setattr(landlock, "_libc", lambda: fake)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "uname", lambda: _Uname("Linux"))
    return fake


def _value(arg: Any) -> Any:
    return getattr(arg, "value", arg)


def test_abi_is_what_the_kernel_reports(libc: FakeLibc) -> None:
    libc.results[landlock._CREATE] = 10
    assert landlock.abi() == 10
    (_, (number, attr, size, flags)), *_ = libc.calls
    assert (number, attr, _value(size), _value(flags)) == (landlock._CREATE, None, 0, 1)


def test_no_landlock_or_landlock_off_is_abi_zero(libc: FakeLibc) -> None:
    libc.results[landlock._CREATE] = -1  # ENOSYS, or EOPNOTSUPP when disabled at boot
    assert landlock.abi() == 0


def test_abi_is_zero_off_linux_without_calling_anything(
    libc: FakeLibc, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The kernel decides, not sys.platform: the bwrap argv tests fake
    # sys.platform == "linux" on macOS, where syscall 444 is not Landlock.
    monkeypatch.setattr(os, "uname", lambda: _Uname("Darwin"))
    assert landlock.abi() == 0
    assert libc.calls == []


def test_restrict_scopes_abstract_sockets_and_resolves_unix_only_under_the_roots(
    libc: FakeLibc, tmp_path: Any
) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    landlock.restrict(scope_abstract=True, unix_roots=[str(root)])
    kinds = [(kind, _value(args[0])) for kind, args in libc.calls]
    assert kinds == [
        ("syscall", landlock._CREATE),
        ("syscall", landlock._ADD_RULE),
        ("prctl", landlock._PR_SET_NO_NEW_PRIVS),
        ("syscall", landlock._RESTRICT),
    ]
    attr = libc.calls[0][1][1]._obj
    assert (attr.handled_access_fs, attr.handled_access_net, attr.scoped) == (
        landlock._ACCESS_FS_RESOLVE_UNIX,
        0,
        landlock._SCOPE_ABSTRACT_UNIX_SOCKET,
    )
    rule = libc.calls[1][1][3]._obj
    assert rule.allowed_access == landlock._ACCESS_FS_RESOLVE_UNIX
    assert ctypes.sizeof(rule) == 12  # the kernel's struct is packed
    assert [_value(a) for a in libc.calls[2][1]] == [landlock._PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0]


def test_restrict_with_scope_only_handles_no_filesystem_right(libc: FakeLibc) -> None:
    landlock.restrict(scope_abstract=True, unix_roots=[])
    attr = libc.calls[0][1][1]._obj
    assert (attr.handled_access_fs, attr.scoped) == (0, landlock._SCOPE_ABSTRACT_UNIX_SOCKET)
    assert landlock._ADD_RULE not in [_value(args[0]) for _, args in libc.calls]


def test_restrict_raises_when_the_kernel_refuses(libc: FakeLibc) -> None:
    libc.results[landlock._RESTRICT] = -1
    with pytest.raises(OSError, match="landlock_restrict_self"):
        landlock.restrict(scope_abstract=True, unix_roots=[])


def test_main_execs_the_worker_once_scoped(
    libc: FakeLibc, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    execs: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(os, "execvp", lambda f, a: execs.append((f, a)))
    landlock.main(["--scope-abstract", "--unix-root", str(tmp_path), "--", "worker", "-x"])
    assert execs == [("worker", ["worker", "-x"])]
    assert landlock._RESTRICT in [_value(args[0]) for _, args in libc.calls]


def test_main_fails_closed_and_never_execs_unscoped(
    libc: FakeLibc, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    libc.results[landlock._CREATE] = -1
    monkeypatch.setattr(os, "execvp", lambda *a: pytest.fail("ran unscoped"))
    assert landlock.main(["--scope-abstract", "--", "worker"]) == 126
    assert "landlock_create_ruleset" in capsys.readouterr().err


def test_main_needs_a_command(libc: FakeLibc) -> None:
    assert landlock.main(["--scope-abstract"]) == 2
    assert libc.calls == []
