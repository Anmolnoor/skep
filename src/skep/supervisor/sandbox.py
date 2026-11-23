"""Native sandbox enforcement for worker processes.

macOS uses Seatbelt (``sandbox-exec``) with generated SBPL. Linux uses
Bubblewrap (``bwrap``) with a read-only root, explicit writable binds for the
workspace/results, and an optional network namespace for deny-all networking.

The sandbox is built around a network allowlist (decision D1):
:class:`NetworkPolicy` carries a domain list and ``()`` means deny all. Seatbelt
can pin concrete domain lists to skep's loopback filtering proxy
(``netproxy.py``). Bubblewrap can safely enforce deny-all and allow-all in this
first Linux slice; concrete domain allowlists fail closed until skep owns an
enforceable Linux proxy namespace path.

Strength of the boundary (stated honestly): the sandbox physically enforces
(1) no outbound network for deny-all runs and (2) no writes outside the
workspace/results/temp boundary. Reads are left open so the Python/git/test
toolchain keeps working — except the secret floor (ES-F4, V3): the operator's
credential dirs and skep's own serve-token/serve.env/``*-secret`` files are
hidden on every backend (``secret_paths``). Env secrets are closed by the G2
env allowlist. A strict-read project (ADR 0068) reads an allowlist instead:
bwrap's empty root with the system dirs, the detected toolchains and the
run's own roots bound back; on Seatbelt a global read deny reopening the
same set. The masks still land last.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from fnmatch import fnmatch
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .. import landlock
from .secret_floor import HOME_SECRETS, SERVE_ENV, SKEP_SECRET_NAMES, alias_key, other_homes
from .store import RETIRED_STORE_FILES

logger = logging.getLogger(__name__)

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
BUBBLEWRAP = "bwrap"
PODMAN = "podman"
_BUBBLEWRAP_HEADER = "# bubblewrap argv"
_PODMAN_HEADER = "# podman argv"

# /dev nodes the toolchain writes to (subprocess DEVNULL, tracing); re-allowed
# individually after the blanket write deny.
_DEV_WRITE_NODES = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/dtracehelper", "/dev/tty")


class SandboxUnavailableError(Exception):
    """Sandboxing was requested but no native backend is available here."""


class SandboxAllowlistUnsupported(Exception):
    """A per-domain allowlist was requested where the backend cannot enforce it."""


@dataclass(frozen=True)
class SandboxAvailability:
    """Whether this process can actually apply the host sandbox backend."""

    usable: bool
    reason: str | None = None
    detail: str | None = None
    backend: str | None = None


@dataclass(frozen=True)
class NetworkPolicy:
    """Outbound network grant, shaped as a domain allowlist (D1).

    ``allow=()`` denies all outbound (the default). ``allow=("*",)`` allows all
    outbound (the manual real-provider path, which must reach a live LLM). A list
    of concrete domains (``("pypi.org", ...)``) is enforced in v3 by pinning the
    sandbox to a loopback filtering proxy (see ``netproxy.py``) — pass the proxy's
    port to ``build_profile``/``write_profile``.
    """

    allow: tuple[str, ...] = ()

    @property
    def is_deny_all(self) -> bool:
        return len(self.allow) == 0

    @property
    def is_allow_all(self) -> bool:
        return self.allow == ("*",)

    @property
    def is_domain_list(self) -> bool:
        return not self.is_deny_all and not self.is_allow_all


DENY_ALL_NETWORK = NetworkPolicy(allow=())
ALLOW_ALL_NETWORK = NetworkPolicy(allow=("*",))


@lru_cache(maxsize=4)
def availability(backend: str | None = None) -> SandboxAvailability:
    """Return whether this process can enforce a sandbox profile.

    ``backend=None`` (the default everywhere) probes the NATIVE host backend:
    ``sandbox-exec`` may exist but still be unusable from a restricted parent
    process; on such hosts it exits immediately with ``sandbox_apply: Operation
    not permitted``. Treat that as unavailable so worker launch does not become
    a misleading crash before the worker starts. Linux follows the same rule for
    Bubblewrap: the binary must exist and successfully run a deny-all probe.
    ``backend="podman"`` (v44-F7) probes the container backend the same way —
    the probe runs the actual overlay-rootfs invocation, so hosts where
    rootless overlay-of-/ cannot work report unusable instead of failing at
    worker launch.
    """
    if backend == PODMAN:
        return _podman_availability()
    if backend is not None:
        return SandboxAvailability(False, "unknown_backend", f"backend={backend!r}")
    if sys.platform == "darwin":
        return _seatbelt_availability()
    if sys.platform.startswith("linux"):
        return _bubblewrap_availability()
    return SandboxAvailability(False, "unsupported_platform", f"platform={sys.platform!r}")


def _seatbelt_availability() -> SandboxAvailability:
    if not Path(SANDBOX_EXEC).exists():
        return SandboxAvailability(False, "missing_binary", f"{SANDBOX_EXEC} does not exist")
    # ES-F6 (B48): probe with the shape workers get. Under a parent sandbox
    # "(allow default)" alone applies while a profile carrying deny rules
    # dies (rc=71) — the probe said usable and every worker crashed instead.
    # No writable root: /usr/bin/true needs none, and a host with no usable
    # temp dir must still get probe_rejected, not a FileNotFoundError.
    profile = build_profile(writable_roots=[])
    try:
        proc = subprocess.run(
            [SANDBOX_EXEC, "-p", profile, "/usr/bin/true"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return SandboxAvailability(False, "probe_failed", str(exc))
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        return SandboxAvailability(False, "probe_rejected", detail)
    return SandboxAvailability(True, backend="seatbelt")


def _bubblewrap_binary() -> str | None:
    return shutil.which(BUBBLEWRAP)


# ES-F2 (B50 + B52): the walls every bwrap invocation carries, probe included.
# --tmpfs /run: a read-only bind does not stop connect() on a unix socket and
# path sockets ignore the netns, so /run/user/$UID/bus and systemd/private
# let `systemd-run --user` start a process on the HOST (fedora-p1-04).
# --unshare-pid (+ the fresh --proc): the worker no longer sees or signals
# serve and its siblings; pid 1 of the namespace dying also reaps a setsid'd
# grandchild that killpg alone used to miss. --die-with-parent ties the tree
# to bwrap; --new-session blocks TIOCSTI keystroke injection into a terminal;
# --unshare-ipc keeps SysV IPC / mqueues private.
_HOST_ROOT = ("--ro-bind", "/", "/")
_BUBBLEWRAP_WALLS = (
    "--dev",
    "/dev",
    "--unshare-pid",
    "--unshare-ipc",
    "--proc",
    "/proc",
    "--tmpfs",
    "/tmp",
    "--tmpfs",
    "/run",
    "--die-with-parent",
    "--new-session",
)
_BUBBLEWRAP_BASE_ARGS = (*_HOST_ROOT, *_BUBBLEWRAP_WALLS)

# ADR 0068: strict read confinement. The backends that enforce it.
STRICT_READ_BACKENDS = frozenset({"bubblewrap", "seatbelt"})
STRICT_READS_KEY = "strict_read_confinement"
# The empty root's system half (bwrap): a usr-merge link is recreated as a
# link, a real directory is bound read-only, an absent one skipped.
_SYSTEM_ROOTS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/lib32", "/libx32", "/opt", "/nix")
# /etc comes back entry by entry, minus these: the host's auth secrets and
# Proxmox's cluster tree (never mounted over: a FUSE mount we may not stat).
_ETC_HIDDEN = frozenset(
    {"pve", "shadow", "shadow-", "gshadow", "gshadow-", "sudoers", "sudoers.d", "ssh"}
)
_SITE_DIRS = ("site-packages", "dist-packages")
# Per-user toolchains: (the variable that moves one, its default under $HOME).
_HOME_TOOLCHAINS = (
    (None, ".local/bin"),  # uv/uvx and the claude launcher (their installers' default)
    ("UV_PYTHON_INSTALL_DIR", ".local/share/uv/python"),
    ("UV_TOOL_DIR", ".local/share/uv/tools"),
    (None, ".local/share/claude"),  # the claude_code engine's versions
    ("CARGO_HOME", ".cargo"),
    ("RUSTUP_HOME", ".rustup"),
    ("NVM_DIR", ".nvm"),
    ("PYENV_ROOT", ".pyenv"),
)


def read_allowlist_for(policy: Mapping[str, Any]) -> tuple[Path, ...] | None:
    """ADR 0068: None keeps a project's reads denylist-only; a strict
    project's ``extra_read_paths`` (often none) turn the allowlist on."""
    if policy.get(STRICT_READS_KEY) is not True:
        return None
    return tuple(Path(path) for path in policy.get("extra_read_paths") or ())


