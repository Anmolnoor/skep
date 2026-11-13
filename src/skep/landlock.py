"""R5b: Landlock for a sandboxed worker's unix sockets (docs/sandboxing.md).

Runs INSIDE the sandbox, before the worker:
``python -E -P -m skep.landlock [--scope-abstract] [--unix-root DIR]... -- argv``
sets no_new_privs, restricts itself and execs the worker, which inherits
the restriction. Serve only calls :func:`abi` (when it writes the profile);
it never restricts itself. If restricting fails the worker does not start.

stdlib only (ctypes + libc ``syscall()``): this module executes inside the
sandbox.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
from collections.abc import Sequence

ABI_SCOPE_ABSTRACT = 6  # LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET (Linux 6.12)
ABI_RESOLVE_UNIX = 9  # LANDLOCK_ACCESS_FS_RESOLVE_UNIX

# Landlock came after the syscall tables were unified: one number per call
# on every architecture.
_CREATE, _ADD_RULE, _RESTRICT = 444, 445, 446
_CREATE_RULESET_VERSION = 1 << 0
_RULE_PATH_BENEATH = 1
_SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0
_ACCESS_FS_REFER = 1 << 13
_ACCESS_FS_RESOLVE_UNIX = 1 << 16
_PR_SET_NO_NEW_PRIVS = 38


class _RulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    ]


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _libc() -> ctypes.CDLL:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    return libc


def abi() -> int:
    """The Landlock ABI the running kernel reports; 0 without Landlock (no
    syscall, or disabled at boot) and off Linux."""
    # the running kernel, not sys.platform (tests fake that on macOS)
    if os.uname().sysname != "Linux":
        return 0
    got = _libc().syscall(
        _CREATE, None, ctypes.c_size_t(0), ctypes.c_uint32(_CREATE_RULESET_VERSION)
    )
    return max(int(got), 0)


def _check(result: int, what: str) -> int:
    if result < 0:
        err = ctypes.get_errno()
        raise OSError(err, f"{what}: {os.strerror(err)}")
    return result


def restrict(*, scope_abstract: bool, unix_roots: Sequence[str]) -> None:
    """Restrict this process (and what it execs): abstract sockets made
    outside it, and path sockets outside ``unix_roots`` that it did not
    make, become unconnectable. Raises :class:`OSError` on any failure."""
    if sys.platform != "linux":
        raise OSError(f"Landlock is Linux-only, not {sys.platform}")
    libc = _libc()
    # A ruleset that handles any fs right also denies REFER (cross-directory
    # rename/link: EXDEV) unless handled and granted; bwrap does the
    # filesystem, so REFER is granted everywhere. The kernel still refuses a
    # move that would carry a socket into a root (no rights gain on refer).
    rules = [("/", _ACCESS_FS_REFER)] if unix_roots else []
    rules += [(root, _ACCESS_FS_RESOLVE_UNIX) for root in unix_roots]
    attr = _RulesetAttr(
        _ACCESS_FS_RESOLVE_UNIX | _ACCESS_FS_REFER if unix_roots else 0,
        0,
        _SCOPE_ABSTRACT_UNIX_SOCKET if scope_abstract else 0,
    )
    size = ctypes.c_size_t(ctypes.sizeof(attr))
    ruleset = _check(
        libc.syscall(_CREATE, ctypes.byref(attr), size, ctypes.c_uint32(0)),
        "landlock_create_ruleset",
    )
    try:
        for root, access in rules:
            parent = os.open(root, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = _PathBeneathAttr(access, parent)
                _check(
                    libc.syscall(
                        _ADD_RULE,
                        ctypes.c_long(ruleset),
                        ctypes.c_long(_RULE_PATH_BENEATH),
                        ctypes.byref(rule),
                        ctypes.c_uint32(0),
                    ),
                    f"landlock_add_rule {root}",
                )
            finally:
                os.close(parent)
        zero = ctypes.c_ulong(0)
        _check(
            libc.prctl(_PR_SET_NO_NEW_PRIVS, ctypes.c_ulong(1), zero, zero, zero),
            "prctl(PR_SET_NO_NEW_PRIVS)",
        )
        _check(
            libc.syscall(_RESTRICT, ctypes.c_long(ruleset), ctypes.c_uint32(0)),
            "landlock_restrict_self",
        )
    finally:
        os.close(ruleset)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="skep.landlock")
    parser.add_argument("--scope-abstract", action="store_true")
    parser.add_argument("--unix-root", action="append", default=[])
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- worker argv")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        print("landlock: no worker command given", file=sys.stderr)
        return 2
    try:
        restrict(scope_abstract=args.scope_abstract, unix_roots=args.unix_root)
    except OSError as exc:
        # The kernel reported this ABI when serve wrote the profile: running
        # unscoped now would contradict what the doctor says.
        print(f"landlock: cannot scope this worker, not starting it: {exc}", file=sys.stderr)
        return 126
    os.execvp(command[0], command)
    return 127  # unreachable: execvp replaces the process or raises


if __name__ == "__main__":
    raise SystemExit(main())
