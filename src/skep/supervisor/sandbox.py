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
workspace/results/temp boundary. Reads are left unrestricted so the
Python/git/test toolchain keeps working; read confinement is deliberately out of
scope (env-secret exposure is already closed by the G2 env allowlist).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

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
    try:
        proc = subprocess.run(
            [SANDBOX_EXEC, "-p", "(version 1)\n(allow default)\n", "/usr/bin/true"],
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


def _bubblewrap_probe_argv(binary: str) -> list[str]:
    return [
        binary,
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--unshare-net",
        "/usr/bin/true",
    ]


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
        rootfs = _podman_rootfs_skeleton(Path(tmp))
        argv = [
            binary,
            "run",
            "--rm",
            "--rootfs",
            "--env-host",
            "--network=none",
            *_podman_ro_bind_args(),
            "--tmpfs",
            "/tmp",
            f"{rootfs}:O",
            "/usr/bin/true",
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
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        return SandboxAvailability(False, "probe_rejected", detail, backend=PODMAN)
    return SandboxAvailability(True, backend=PODMAN)


def available() -> bool:
    """True when this process can enforce the native sandbox backend."""
    return availability().usable


def default_writable_roots() -> tuple[Path, ...]:
    """The macOS temp roots the worker toolchain legitimately writes to.

    Both the symlink form (``/tmp`` → ``/private/tmp``, ``/var/folders`` →
    ``/private/var/folders``) and the resolved form are included; Seatbelt
    matches the real (resolved) path, but listing both is harmless and explicit.
    """
    candidates = [
        Path("/tmp"),
        Path("/private/tmp"),
        Path("/var/folders"),
        Path("/private/var/folders"),
        Path(tempfile.gettempdir()),
        Path(tempfile.gettempdir()).resolve(),
    ]
    seen: dict[str, Path] = {}
    for path in candidates:
        seen.setdefault(str(path), path)
    return tuple(seen.values())


def _network_rules(network: NetworkPolicy, proxy_port: int | None) -> list[str]:
    if network.is_deny_all:
        return ["(deny network*)"]
    if network.is_allow_all:
        # allow-default already permits network; an explicit comment keeps the
        # generated profile self-documenting.
        return ["; network: allow-all (manual real-provider path; allow-default)"]
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
        f'(allow network-outbound (remote ip "localhost:{proxy_port}"))',
    ]


# v28-F3: the bind-mount target inside the sandbox for the host proxy socket.
# Short (AF_UNIX 108-char limit) and under the tmpfs bwrap already mounts.
SANDBOX_PROXY_SOCKET = "/tmp/.skep-proxy.sock"


def _bubblewrap_args(
    *,
    writable_roots: Iterable[Path],
    network: NetworkPolicy,
    proxy_port: int | None,
    unix_socket_path: str | None = None,
) -> list[str]:
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
    args = [
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
    ]
    if network.is_deny_all or network.is_domain_list:
        args.append("--unshare-net")
    tmp_mountpoints: dict[str, None] = {}
    for root in writable_roots:
        for mountpoint in _tmp_mountpoints_for_bind(Path(root)):
            tmp_mountpoints.setdefault(mountpoint, None)
    for mountpoint in tmp_mountpoints:
        args.extend(["--dir", mountpoint])
    for root in writable_roots:
        path = str(Path(root))
        args.extend(["--bind", path, path])
    if network.is_domain_list:
        assert unix_socket_path is not None and proxy_port is not None
        args.extend(["--bind", unix_socket_path, SANDBOX_PROXY_SOCKET])
        # The shim prefix runs INSIDE the sandbox before the worker; wrap_command
        # appends the trailing "--" that separates it from the worker argv.
        args.extend(
            [
                "--",
                sys.executable,
                "-m",
                "skep.netshim",
                "--unix",
                SANDBOX_PROXY_SOCKET,
                "--port",
                str(proxy_port),
            ]
        )
    return args


def _podman_args(
    *, writable_roots: Iterable[Path], network: NetworkPolicy, rootfs: Path
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
    args = ["run", "--rm", "--rootfs", "--env-host"]
    if network.is_deny_all:
        args.append("--network=none")
    args.extend(_podman_ro_bind_args())
    args.extend(["--tmpfs", "/tmp"])
    for root in roots:
        args.extend(["-v", f"{root}:{root}"])
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
) -> str:
    """Return the SBPL text confining writes to ``writable_roots`` and gating network.

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
        *_network_rules(network, proxy_port),
        "(deny file-write*)",
    ]
    if roots:
        subpaths = " ".join(f'(subpath "{_sbpl_quote(root)}")' for root in roots)
        lines.append(f"(allow file-write* {subpaths})")
    dev_nodes = " ".join(f'(literal "{node}")' for node in _DEV_WRITE_NODES)
    lines.append(f"(allow file-write* {dev_nodes})")
    return "\n".join(lines) + "\n"


def write_profile(
    profile_path: Path,
    *,
    workspace: Path,
    extra_writable: Iterable[Path] = (),
    network: NetworkPolicy = DENY_ALL_NETWORK,
    proxy_port: int | None = None,
    unix_socket_path: str | None = None,
    backend: str | None = None,
) -> Path:
    """Materialize the profile for one task and return its path (kept as evidence)."""
    probe = availability(backend)
    if probe.backend == PODMAN:
        return _write_argv_profile(
            profile_path,
            _podman_args(
                writable_roots=[workspace, *extra_writable],
                network=network,
                rootfs=_podman_rootfs_skeleton(profile_path.parent),
            ),
            header=_PODMAN_HEADER,
        )
    if probe.backend == "bubblewrap":
        writable_roots = [workspace, *extra_writable]
        return _write_bubblewrap_profile(
            profile_path,
            _bubblewrap_args(
                writable_roots=writable_roots,
                network=network,
                proxy_port=proxy_port,
                unix_socket_path=unix_socket_path,
            ),
        )
    writable_roots = [workspace, *extra_writable, *default_writable_roots()]
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(
        build_profile(
            writable_roots=writable_roots,
            network=network,
            proxy_port=proxy_port,
            unix_socket_path=unix_socket_path,
        )
    )
    return profile_path


def wrap_command(
    argv: Iterable[str], profile_path: Path, *, backend: str | None = None
) -> list[str]:
    """Prefix a worker argv with the active sandbox backend."""
    probe = availability(backend)
    if not probe.usable:
        detail = f": {probe.detail}" if probe.detail else ""
        raise SandboxUnavailableError(
            f"sandbox enforcement requested but no usable sandbox backend is available "
            f"(reason={probe.reason!r}, platform={sys.platform!r}){detail}. "
            "Disable the sandbox or run from a host process allowed to apply sandbox profiles."
        )
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