def resolved_read_allowlist(paths: Iterable[Path], skep_home: Path) -> tuple[Path, ...]:
    """ADR 0068 (G1): each extra path judged, then resolved ONCE at dispatch.
    The run records these targets, so a link the operator retargets later
    never moves the wall G10 builds. Refuses like ``write_profile`` does.
    3I: only the ones that exist, as the worker's profile binds them — one
    made after dispatch is never in G10's wall."""
    paths = _validated_extra(paths, skep_home)
    return tuple(dict.fromkeys(path.resolve() for path in paths if _exists(path)))


def strict_read_roots(
    skep_home: Path, *, worker_llm_key: bool = False, extra: Iterable[Path] = ()
) -> list[Path]:
    """ADR 0068: what a strict run reads beyond the OS's system roots and its
    own workspace/git roots — the half every backend's profile shares.

    The detected toolchains (skep's own interpreter and package, the usual
    per-user installs, ``<skep home>/tools/.venvs``), the skep-home files the
    builtin worker reads (``profile.json``, ``worker_plugins``; the store and
    its own LLM key only with ``worker_llm_key``) and the operator's
    ``extra_read_paths``. Resolved, existing paths only. A detected root at
    or above the home, on /etc's reserved entries, or (a per-user toolchain)
    itself a link, is dropped, as is a skep-home root reached through a link
    (G2); an extra path at or above the home, in or over
    the skep home, or on /etc's reserved entries refuses the run
    (``extra_read_path_problem``). The secret masks still land after every one."""
    roots = _strict_candidates(skep_home, worker_llm_key=worker_llm_key, extra=extra)
    return list(dict.fromkeys(path.resolve() for path in roots))


def _in_place(path: Path, skep_home: Path) -> bool:
    """G2: a skep-home root is bound only where it sits in the home, never
    through a link of its own or a linked parent — a ``worker_plugins``
    linked to ~/Downloads would bind the downloads, past every broad-root
    check. A relocated shelf is named in ``extra_read_paths``, which are."""
    real = skep_home.resolve() / path.relative_to(skep_home)
    if os.path.realpath(real) == str(real):
        return True
    logger.warning(
        "strict reads: %s is reached through a link (to %s), so it is not bound; name the "
        "target in the project's extra_read_paths if the run needs it",
        path,
        os.path.realpath(real),
    )
    return False


def _strict_candidates(
    skep_home: Path, *, worker_llm_key: bool, extra: Iterable[Path]
) -> list[Path]:
    """``strict_read_roots`` as written (absolute, not resolved): bwrap
    recreates a written path that is a link (uv's ``cpython-3.12-…`` minor
    version link, which a venv's python points through) as a symlink to the
    resolved root, so the masks on the resolved path still hold."""
    home = Path.home().resolve()
    detected = [
        Path(sys.prefix),
        Path(sys.base_prefix),
        Path(sys.exec_prefix),
        Path(__file__).resolve().parents[2],
        # what .pth files chain in (uv's `run --with` overlay env, an editable install)
        *(Path(entry) for entry in sys.path if Path(entry).name in _SITE_DIRS),
    ]
    toolchains = [
        Path(os.environ.get(env) or home / rel) if env else home / rel
        for env, rel in _HOME_TOOLCHAINS
    ]
    for root in toolchains:
        # W2: bound where it is, never through a link of its own — a
        # ~/.local/bin linked to ~/Downloads would bind the downloads. The
        # operator follows such a link on purpose with extra_read_paths.
        if root.is_symlink():
            logger.warning(
                "strict reads: %s is a link (to %s), so it is not bound; name it in the "
                "project's extra_read_paths if the run needs it",
                root,
                root.resolve(),
            )
        else:
            detected.append(root)
    supervisor = skep_home / "supervisor"
    own = [skep_home / "profile.json", skep_home / "worker_plugins", skep_home / "tools" / ".venvs"]
    if worker_llm_key:
        names = (*_LIVE_STORE_FILES, "llm-secret", *_active_llm_key_names(supervisor))
        own += [supervisor / name for name in names]
    own = [path for path in own if _in_place(path, skep_home)]
    extra = _validated_extra(extra, skep_home)
    # R4 (Sol R3-2): ``extra`` is the run's RECORDED wall (resolved, existing at
    # dispatch). One that vanished since must not just drop out of this
    # profile — it could return before G10 and widen G10's reads past the
    # worker's. Refuse instead (the worker: sandbox_unavailable; G10: unavailable).
    if gone := [str(path) for path in extra if not _exists(path)]:
        raise SandboxUnavailableError(
            f"recorded extra read root(s) {', '.join(gone)} vanished after dispatch; "
            "refusing to build a narrower wall than the run recorded — re-dispatch the run"
        )
    detected = [Path(os.path.abspath(root)) for root in detected]
    found = [*(root for root in detected if _detected_root_ok(root.resolve(), home)), *own]
    # R5 (Sol): recorded extras are NOT re-filtered — one gone after the check
    # above makes the bind fail closed instead of narrowing the worker's wall.
    return list(dict.fromkeys([*(path for path in found if _exists(path)), *extra]))


def _etc_reserved(real: Path) -> bool:
    """/etc, a dir that holds it (3I: /private on macOS, with the host's /tmp
    and /var), or one of its hidden entries, by either OS's list and every
    spelling macOS opens it by (/private/etc, case, the data volume):
    binding any of them reopens them."""
    key, etc = alias_key(real), alias_key(Path("/etc").resolve())
    return etc.is_relative_to(key) or any(key.is_relative_to(etc / n) for n in _ALL_ETC_HIDDEN)


def _over(real: Path, inner: Path) -> bool:
    """``real`` is ``inner`` or above it, by any spelling APFS opens."""
    return alias_key(inner).is_relative_to(alias_key(real))


def _detected_root_ok(real: Path, home: Path) -> bool:
    """W2/W11: never the home or above it, never /etc's reserved entries."""
    return not _over(real, home) and not _etc_reserved(real)


def extra_read_path_problem(path: Path, skep_home: Path) -> str | None:
    """ADR 0068 (W2, W11): why ``path`` cannot be an extra read root, judged
    where it resolves — at or above the home (``/`` included), in or over the
    skep home, /etc or its reserved entries — or None."""
    real = Path(path).resolve()
    home = Path.home().resolve()
    skep = skep_home.resolve()
    if _over(real, home):
        return (
            f"{path} resolves to {real}, at or above the home {home}: that reopens every file "
            f"strict reads close. Name a subdirectory instead, e.g. {home / 'datasets'}"
        )
    if _over(skep, real) or _over(real, skep):
        return (
            f"{path} resolves to {real}, in or over the skep home {skep} (other runs' "
            "worktrees, results and audit trails). Name a directory outside it"
        )
    if _etc_reserved(real):
        return (
            f"{path} resolves to {real}: /etc comes back without "
            f"{', '.join(sorted(_ALL_ETC_HIDDEN))}, and no extra path reopens them or a "
            "dir that holds /etc (on macOS, /private holds the host's /tmp and /var too)"
        )
    if sys.platform == "darwin" and (
        var := next((v for v in _HOMEBREW_VAR if _over(Path(v), real)), None)
    ):  # R4 (Sol): bwrap honours these; only Seatbelt masks them
        return (
            f"{path} resolves to {real}, in Homebrew's var/ ({var}): a strict run on macOS "
            "masks it (local databases and service state), so the path would read as "
            "missing. Name a directory outside it"
        )
    return None


def _validated_extra(extra: Iterable[Path], skep_home: Path) -> list[Path]:
    """The extra paths, absolute, each checked before anything walks or binds
    it (W13: a link to / used to walk the whole host first) — or refused."""
    paths = [Path(os.path.abspath(path)) for path in extra]
    for path in paths:
        if problem := extra_read_path_problem(path, skep_home):
            raise SandboxUnavailableError(
                f"extra_read_paths: {problem}. The run is refused rather than run with "
                "that read open; change the project's extra_read_paths."
            )
    return paths


