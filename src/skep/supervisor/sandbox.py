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
env allowlist.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import fnmatch
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from .. import landlock
from .secret_floor import HOME_SECRETS, SERVE_ENV, SKEP_SECRET_NAMES, other_homes

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
_BUBBLEWRAP_BASE_ARGS = (
    "--ro-bind",
    "/",
    "/",
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
# profile's ``llm-secret-<id>`` (llm_plan.py) until V6 moves the credential
# into the proxy. The store (supervisor.sqlite3) stays readable: that same
# worker reads its provider settings from it. Checked when the profile is
# built, so a secret file created mid-run is not covered.
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
    found = []
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
    candidates += [skep_home / SERVE_ENV, supervisor / "audit", supervisor / "chat-attachments"]
    readable = {"llm-secret", *_active_llm_key_names(supervisor)} if worker_llm_key else set()
    if supervisor.is_dir():
        candidates += sorted(
            p
            for p in supervisor.iterdir()
            if p.name not in readable and any(fnmatch(p.name, pat) for pat in SKEP_SECRET_NAMES)
        )
    return [path.resolve() for path in candidates if _exists(path)]


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
    from .serve.llm import provider_secret_path
    from .store import RunStore

    store = RunStore(db_path)
    try:
        active = store.active_provider_profile()
    finally:
        store.close()
    return {provider_secret_path(supervisor, active.provider_id).name} if active else set()


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


# v115-F9: loopback is not egress. The project's own tests start servers on
# 127.0.0.1 and talk to them; with loopback denied, skep's own suite lost 210
# tests inside the wall on its first self-maintenance run. The wall still
# governs what leaves the machine — and it is STATED (I12): a sandboxed worker
# can reach services listening on the host's loopback; those must carry their
# own auth (the serve API does, G2).
# Seatbelt accepts only "*" or "localhost" in a network address filter (an IP
# literal is a profile syntax error), and "localhost" matches every address
# this host owns. So the wall, stated exactly: a sandboxed worker can reach
# listeners on THIS host; it cannot reach any other machine.
_LOOPBACK_RULES = (
    '(allow network-bind (local ip "localhost:*"))',
    '(allow network-inbound (local ip "localhost:*"))',
    '(allow network-outbound (remote ip "localhost:*"))',
    # v115-F14: Unix-domain sockets are files, and "(deny network*)" covers
    # them too — skep's own netshim/netproxy tests bind one and died with
    # EPERM inside the re-verify wall. Creating one still needs a writable
    # root (file-write says where); which ones it may CONNECT to is
    # _unix_socket_rules (W34).
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


def _network_rules(network: NetworkPolicy, proxy_port: int | None) -> list[str]:
    if network.is_deny_all:
        return ["(deny network*)", *_LOOPBACK_RULES]
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
        *_LOOPBACK_RULES,
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
    read_denied: Iterable[Path] = (),
    readable_roots: Iterable[Path] = (),
    write_denied: Iterable[Path] = (),
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
    args = [*_BUBBLEWRAP_BASE_ARGS, *_run_rebinds()]
    if network.is_deny_all or network.is_domain_list:
        args.append("--unshare-net")
    writable_roots = list(writable_roots)
    readable_roots = list(readable_roots)
    tmp_mountpoints: dict[str, None] = {}
    for root in [*readable_roots, *writable_roots]:
        for mountpoint in _tmp_mountpoints_for_bind(Path(root)):
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


def _landlock_step(writable_roots: Iterable[Path]) -> list[str]:
    """R5b: the in-sandbox step that scopes the worker's unix sockets with
    what the running kernel's Landlock ABI offers; none below ABI 6 (the
    run carries on with the masks alone, and doctor says so). ``-P``: the
    worker's cwd must not shadow skep's own module."""
    abi = landlock.abi()
    flags = ["--scope-abstract"] if abi >= landlock.ABI_SCOPE_ABSTRACT else []
    if abi >= landlock.ABI_RESOLVE_UNIX:
        for root in writable_roots:
            flags += ["--unix-root", str(root)]
    return [sys.executable, "-P", "-m", "skep.landlock", *flags] if flags else []


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
        *_unix_socket_rules(roots, network),
        "(deny file-write*)",
    ]
    if roots:
        subpaths = " ".join(f'(subpath "{_sbpl_quote(root)}")' for root in roots)
        lines.append(f"(allow file-write* {subpaths})")
    dev_nodes = " ".join(f'(literal "{node}")' for node in _DEV_WRITE_NODES)
    lines.append(f"(allow file-write* {dev_nodes})")
    for pinned in write_denied:  # W31: last match wins over its writable root
        lines.append(f'(deny file-write* (literal "{_sbpl_quote(pinned)}"))')
    # ES-F4 (B05): last match wins, so these follow every allow above. The
    # loopback allow (v115-F9) keeps a worker's own test servers reachable;
    # serve's port is the one listener on this host it must never dial.
    for port in sorted(_serve_ports(skep_home)):
        lines.append(f'(deny network-outbound (remote ip "localhost:{port}"))')
    for denied in read_denied:
        kind = "subpath" if denied.is_dir() else "literal"
        lines.append(f'(deny file-read* ({kind} "{_sbpl_quote(denied)}"))')
    return "\n".join(lines) + "\n"


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
    (bwrap/podman's tmpfs /tmp); Seatbelt leaves reads open and ignores it.

    ``worker_llm_key`` leaves the worker's own LLM key readable (B125): only
    a worker whose caste calls the LLM passes it.

    ``write_denied`` (W31): files inside a writable root the worker may read
    but never write, rename or delete — the run's git protocol pin.
    """
    write_denied = list(write_denied)
    probe = require_usable(backend)
    _refuse_home_inside(skep_home, [workspace, *extra_writable])
    read_denied = secret_paths(skep_home, worker_llm_key=worker_llm_key)
    if probe.backend in (PODMAN, "bubblewrap"):  # Seatbelt binds no readable root
        extra_readable = list(extra_readable)
        read_denied += _sockets_under(extra_readable)
    if probe.backend == PODMAN:
        extra_writable = list(extra_writable)
        args = _podman_args(
            writable_roots=[workspace, *extra_writable],
            network=network,
            rootfs=_podman_rootfs_skeleton(profile_path.parent),
            read_denied=read_denied,
            readable_roots=extra_readable,
            write_denied=write_denied,
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