def _alias_links(paths: Iterable[Path], bound: Iterable[Path]) -> list[str]:
    """A written root that is a link becomes a link to its resolved root in
    bwrap's empty root — unless a bound root already holds it (the link is
    there, read-only). ponytail: only a link in a real parent — one under a
    linked parent dir (Fedora Atomic's /home) stays unreachable by that name."""
    bound = [Path(root) for root in bound]
    args: list[str] = []
    for path in paths:
        real = path.resolve()
        if (
            real != path
            and path.parent.resolve() == path.parent
            and not any(path.is_relative_to(root) for root in bound)
        ):
            args += ["--symlink", str(real), str(path)]
    return args


# ADR 0068 on Seatbelt: what a strict run reads of macOS itself. Of
# /private/var only sh's and xcode-select's links, the zone files
# /etc/localtime points into and resolv.conf; /private/etc minus its auth
# entries (masked last, after every allow).
_SEATBELT_SYSTEM_ROOTS = (
    "/usr", "/System", "/Library", "/opt/homebrew", "/bin", "/sbin", "/dev", "/private/etc",
    "/private/var/select", "/private/var/db/timezone", "/private/var/run/resolv.conf",
)  # fmt: skip
_SEATBELT_ETC_HIDDEN = ("master.passwd", "sudoers", "sudoers.d", "ssh", "kcpassword")
_ALL_ETC_HIDDEN = _ETC_HIDDEN | set(_SEATBELT_ETC_HIDDEN)
# M8: Homebrew's (Apple silicon, Intel) local databases and service state,
# under roots reopened whole; masked last with the /etc entries.
_HOMEBREW_VAR = ("/opt/homebrew/var", "/usr/local/var")
# /usr/bin/git is xcrun's shim: it runs git from the selected developer dir
_XCODE_SELECT_LINKS = ("/private/var/select/developer_dir", "/private/var/db/xcode_select_link")


def _seatbelt_read_rules(roots: Iterable[Path]) -> list[str]:
    """ADR 0068: deny every read, then reopen macOS's system roots, the
    developer dir (the whole Xcode.app: xcrun stats its Info.plist) and
    ``roots``; metadata alone on each one's ancestors, so getcwd/realpath
    resolve without listing anything, and on a /private root's /etc, /var or
    /tmp link spelling (libc stats /etc/localtime's /var/db/timezone target
    by that name). ``/`` itself is read whole: dyld aborts every process
    without it. A denied path is ENOENT, as under bwrap's empty root — git
    dies on an EPERM ~/.gitconfig, not on a missing one.
    /System/Volumes is closed again last (M4): /System reopened it, with the
    data volume's entries no firmlink maps back to the root."""
    allowed = [Path(root) for root in _SEATBELT_SYSTEM_ROOTS]
    for link in _XCODE_SELECT_LINKS:
        if os.path.islink(link):
            dev = Path(link).resolve()
            # M10: the link itself too — xcrun reads the selection through it
            allowed += [Path(link), dev.parents[1] if dev.parent.name == "Contents" else dev]
    allowed += [Path(root).resolve() for root in roots]
    allowed = list(dict.fromkeys(allowed))
    links = [
        Path("/", *root.parts[2:])
        for root in allowed
        if root.parts[1:2] == ("private",) and root.parts[2:3] in (("etc",), ("var",), ("tmp",))
    ]
    named = [*allowed, *links]
    ancestors = sorted({parent for root in named for parent in root.parents} - {Path("/")})

    rules = [
        "(deny file-read* file-test-existence (with errno ENOENT))",
        '(allow file-read* file-test-existence (literal "/"))',
    ]
    for ops, kind, paths in (
        ("file-read-metadata", "literal", ancestors),
        ("file-read-metadata", "subpath", links),
        ("file-read*", "subpath", allowed),
    ):
        if paths:  # an allow with no filter would reopen every path
            filters = " ".join(f'({kind} "{_sbpl_quote(path)}")' for path in paths)
            rules.append(f"(allow {ops} file-test-existence {filters})")
    rules.append(
        '(deny file-read* file-test-existence (subpath "/System/Volumes") (with errno ENOENT))'
    )
    return rules


def _strict_root_args() -> list[str]:
    """ADR 0068: the system half of bwrap's empty root, read-only."""
    paths = [*_SYSTEM_ROOTS, *(f"/etc/{name}" for name in sorted(os.listdir("/etc")))]
    args: list[str] = []
    for path in paths:
        if path.removeprefix("/etc/") in _ETC_HIDDEN:
            continue
        if os.path.islink(path):
            args += ["--symlink", os.readlink(path), path]
        elif os.path.exists(path):
            args += ["--ro-bind", path, path]
    return args


def _run_rebinds() -> list[str]:
    """Bind back the one /run file the toolchain needs: a resolv.conf that is
    a symlink into /run (systemd-resolved on Fedora/Ubuntu), else DNS dies in
    allow-all runs. Nothing else under /run is a worker's business."""
    target = os.path.realpath("/etc/resolv.conf")
    if target.startswith("/run/") and os.path.isfile(target):
        return ["--ro-bind", target, target]
    return []


def _bubblewrap_probe_argv(binary: str) -> list[str]:
    return [binary, *_BUBBLEWRAP_BASE_ARGS, "--unshare-net", "/usr/bin/true"]


def _bubblewrap_availability() -> SandboxAvailability:
    binary = _bubblewrap_binary()
    if binary is None:
        return SandboxAvailability(False, "missing_binary", "bwrap was not found on PATH")
    try:
        proc = subprocess.run(
            _bubblewrap_probe_argv(binary),
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return SandboxAvailability(False, "probe_failed", str(exc))
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        return SandboxAvailability(False, "probe_rejected", detail)
    return SandboxAvailability(True, backend="bubblewrap")


def _podman_binary() -> str | None:
    return shutil.which(PODMAN)


# The container root is a fabricated SKELETON (empty mountpoint dirs + the
# usual /bin -> /usr/bin merge symlinks), run as a throwaway overlay, with the
# host toolchain RO-bind-mounted in — bwrap's read-only-root semantics rebuilt
# in podman terms. Overlaying the host / itself is NOT viable rootless (kernel
# overlayfs refuses upperdir-inside-lowerdir; fuse-overlayfs breaks crun's
# proc mount), which the probe would have reported honestly.
_PODMAN_SKELETON_DIRS = (
    "usr",
    "etc",
    "home",
    "var",
    "opt",
    "proc",
    "sys",
    "dev",
    "tmp",
    "run",
    "root",
)
_PODMAN_SKELETON_LINKS = {
    "bin": "usr/bin",
    "sbin": "usr/sbin",
    "lib": "usr/lib",
    "lib64": "usr/lib64",
}
_PODMAN_RO_BINDS = ("/usr", "/etc", "/home", "/var", "/opt")


def _podman_rootfs_skeleton(base: Path) -> Path:
    root = base / "podman-rootfs"
    for name in _PODMAN_SKELETON_DIRS:
        (root / name).mkdir(parents=True, exist_ok=True)
    for link, target in _PODMAN_SKELETON_LINKS.items():
        path = root / link
        if not path.is_symlink():
            path.symlink_to(target)
    return root


def _podman_ro_bind_args() -> list[str]:
    args: list[str] = []
    for bind in _PODMAN_RO_BINDS:
        if Path(bind).is_dir():
            args.extend(["-v", f"{bind}:{bind}:ro"])
    return args


def _podman_availability() -> SandboxAvailability:
    binary = _podman_binary()
    if binary is None:
        return SandboxAvailability(False, "missing_binary", "podman was not found on PATH")
    with tempfile.TemporaryDirectory(prefix="skep-podman-probe-") as tmp:
        # ES-F7 (B53): a real round trip through a writable mount, with the
        # exact argv workers get. Running /usr/bin/true with nothing mounted
        # said "usable" on SELinux hosts where the worker could not even read
        # its own worktree.
        work = Path(tmp) / "work"
        work.mkdir()
        (work / "probe-in").write_text("skep\n", encoding="utf-8")
        argv = [
            binary,
            *_podman_args(
                writable_roots=[work],
                network=DENY_ALL_NETWORK,
                rootfs=_podman_rootfs_skeleton(Path(tmp)),
            ),
            "/bin/sh",
            "-c",
            "cat probe-in > probe-out",
        ]
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,  # image-less, but rootless storage setup can be slow
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return SandboxAvailability(False, "probe_failed", str(exc), backend=PODMAN)
        round_trip = (work / "probe-out").is_file() and (
            (work / "probe-out").read_text(encoding="utf-8") == "skep\n"
        )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        return SandboxAvailability(False, "probe_rejected", detail, backend=PODMAN)
    if not round_trip:
        return SandboxAvailability(
            False,
            "probe_rejected",
            "the container started but could not read and write its workspace mount",
            backend=PODMAN,
        )
    return SandboxAvailability(True, backend=PODMAN)


def require_usable(backend: str | None = None) -> SandboxAvailability:
    """The probe result, or :class:`SandboxUnavailableError` naming why not."""
    probe = availability(backend)
    if not probe.usable:
        detail = f": {probe.detail}" if probe.detail else ""
        raise SandboxUnavailableError(
            f"sandbox enforcement requested but no usable sandbox backend is available "
            f"(reason={probe.reason!r}, platform={sys.platform!r}){detail}. "
            "Fix the backend (`skep doctor`) or run in execution mode 'workspace'; "
            "skep never runs a sandbox run unconfined."
        )
    return probe


def available() -> bool:
    """True when this process can enforce the native sandbox backend."""
    return availability().usable


# ES-F4 (B05, V3 sandbox half): paths no worker may read, whatever its
# backend. The operator's credential dirs, plus skep's own control-plane
# secrets: serve-token (the whole REST API), serve.env (engine auth), every
# ``*-secret`` and ``llm-secret*`` file (secret_floor.SKEP_SECRET_NAMES), the
# other runs' audit trails and the chat attachments (B125). One carve-out:
# a worker whose caste calls the LLM reads ``llm-secret`` and the ACTIVE
# profile's ``llm-secret-<id>`` (llm_plan.py) unless its run is relayed
# (ADR 0062, ``provider_key_relay``): then the proxy holds the credential.
# The store (supervisor.sqlite3) stays readable for that same worker only: it
# reads its provider settings from it. v127-F6: every other profile (external
# engines, no-provider castes, relayed runs, re-verify, diagnose) has the store
# and its WAL masked — it holds every chat and run instruction — and nobody
# reads the backup dirs or a retired layout's store. Checked when the profile is
# built, so a secret file created mid-run is not covered (relay-secret is
# made before the list, ES-F264) — nor, under bwrap/podman, a store WAL/SHM
# that appears after it (they mask only paths that exist; serve normally
# holds them open). Seatbelt denies by literal path, so write_profile names
# the live store and its WAL/SHM whether or not they exist yet.
# W33: the engine logins (~/Library/Keychains, ~/.claude/.credentials.json,
# ~/.codex/auth.json) are masked too — each external engine's config dir is
# redirected per run and a dispatch without its auth env is refused (NO AUTH
# ENV), so no run authenticates from them.
SANDBOX_READABLE_SECRETS: dict[str, str] = {
    ".npmrc": "npm install reads registry auth from it",
}
_HOME_SECRET_PATHS = tuple(rel for rel in HOME_SECRETS if rel not in SANDBOX_READABLE_SECRETS)

# R5 (D19): a read-only bind does not stop connect(), so bwrap's ``--ro-bind
# / /`` left an agent socket outside /tmp and /run reachable. These hold one
# (relative to every home; ~/.ssh and ~/.gnupg are already in HOME_SECRETS).
# Hidden like a secret on every backend; Seatbelt refuses the connect anyway
# (W34). ponytail: known places only — on a kernel below Landlock ABI 9 a
# socket at a path nobody listed stays reachable (R5b, socket_gap).
AGENT_SOCKETS: tuple[str, ...] = (
    ".1password",  # 1Password SSH agent: agent.sock
    ".bitwarden-ssh-agent.sock",
    ".docker/desktop",  # Docker Desktop for Linux: docker.sock
    ".docker/run",
    ".colima",
    ".lima",
    ".rd/docker.sock",  # Rancher Desktop (its bin/ holds kubectl, helm: on PATH)
    ".orbstack/run",
    ".local/share/containers/podman/machine",
)
# R5 (I12): what each Linux backend alone cannot close, said wherever the
# wall is described (doctor's sandbox line, docs/sandboxing.md). A podman run
# always gets its own network namespace, so no abstract socket reaches it.
_UNLISTED_SOCKET = {
    "bubblewrap": "a unix socket at a path outside /tmp and /run that the mask list does not name",
    PODMAN: "a unix socket under the host dirs it binds (/home /var /etc /usr /opt) at a path "
    "the mask list does not name",
}


def socket_gap(backend: str) -> str:
    """R5b: the unix sockets a worker on ``backend`` still reaches under this
    kernel's Landlock ABI (ABI 6 scopes abstract sockets, ABI 9 path
    sockets); "" for a backend with no gap (Seatbelt, W34)."""
    if backend not in _UNLISTED_SOCKET:
        return ""
    abi = landlock.abi()
    if abi >= landlock.ABI_RESOLVE_UNIX:
        return (
            f"Landlock ABI {abi}: a worker connects only to unix sockets in its own "
            "roots or ones it made itself."
        )
    path = _UNLISTED_SOCKET[backend]
    if backend == PODMAN:
        return (
            f"Not walled on podman: any run can connect to {path} "
            f"(path sockets need Landlock ABI 9; this kernel has {abi})."
        )
    if abi >= landlock.ABI_SCOPE_ABSTRACT:
        return (
            f"Landlock ABI {abi} refuses abstract unix sockets made outside the worker. "
            f"Not walled on Linux: any run can connect to {path} "
            "(path sockets need Landlock ABI 9)."
        )
    return (
        "Not walled on Linux: an allow-all run can connect to abstract unix sockets "
        f"(session D-Bus, X11), and any run to {path}. Landlock is off or too old here "
        f"(ABI {abi}; 6 scopes abstract sockets, 9 path sockets)."
    )


# R5: wherever the operator's own agents listen (serve's environment).
_SOCKET_ENV = ("SSH_AUTH_SOCK", "DOCKER_HOST", "CONTAINER_HOST")


def _env_sockets() -> list[Path]:
    """The unix sockets ``$SSH_AUTH_SOCK`` and a ``unix://`` docker/podman
    host name — only real sockets, so a stray value can never mask a dir."""
    found = []
    for name in _SOCKET_ENV:
        value = os.environ.get(name, "").removeprefix("unix://")
        if value.startswith("/") and _is_socket(Path(value)):
            found.append(Path(value))
    return found


def _sockets_under(roots: Iterable[Path]) -> list[Path]:
    """R5: the sockets inside a read-only root (git's fsmonitor socket in the
    common dir): bound read-only they stay connectable, so each gets
    /dev/null laid over it. ponytail: ``objects/`` is skipped — git keeps
    only object files there, and it is the one big subtree."""
    found = [Path(root) for root in roots if _is_socket(Path(root))]  # a root that IS one
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name != "objects"]
            found += [path for name in filenames if _is_socket(path := Path(dirpath, name))]
    return found


def _is_socket(path: Path) -> bool:
    try:
        return path.is_socket()
    except OSError:
        return False


def _refuse_home_inside(skep_home: Path, writable_roots: Iterable[Path]) -> None:
    """ES-F5 (B46): a skep home under a worker-writable root hands the worker
    the store, the baseline and the token — refuse the run, name the fix."""
    home = skep_home.resolve()
    for root in writable_roots:
        if home.is_relative_to(Path(root).resolve()):
            raise SandboxUnavailableError(
                f"the skep home {home} lies inside the worker-writable root {root}: a "
                "sandboxed worker could rewrite the store, baseline and serve token. "
                "Move the home (SKEP_HOME / --home) out of that directory."
            )


_LIVE_STORE_FILES = ("supervisor.sqlite3", "supervisor.sqlite3-wal", "supervisor.sqlite3-shm")


def secret_paths(skep_home: Path, *, worker_llm_key: bool = False) -> list[Path]:
    """The existing secret-floor paths to hide from a worker (resolved).

    ``skep_home`` is the TOP-LEVEL home (``~/.skep``), not the supervisor
    dir secret_floor takes (B148)."""
    home = Path.home()
    supervisor = skep_home / "supervisor"
    candidates = [home / rel for rel in (*_HOME_SECRET_PATHS, *AGENT_SOCKETS)]
    # W40: every other home's credential files, its engine logins included
    own = {home.resolve(), Path(os.path.abspath(home))}
    candidates += [
        other / rel
        for other in other_homes()
        if other.resolve() not in own
        for rel in (*HOME_SECRETS, *AGENT_SOCKETS)
    ]
    candidates += _env_sockets()
    # W3: the credentials where $CARGO_HOME moved them — the root the strict
    # wall binds whole (the default ~/.cargo ones are in HOME_SECRETS).
    cargo = Path(os.environ.get("CARGO_HOME") or home / ".cargo")
    candidates += [cargo / "credentials", cargo / "credentials.toml"]
    # W33 where the operator moved it: skep reads the Codex login from
    # $CODEX_HOME when set (codex_login.auth_file), so the mask follows it.
    from .codex_login import auth_file

    candidates.append(auth_file())
    candidates += [skep_home / SERVE_ENV, supervisor / "audit", supervisor / "chat-attachments"]
    readable = {"llm-secret", *_active_llm_key_names(supervisor)} if worker_llm_key else set()
    if supervisor.is_dir():
        candidates += sorted(
            p
            for p in supervisor.iterdir()
            if p.name not in readable and any(fnmatch(p.name, pat) for pat in SKEP_SECRET_NAMES)
        )
    # v127-F6: the store, its backups and the retired layouts' stores
    live_store = set(_LIVE_STORE_FILES) if worker_llm_key else set()
    candidates += [skep_home / "backups", skep_home / "archive"]
    candidates += [skep_home / rel for rel in RETIRED_STORE_FILES]
    for root in (skep_home, supervisor):
        if root.is_dir():
            candidates += sorted(
                p
                for p in root.iterdir()
                if fnmatch(p.name, "supervisor*.sqlite3*")
                and not (root == supervisor and p.name in live_store)
            )
    # dict: the default ~/.codex/auth.json is already in HOME_SECRETS
    return list(dict.fromkeys(path.resolve() for path in candidates if _exists(path)))


def _exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:  # another user's home we may not look into: nothing to mask
        return False


def _active_llm_key_names(supervisor: Path) -> set[str]:
    """The active provider profile's key file name (v108-F4), if any."""
    db_path = supervisor / "supervisor.sqlite3"
    if not db_path.is_file():
        return set()
    from ..profile import load_profile, profile_path
    from .codex_login import uses_codex_login
    from .serve.llm import provider_secret_path, worker_key_profile
    from .store import RunStore

    store = RunStore(db_path)
    try:
        active = store.active_provider_profile()
        if active is not None and uses_codex_login(active.base_url):
            # v118-F4/R1: never the login — the key of the provider the
            # workers kept (profile.json), or none.
            endpoint = ""
            if profile_path(supervisor.parent).is_file():
                with contextlib.suppress(OSError, ValueError):
                    endpoint = load_profile(supervisor.parent).provider.endpoint or ""
            active = worker_key_profile(store, endpoint)
    finally:
        store.close()
    if active is None:
        return set()
    return {provider_secret_path(supervisor, active.provider_id).name}


# ``skep serve`` records its port here (under ``<home>/supervisor``) so every
# profile written for that home denies it — a CLI ``skep run`` included, which
# never sees serve's environment (Mac finding F). A stale file only denies a
# port nothing listens on.
SERVE_PORT_FILE = "serve-port"


def _serve_ports(skep_home: Path | None) -> set[int]:
    """Where serve may listen: the default, the CLI's $SKEP_SERVE_URL, and the
    port ``skep serve`` recorded under this home."""
    ports = {8765}
    url_port = urlparse(os.environ.get("SKEP_SERVE_URL", "")).port
    if url_port:
        ports.add(url_port)
    port_file = skep_home / "supervisor" / SERVE_PORT_FILE if skep_home else None
    raw = port_file.read_text(encoding="utf-8").strip() if port_file and port_file.is_file() else ""
    if raw.isdigit():
        ports.add(int(raw))
    return ports


# D6 (operator decision 2026-11-13, supersedes v115-F9's open loopback):
# loopback is closed to a sandboxed worker in every network mode except the
# ports the run was given — the project's ``loopback_ports`` and a local
# model provider's own port (``netproxy.run_loopback_ports``); serve's port is
# denied last, so no list reopens it. Seatbelt accepts only "*" or
# "localhost" in a network address filter (an IP literal is a profile syntax
# error), and "localhost" matches every address this host owns, so a closed
# port is closed on every address of this host. What closes is the worker
# REACHING a port (connect); binding and accepting stay open as before —
# per-port bind/inbound filters refused listen() on allowed ports under
# allow-default on this Seatbelt (measured 2026-11-13), and binding reaches
# nothing.
def _loopback_rules(ports: Iterable[int], *, open_bind: bool) -> list[str]:
    rules = (
        [
            '(allow network-bind (local ip "localhost:*"))',
            '(allow network-inbound (local ip "localhost:*"))',
        ]
        if open_bind  # under (deny network*); allow-default already allows both
        else []
    )
    rules.append('(deny network-outbound (remote ip "localhost:*"))')
    rules += [
        f'(allow network-outbound (remote ip "localhost:{port}"))' for port in sorted(set(ports))
    ]
    return rules


# v115-F14: Unix-domain sockets are files, and "(deny network*)" covers them
# too — skep's own netshim/netproxy tests bind one and died with EPERM inside
# the re-verify wall. Creating one still needs a writable root (file-write
# says where); which ones it may CONNECT to is _unix_socket_rules (W34).
_UNIX_SOCKET_BIND_RULES = (
    "(allow network-bind (local unix-socket))",
    "(allow network-inbound (local unix-socket))",
)

# W34: a connect to any unix socket on the host reached the operator's
# ssh-agent (it listed the keys), Docker, gpg-agent. Every network mode now
# denies it and allows only the run's own roots, plus mDNSResponder where the
# run may reach the network. Seatbelt matches the resolved path.
# getaddrinfo asks mDNSResponder for every name but /etc/hosts ones, and it
# sends the query on: a deny-all or proxied worker (the proxy resolves for
# it) must not, or a name becomes a DNS channel out.
_DNS_SOCKET = "/private/var/run/mDNSResponder"


def _unix_socket_rules(roots: Iterable[Path], network: NetworkPolicy) -> list[str]:
    allowed = [f'(literal "{_DNS_SOCKET}")'] if network.is_allow_all else []
    allowed += [f'(subpath "{_sbpl_quote(Path(root).resolve())}")' for root in roots]
    rules = ["(deny network-outbound (remote unix-socket))"]
    if allowed:  # an unfiltered "(allow network-outbound)" would open all egress
        rules.append(f"(allow network-outbound {' '.join(allowed)})")
    return rules


def _network_rules(
    network: NetworkPolicy,
    proxy_port: int | None,
    loopback_ports: Iterable[int] = (),
    unix_rules: Iterable[str] = (),
) -> list[str]:
    """The per-port allows come LAST: on this Seatbelt any network-outbound
    deny after a one-port allow (the unix-socket deny, a serve-port deny)
    also refused some of those ports — 20000 and 50000 did, 5432 and 40000
    did not (measured 2026-11-13)."""
    if network.is_deny_all:
        return [
            "(deny network*)",
            *_UNIX_SOCKET_BIND_RULES,
            *unix_rules,
            *_loopback_rules(loopback_ports, open_bind=True),
        ]
    if network.is_allow_all:
        # allow-default already permits network; loopback is still closed (D6).
        return [
            "; network: allow-all (manual real-provider path; allow-default)",
            *unix_rules,
            *_loopback_rules(loopback_ports, open_bind=False),
        ]
    # Concrete domain allowlist (D1, v3): deny all egress except the loopback port
    # of the filtering proxy that enforces the domains. Seatbelt gates by IP/port,
    # so pinning egress to the proxy is the half it *can* enforce; netproxy.py
    # enforces the domains themselves. A domain list with no proxy port is a
    # misconfiguration, not a silent un-enforcement.
    if proxy_port is None:
        raise SandboxAllowlistUnsupported(
            f"network allowlist {network.allow!r} needs a filtering proxy to enforce per-domain "
            "rules (Seatbelt filters by IP/port, not DNS). Start a FilteringProxy and pass its "
            "port, or use deny-all / allow-all."
        )
    return [
        "(deny network*)",
        *_UNIX_SOCKET_BIND_RULES,
        *unix_rules,
        *_loopback_rules(loopback_ports, open_bind=True),
        f'(allow network-outbound (remote ip "localhost:{proxy_port}"))',
    ]


# v28-F3: the bind-mount target inside the sandbox for the host proxy socket.
# Short (AF_UNIX 108-char limit) and under the tmpfs bwrap already mounts.
# ADR 0060: the socket's private DIRECTORY is bound (read-only; connect()
# still works), not the socket file: a file bind pins the inode of a listener
# that dies with serve, while a re-adopting serve listens at the same path.
SANDBOX_PROXY_DIR = "/tmp/.skep-proxy"


def _bubblewrap_args(
    *,
    writable_roots: Iterable[Path],
    network: NetworkPolicy,
    proxy_port: int | None,
    unix_socket_path: str | None = None,
    read_denied: Iterable[Path] = (),
    readable_roots: Iterable[Path] = (),
    write_denied: Iterable[Path] = (),
    loopback_ports: Iterable[int] = (),
    unmasked: Iterable[Path] = (),
    strict: bool = False,
    aliases: Iterable[Path] = (),
) -> list[str]:
    # ADR 0068: ``strict`` starts from bwrap's empty root instead of the
    # host's: the system dirs, then ``readable_roots`` (which the caller has
    # widened with strict_read_roots), are all a run sees outside its own;
    # ``aliases`` are their written names (``_alias_links``).
    # D6: the netns has its own loopback, so the host's is reachable only
    # through the proxy bridge, which admits the run's loopback ports alone;
    # netshim presents each of them on the netns loopback over that bridge.
    # v28-F3: a domain list is now enforceable on Linux. Keep --unshare-net
    # (the deny-all pin Linux already trusts — the netns has NO route out),
    # bind-mount the host FilteringProxy's unix socket in, and prefix the
    # worker with skep.netshim, which presents the proxy port on the netns
    # loopback and bridges it to that socket. Egress is exactly as tight as
    # deny-all, plus one filtered pipe.
    if network.is_domain_list and (proxy_port is None or unix_socket_path is None):
        raise SandboxAllowlistUnsupported(
            f"network allowlist {network.allow!r} needs a filtering proxy and its bridge "
            "socket to enforce per-domain rules on bubblewrap. Start a FilteringProxy with "
            "a unix_socket_path and pass both, or use deny-all / allow-all."
        )
    base = _strict_root_args() if strict else list(_HOST_ROOT)
    args = [*base, *_BUBBLEWRAP_WALLS, *_run_rebinds()]
    if network.is_deny_all or network.is_domain_list:
        args.append("--unshare-net")
    writable_roots = list(writable_roots)
    readable_roots = list(readable_roots)
    tmp_mountpoints: dict[str, None] = {}
    for root in [*readable_roots, *writable_roots]:
        # an existing non-dir root (a git common dir entry: a file, an
        # fsmonitor socket) needs only its parents — a --dir at a socket's
        # path broke the bind (W12); a root not made yet is a dir to be
        node = Path(root)
        dir_root = node.parent if node.exists() and not node.is_dir() else node
        for mountpoint in _tmp_mountpoints_for_bind(dir_root):
            tmp_mountpoints.setdefault(mountpoint, None)
    for mountpoint in tmp_mountpoints:
        args.extend(["--dir", mountpoint])
    for root in readable_roots:
        # Before the writable binds, so those land on top (git's objects/
        # inside a read-only common dir).
        args.extend(["--ro-bind", str(root), str(root)])
    for root in writable_roots:
        path = str(Path(root))
        args.extend(["--bind", path, path])
    for pinned in write_denied:
        # W31: read-only over its writable root (the run's git pin).
        args.extend(["--ro-bind", str(pinned), str(pinned)])
    args.extend(_alias_links(aliases, [*readable_roots, *writable_roots]))
    for denied in read_denied:
        # ES-F4: after the writable binds, so a mask wins inside one too.
        # /tmp and /run are fresh tmpfs here: their host paths do not exist,
        # except inside a root bound back in (R5: a repo's git dir under /tmp).
        if denied.parts[1:2] in (("tmp",), ("run",)) and not any(
            denied.is_relative_to(root) for root in [*readable_roots, *writable_roots]
        ):
            continue
        args.extend(
            _mask_args(
                denied,
                dev_null=["--ro-bind", "/dev/null", str(denied)],
                tmpfs=["--tmpfs", str(denied)],
            )
        )
    for own in unmasked:  # after the masks: this run's own file in a masked dir
        args.extend(["--ro-bind", str(own), str(own)])
    if network.is_domain_list and unix_socket_path is not None:
        args.extend(["--ro-bind", str(Path(unix_socket_path).parent), SANDBOX_PROXY_DIR])
    if strict:  # the empty root's own dirs, made read-only once every bind is in
        args.extend(["--remount-ro", "/"])
    if network.is_domain_list:
        assert unix_socket_path is not None and proxy_port is not None
        # ES-F251: netshim binds each port inside the worker's own netns,
        # whose unprivileged-port floor is 1024 and which the worker holds no
        # capability to lower: a port below it killed the worker (netshim
        # exit 3). Refuse before anything starts, and say what to change.
        if privileged := sorted({port for port in loopback_ports if port < 1024}):
            shown = ", ".join(str(port) for port in privileged)
            raise SandboxUnavailableError(
                f"loopback port {shown} is below 1024: on Linux the sandbox presents each "
                "loopback port inside the worker's own network namespace, where an "
                "unprivileged process cannot bind a port below 1024, so the worker "
                "could not start there. Serve the local model provider or service on "
                "port 1024 or above (ollama's default is 11434) and update the "
                "provider URL or the project's loopback_ports to match; skep never "
                "runs a sandbox run unconfined."
            )
        socket_path = Path(unix_socket_path)
        # The shim prefix runs INSIDE the sandbox before the worker; wrap_command
        # appends the trailing "--" that separates it from the worker argv.
        args.extend(
            [
                "--",
                sys.executable,
                "-m",
                "skep.netshim",
                "--unix",
                f"{SANDBOX_PROXY_DIR}/{socket_path.name}",
                "--port",
                str(proxy_port),
                *(
                    arg
                    for port in sorted(set(loopback_ports))
                    for arg in ("--loopback-port", str(port))
                ),
            ]
        )
    return args


def _landlock_step(writable_roots: Iterable[Path]) -> list[str]:
    """R5b: the in-sandbox step that scopes the worker's unix sockets with
    what the running kernel's Landlock ABI offers; none below ABI 6 (the
    run carries on with the masks alone, and doctor says so). ``-E -P``:
    neither the worker's cwd nor a worktree entry on PYTHONPATH may shadow
    skep's own module (the env still reaches the worker through exec)."""
    abi = landlock.abi()
    flags = ["--scope-abstract"] if abi >= landlock.ABI_SCOPE_ABSTRACT else []
    if abi >= landlock.ABI_RESOLVE_UNIX:
        for root in writable_roots:
            flags += ["--unix-root", str(root)]
    return [sys.executable, "-E", "-P", "-m", "skep.landlock", *flags] if flags else []


def _mask_args(path: Path, *, dev_null: list[str], tmpfs: list[str]) -> list[str]:
    """An empty tmpfs over a secret directory, /dev/null over a secret file."""
    return tmpfs if path.is_dir() else dev_null


def _podman_args(
    *,
    writable_roots: Iterable[Path],
    network: NetworkPolicy,
    rootfs: Path,
    read_denied: Iterable[Path] = (),
    readable_roots: Iterable[Path] = (),
    write_denied: Iterable[Path] = (),
    unmasked: Iterable[Path] = (),
) -> list[str]:
    """v44-F7: the container equivalent of the bwrap invocation.

    Skeleton rootfs as a throwaway overlay + host toolchain RO binds (tools
    and venv visible, everything else read-only), writable roots as real bind
    mounts, ``--network=none`` as the deny-all pin. A concrete domain list
    fails closed: the netshim/unix-socket bridge is bwrap-specific, and a
    half-enforced allowlist would be a lie. All options precede the positional
    ``rootfs:O``; the worker argv appends after it (podman's command slot).
    """
    if network.is_domain_list:
        raise SandboxAllowlistUnsupported(
            f"network allowlist {network.allow!r} cannot be enforced by the podman "
            "backend (the filtering-proxy bridge is bubblewrap-specific). Use "
            "deny-all / allow-all, or the native backend."
        )
    roots = [Path(root) for root in writable_roots]
    # ES-F7 (B53): on SELinux-enforcing hosts container_t cannot read
    # user_home_t, so the worker could not read its worktree, task file or
    # skep's venv. label=disable is the narrowest setting that works: ``:z``
    # relabels host files permanently (the repo's objects/, the results dir)
    # and still leaves /home unreadable. The wall is then the mount layout,
    # namespaces and the secret masks — the same wall as bubblewrap.
    args = ["run", "--rm", "--rootfs", "--env-host", "--security-opt", "label=disable"]
    if network.is_deny_all:
        args.append("--network=none")
    args.extend(_podman_ro_bind_args())
    args.extend(["--tmpfs", "/tmp"])
    for root in readable_roots:
        args.extend(["-v", f"{root}:{root}:ro"])
    for root in roots:
        args.extend(["-v", f"{root}:{root}"])
    for pinned in write_denied:  # W31: read-only over its writable root
        args.extend(["-v", f"{pinned}:{pinned}:ro"])
    for denied in read_denied:  # ES-F4: /home rides in read-only; hide these
        # R5: podman's --tmpfs copies the directory it covers into the tmpfs
        # by default (tmpcopyup), so the mask showed every secret under it.
        args.extend(
            _mask_args(
                denied,
                dev_null=["-v", f"/dev/null:{denied}:ro"],
                tmpfs=["--tmpfs", f"{denied}:notmpcopyup"],
            )
        )
    for own in unmasked:  # podman mounts a child over its parent's tmpfs
        args.extend(["-v", f"{own}:{own}:ro"])
    if roots:
        # Popen(cwd=workspace) only moves the podman CLIENT; the container's
        # workdir needs pinning explicitly (workspace is always the first root).
        args.extend(["-w", str(roots[0])])
    args.append(f"{rootfs}:O")
    return args


def _tmp_mountpoints_for_bind(path: Path) -> list[str]:
    parts = path.parts
    if len(parts) < 3 or parts[0] != "/" or parts[1] != "tmp":
        return []
    current = Path("/tmp")
    mountpoints: list[str] = []
    for part in parts[2:]:
        current /= part
        mountpoints.append(str(current))
    return mountpoints


def _write_argv_profile(profile_path: Path, args: list[str], *, header: str) -> Path:
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(f"{header}\n{json.dumps(args, indent=2)}\n")
    return profile_path


def _read_argv_profile(profile_path: Path, *, header: str) -> list[str]:
    text = profile_path.read_text(encoding="utf-8")
    first, _, body = text.partition("\n")
    if first != header:
        raise SandboxUnavailableError(
            f"{profile_path} is not a {header.removeprefix('# ')} profile evidence file"
        )
    parsed = json.loads(body)
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise SandboxUnavailableError(f"{profile_path} contains invalid sandbox argv evidence")
    return parsed


def _write_bubblewrap_profile(profile_path: Path, args: list[str]) -> Path:
    return _write_argv_profile(profile_path, args, header=_BUBBLEWRAP_HEADER)


def _read_bubblewrap_profile(profile_path: Path) -> list[str]:
    return _read_argv_profile(profile_path, header=_BUBBLEWRAP_HEADER)


def _sbpl_quote(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def build_profile(
    *,
    writable_roots: Iterable[Path],
    network: NetworkPolicy = DENY_ALL_NETWORK,
    proxy_port: int | None = None,
    unix_socket_path: str | None = None,
    read_denied: Iterable[Path] = (),
    skep_home: Path | None = None,
    write_denied: Iterable[Path] = (),
    loopback_ports: Iterable[int] = (),
    unmasked: Iterable[Path] = (),
    read_allowlist: Iterable[Path] | None = None,
) -> str:
    """Return the SBPL text confining writes to ``writable_roots`` and gating network.

    ``read_allowlist`` (ADR 0068): None keeps reads open but ``read_denied``;
    otherwise reads are confined to macOS's system roots, the writable roots
    and these (``_seatbelt_read_rules``), with ``read_denied`` still last.

    Last-matching-rule-wins SBPL: ``allow default`` keeps reads/exec/IPC working,
    then network is denied (deny-all) and writes are denied everywhere and
    re-allowed only under the given roots. When ``network`` names concrete domains
    (D1), ``proxy_port`` pins egress to the loopback filtering proxy enforcing them.
    """
    # macOS pins egress to the proxy TCP port directly; the unix bridge is a
    # Linux-only concern, so Seatbelt ignores it.
    _ = unix_socket_path
    roots = [Path(root) for root in writable_roots]
    lines = [
        "(version 1)",
        "(allow default)",
        # M9: no process outside the wall — LaunchServices (`open`, an app or
        # a .command), launchd jobs, Apple events (`tell application
        # "Terminal" to do script`) — and no Spotlight name/content queries.
        "(deny lsopen)",
        "(deny job-creation)",
        "(deny appleevent-send)",
        '(deny mach-lookup (global-name "com.apple.metadata.mds"))',
        # ES-F4 (B05) + D6: serve's port never opens, even when a project lists
        # it — dropped from the allows (the loopback deny covers it), never a
        # trailing per-port deny (see _network_rules).
        *_network_rules(
            network,
            proxy_port,
            set(loopback_ports) - _serve_ports(skep_home),
            _unix_socket_rules(roots, network),
        ),
        "(deny file-write*)",
    ]
    if roots:
        subpaths = " ".join(f'(subpath "{_sbpl_quote(root)}")' for root in roots)
        lines.append(f"(allow file-write* {subpaths})")
    dev_nodes = " ".join(f'(literal "{node}")' for node in _DEV_WRITE_NODES)
    lines.append(f"(allow file-write* {dev_nodes})")
    for pinned in write_denied:  # W31: last match wins over its writable root
        lines.append(f'(deny file-write* (literal "{_sbpl_quote(pinned)}"))')
    if read_allowlist is not None:
        lines += _seatbelt_read_rules([*roots, *read_allowlist])
    for denied in read_denied:
        kind = "subpath" if denied.is_dir() else "literal"
        lines.append(f'(deny file-read* ({kind} "{_sbpl_quote(denied)}"))')
    for own in unmasked:  # last match wins: this run's own file in a masked dir
        lines.append(f'(allow file-read* (literal "{_sbpl_quote(own)}"))')
    return "\n".join(lines) + "\n"


def in_wall_tmp_env(tmp_dir: Path) -> dict[str, str]:
    """The temp-file env for a sandboxed process whose scratch is ``tmp_dir``.

    v120-F1: TMPDIR alone is not enough — zsh writes here-document temp files
    under ``$TMPPREFIX`` (default ``/tmp/zsh``), and /tmp is outside the wall
    (ES-F5), so every ``zsh -c "... <<'PY'"`` died with "can't create temp
    file for here document". TMPPREFIX is a filename prefix, not a directory.

    v127-A0 trial fix (V01): on macOS, XDG_RUNTIME_DIR too — tools that make
    temp dirs under it, else /tmp (skep's own suite), hit /tmp outside the
    Seatbelt wall, and re-verify failed a correct patch. Not on Linux: bwrap
    gives every run a private writable /tmp, and a runtime dir this deep
    would push AF_UNIX socket paths past their 108-char cap. Every POSIX
    temp lookup (Python, Node, Go) reads TMPDIR before TEMP/TMP.
    """
    env = {"TMPDIR": str(tmp_dir), "TMPPREFIX": str(tmp_dir / "zsh")}
    if sys.platform == "darwin":
        env["XDG_RUNTIME_DIR"] = str(tmp_dir)
    return env


def write_profile(
    profile_path: Path,
    *,
    workspace: Path,
    extra_writable: Iterable[Path] = (),
    extra_readable: Iterable[Path] = (),
    network: NetworkPolicy = DENY_ALL_NETWORK,
    proxy_port: int | None = None,
    unix_socket_path: str | None = None,
    backend: str | None = None,
    skep_home: Path,
    worker_llm_key: bool = False,
    write_denied: Iterable[Path] = (),
    loopback_ports: Iterable[int] = (),
    unmasked: Iterable[Path] = (),
    read_allowlist: Iterable[Path] | None = None,
) -> Path:
    """Materialize the profile for one task and return its path (kept as evidence).

    ES-F1 (B47, I12): raises :class:`SandboxUnavailableError` when the backend
    cannot be applied here. Every sandboxed launch writes its profile first, so
    this is the chokepoint: no wall, no profile, no run — never a quiet
    unconfined fallback recorded as ``sandbox``.

    ``skep_home`` is required (Mac finding A): ``skep serve --home X`` does
    not export SKEP_HOME, so an environment fallback hid the wrong home's
    secrets. Every caller passes the home its own config names.

    ``extra_readable`` is bound read-only where a backend hides the host
    (bwrap/podman's tmpfs /tmp); Seatbelt reads it on a strict run alone.

    ``worker_llm_key`` leaves the worker's own LLM key readable (B125): only
    a worker whose caste calls the LLM passes it.

    ``write_denied`` (W31): files inside a writable root the worker may read
    but never write, rename or delete — the run's git protocol pin.

    ``loopback_ports`` (D6): the only loopback ports the run reaches
    (``netproxy.run_loopback_ports``); the default closes loopback.

    ``unmasked`` (ES-F122 follow-up): files inside a masked directory that
    THIS run alone may read, bound back read-only after the masks — a codex
    run's own login copy under the audit dir every other run cannot see.

    ``read_allowlist`` (ADR 0068, ``read_allowlist_for``): None keeps reads
    open but the secret floor (denylist-only); a tuple — the project's
    ``extra_read_paths`` — confines reads to an allowlist: the system dirs,
    ``strict_read_roots`` and the run's own roots. A backend outside
    ``STRICT_READ_BACKENDS`` refuses it rather than run with open reads.
    """
    write_denied = list(write_denied)
    unmasked = [Path(own).resolve() for own in unmasked]
    read_allowlist = None if read_allowlist is None else tuple(read_allowlist)
    probe = require_usable(backend)
    if read_allowlist is not None and probe.backend not in STRICT_READ_BACKENDS:
        raise SandboxUnavailableError(
            f"strict read confinement (ADR 0068) is enforced on bubblewrap and Seatbelt "
            f"only; the {probe.backend} backend cannot confine reads to an allowlist yet, "
            "so this run is refused rather than run with open reads. Use the native "
            "bubblewrap backend, or turn it off for this project: `skep project setup "
            "<repo> --no-strict-reads`."
        )
    from .worktree import git_metadata_masked, git_walk_links

    if read_allowlist is not None and (links := git_walk_links(workspace)):
        shown = ", ".join(f"{link} (a link to {os.path.realpath(link)})" for link in links)
        raise SandboxUnavailableError(
            f"strict reads (ADR 0068): {shown} in the repo's shared git dir. git's "
            "reachability walk reads it, and inside the wall the link leads nowhere, so a "
            "`git prune` there would delete objects only it points to. The run is refused "
            f"rather than run with hidden pointers. Move the target back into {links[0].parent} "
            "in place of the link, or turn strict reads off for this project: `skep project "
            "setup <repo> --no-strict-reads`."
        )
    _refuse_home_inside(skep_home, [workspace, *extra_writable])
    # ES-F264: the mask list holds only files that exist now. relay-secret
    # used to appear on the first relayed dispatch, readable by every run
    # already going; made here first, it is on every run's list.
    # A home this process cannot write is one it cannot make the secret in
    # mid-run either: nothing to mask.
    from .provider_relay import relay_secret

    with contextlib.suppress(OSError):
        relay_secret(skep_home / "supervisor")
    read_denied = secret_paths(skep_home, worker_llm_key=worker_llm_key)
    git_masked = git_metadata_masked(workspace)  # inside the common dir bound whole (3I)
    if probe.backend in (PODMAN, "bubblewrap"):  # Seatbelt binds no readable root
        # a mount needs its file; a link can't be mounted over (R4, Neon) — so a
        # linked message file refuses the wall rather than going unmasked (R5, Sol)
        if linked := [str(p) for p in git_masked if p.is_symlink()]:
            raise SandboxUnavailableError(
                f"{', '.join(linked)} is a symlink, so the sandbox cannot mask it and the "
                "in-progress git message would be readable; replace it with a regular file"
            )
        read_denied += [p for p in git_masked if _exists(p)]
        extra_readable = list(extra_readable)
        # ponytail: the detected toolchains are not walked for sockets (~/.rustup
        # alone is tens of thousands of files); the operator's paths are.
        extra_roots = [path.resolve() for path in _validated_extra(read_allowlist or (), skep_home)]
        read_denied += _sockets_under([*extra_readable, *extra_roots])
        aliases = (
            _strict_candidates(skep_home, worker_llm_key=worker_llm_key, extra=read_allowlist)
            if read_allowlist is not None
            else []
        )
        extra_readable += dict.fromkeys(path.resolve() for path in aliases)
    if probe.backend == PODMAN:
        extra_writable = list(extra_writable)
        args = _podman_args(
            writable_roots=[workspace, *extra_writable],
            network=network,
            rootfs=_podman_rootfs_skeleton(profile_path.parent),
            read_denied=read_denied,
            readable_roots=extra_readable,
            write_denied=write_denied,
            unmasked=unmasked,
        )
        # podman's command slot follows rootfs:O; the worker argv lands after "--"
        if step := _landlock_step([workspace, *extra_writable]):
            args += [*step, "--"]
        return _write_argv_profile(profile_path, args, header=_PODMAN_HEADER)
    if probe.backend == "bubblewrap":
        writable_roots = [workspace, *extra_writable]
        args = _bubblewrap_args(
            writable_roots=writable_roots,
            network=network,
            proxy_port=proxy_port,
            unix_socket_path=unix_socket_path,
            read_denied=read_denied,
            readable_roots=extra_readable,
            write_denied=write_denied,
            loopback_ports=loopback_ports,
            unmasked=unmasked,
            strict=read_allowlist is not None,
            aliases=aliases,
        )
        # After the netshim when there is one (it alone needs the proxy
        # bridge); wrap_command's "--" then ends the step's options.
        if step := _landlock_step(writable_roots):
            args += step if network.is_domain_list else ["--", *step]
        return _write_bubblewrap_profile(profile_path, args)
    # ES-F5 (B11): no blanket /tmp, /private/tmp, /var/folders roots — they
    # were shared by every run (run B read run A's files) and held any
    # SKEP_HOME placed under $TMPDIR. Each run's TMPDIR is already private,
    # inside its workspace (dispatch/reverify/diagnose/fast lane set it).
    writable_roots = [workspace, *extra_writable]
    if not worker_llm_key:  # v127-F6: a literal deny holds before the file exists
        live = [(skep_home / "supervisor" / name).resolve() for name in _LIVE_STORE_FILES]
        read_denied = list(dict.fromkeys([*read_denied, *live]))
    read_denied = [*read_denied, *git_masked]  # a literal deny holds for a file made later
    strict_roots = None
    if read_allowlist is not None:  # ADR 0068: what it reads beyond macOS and its roots
        strict_roots = [
            *strict_read_roots(skep_home, worker_llm_key=worker_llm_key, extra=read_allowlist),
            *extra_readable,
            *unmasked,  # still masked but for the literal allow after the masks
        ]
        read_denied = [
            *read_denied,
            *(Path("/private/etc", n) for n in _SEATBELT_ETC_HIDDEN),
            *map(Path, _HOMEBREW_VAR),
        ]
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(
        build_profile(
            writable_roots=writable_roots,
            network=network,
            proxy_port=proxy_port,
            unix_socket_path=unix_socket_path,
            read_denied=read_denied,
            skep_home=skep_home,
            write_denied=write_denied,
            loopback_ports=loopback_ports,
            unmasked=unmasked,
            read_allowlist=strict_roots,
        )
    )
    return profile_path


def wrap_command(
    argv: Iterable[str], profile_path: Path, *, backend: str | None = None
) -> list[str]:
    """Prefix a worker argv with the active sandbox backend."""
    probe = require_usable(backend)
    if probe.backend == PODMAN:
        binary = _podman_binary()
        if binary is None:
            raise SandboxUnavailableError(
                "podman was available during probe but is no longer on PATH"
            )
        return [binary, *_read_argv_profile(profile_path, header=_PODMAN_HEADER), *argv]
    if probe.backend == "bubblewrap":
        binary = _bubblewrap_binary()
        if binary is None:
            raise SandboxUnavailableError(
                "Bubblewrap was available during probe but is no longer on PATH"
            )
        return [binary, *_read_bubblewrap_profile(profile_path), "--", *argv]
    return [SANDBOX_EXEC, "-f", str(profile_path), *argv]
