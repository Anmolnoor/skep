"""Worker process spawning with a strict env allowlist (decision G2).

G2 is a v1 acceptance criterion: the worker child receives the task's
``env_allowlist`` picks plus a fixed non-secret infrastructure baseline
(PATH, HOME) — and **nothing else**. A worker that inherits the parent
environment can exfiltrate every secret in it; this module is the boundary.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

from skep.worker_contract import CodingWorkerTask
from skep.worker_contract.worker_state import (
    approval_grants_from_state,
    approved_network_hosts_from_verdict,
)

from . import sandbox
from .castes import CASTES
from .config import SupervisorConfig
from .netproxy import run_loopback_ports
from .worktree import git_metadata_readable_roots, git_metadata_writable_roots

logger = logging.getLogger("skep.spawner")


def resolve_sandbox_backend(config: SupervisorConfig, network: sandbox.NetworkPolicy) -> str | None:
    """v44-F7: which backend wraps this worker; None = the native host backend.

    An opted-in podman backend falls back to native — loudly, never silently —
    when podman is unusable here or the run needs a concrete domain allowlist
    (the filtering-proxy bridge is bubblewrap-specific). Fail-open toward the
    STRICTER native backend, never toward no sandbox.
    """
    if config.sandbox_backend in ("auto", "", "none"):
        return None
    backend = config.sandbox_backend
    if backend == sandbox.PODMAN and network.is_domain_list:
        logger.warning(
            "sandbox_backend=podman cannot enforce a domain allowlist; "
            "using the native backend for this run"
        )
        return None
    probe = sandbox.availability(backend)
    if not probe.usable:
        logger.warning(
            "sandbox_backend=%s is unusable here (%s: %s); using the native backend",
            backend,
            probe.reason,
            probe.detail,
        )
        return None
    return backend


def build_worker_env(
    allowlist: Iterable[str],
    *,
    baseline: Iterable[str] = ("PATH", "HOME"),
) -> dict[str, str]:
    """Return exactly the allowlisted + baseline variables present in the parent env.

    The supervisor's own venv is stripped from the worker PATH: workers must
    resolve ``python``/``pip``/toolchain from the system, never from skep's
    private interpreter. Leaving it in shadows the system python (the venv
    ships no pip, so toolchain bootstraps die on ``No module named pip``) and
    invites an approved install to mutate the supervisor's own environment —
    a side effect outside the worktree. Worker processes themselves are
    launched via absolute paths (``sys.executable``), so they don't need it.
    """
    names = [*baseline, *allowlist]
    env = {name: os.environ[name] for name in names if name in os.environ}
    path = env.get("PATH")
    if path and sys.prefix != sys.base_prefix:
        prefix = Path(sys.prefix)
        kept = [
            entry
            for entry in path.split(os.pathsep)
            if entry and not Path(entry).is_relative_to(prefix)
        ]
        env["PATH"] = os.pathsep.join(kept)
    return env


# B144: git inside a worker may use only its local ``file`` transport. The
# git floor reads command lines, so a script's own ``subprocess.run(['git',
# 'push'])`` slipped past it; this env reaches every git the worker tree
# starts (engines, scripts, grandchildren via the capability passthrough) and
# outranks ``-c protocol.*.allow``. https, ssh, git:// and ext:: all fail with
# "transport not allowed"; worktree, commit, diff and local clones still work.
# Supervisor-side pushes (open_pr, push) never run in this env.
# ponytail: env, so a script that rebuilds its env from scratch drops it; the
# sandbox's network policy is the wall, this closes the inherited-env path.
WORKER_GIT_ENV = {"GIT_ALLOW_PROTOCOL": "file"}


def _proxy_env(port: int) -> dict[str, str]:
    """Env that routes the worker's HTTP(S) tooling through the D1 filtering proxy.

    pip / git / curl / requests all honour these. Combined with the Seatbelt pin
    to ``localhost:port`` (the worker's only allowed egress), the proxy is the
    sole path out — so the domain allowlist is enforced, not merely suggested.

    D6 (supersedes v115-F9's ``NO_PROXY`` loopback exemption): loopback HTTP
    goes through the proxy too, on every backend, so a closed loopback port
    answers with a 403 that names the port and the setup command that opens
    it, where a direct connect would only say "Operation not permitted". (A
    dead open port now answers 502 rather than a connection error.)
    """
    url = f"http://127.0.0.1:{port}"
    env = {
        "HTTP_PROXY": url,
        "HTTPS_PROXY": url,
        "ALL_PROXY": url,
        "http_proxy": url,
        "https_proxy": url,
        "all_proxy": url,
    }
    return env


def effective_network_domains(task: CodingWorkerTask) -> tuple[str, ...]:
    """Return the worker's effective network grant for this run.

    Normal runs use the task's egress — its network allowlist plus its
    ``provider_hosts`` (ADR 0059 D3), so a provider-only run gets one filtered
    pipe to its provider instead of deny-all. A resumed
    approved ``network.fetch`` is the one case where the worker must regain a
    narrow host grant that was intentionally absent from ``permissions.network``
    on the suspended run: the approval decision names the allowed host.
    """
    domains = tuple(task.permissions.egress())
    if domains:
        # ADR 0059 D3 (V7): a deferred ask a human approved for THIS run's
        # resume chain joins its egress — only hosts the dispatch named as
        # asks, never another approved host (the proxy is the wall).
        approved = (
            approved_network_hosts_from_verdict(task.approval_verdict)
            | (approval_grants_from_state(task.worker_state)[3])
        )
        asked = tuple(host for host in task.permissions.network_ask if host in approved)
        return domains if domains == ("*",) else (*domains, *asked)
    verdict = task.approval_verdict
    if (
        verdict is not None
        and verdict.approved
        and verdict.action == "network.fetch"
        and verdict.decision is not None
        and isinstance(verdict.decision.detail, str)
        and verdict.decision.detail
    ):
        return (verdict.decision.detail,)
    return ()


def spawn_worker(
    config: SupervisorConfig,
    task: CodingWorkerTask,
    task_path: Path,
    out_path: Path,
    *,
    log_path: Path,
    network_proxy_port: int | None = None,
    network_proxy_unix_path: str | None = None,
    sandbox_enabled: bool | None = None,
    extra_env: dict[str, str] | None = None,
    worker_argv: tuple[str, ...] | None = None,
    extra_writable: Iterable[Path] = (),
    write_denied: Iterable[Path] = (),
) -> subprocess.Popen[bytes]:
    """Start the headless worker in its own session (so the whole tree is killable).

    When enabled (Q1), the worker argv is wrapped in the host sandbox backend:
    Seatbelt on macOS, Bubblewrap on Linux. Writes are physically confined to
    the workspace, the run's own result dir (W26), and temp. Network follows
    ``permissions.network`` (D1): an empty list denies all, ``["*"]`` allows all,
    and concrete domain lists use the loopback filtering proxy only on backends
    that can physically pin egress to it. The generated profile/evidence is kept
    beside the worker log as proof of exactly what was enforced.

    ``write_denied`` (W31): files inside a writable root the sandbox keeps
    read-only — the run's git protocol pin (``worktree.pin_git_protocols``).
    """
    argv = [
        # D2: route by caste. v90-F1: a coding run may override this with a
        # selected engine's argv (ADR 0047); castes keep their own workers.
        *(worker_argv if worker_argv is not None else config.command_for(task.worker_kind)),
        "--headless",
        "--task-file",
        str(task_path),
        "--out",
        str(out_path),
    ]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    domains = effective_network_domains(task)
    proxy_port = network_proxy_port if sandbox.NetworkPolicy(allow=domains).is_domain_list else None
    use_sandbox = config.sandbox if sandbox_enabled is None else sandbox_enabled
    if not domains:
        network = sandbox.DENY_ALL_NETWORK
    elif domains == ("*",):
        network = sandbox.ALLOW_ALL_NETWORK
    else:
        network = sandbox.NetworkPolicy(allow=domains)
    backend = resolve_sandbox_backend(config, network) if use_sandbox else None
    if use_sandbox:
        # ES-F1 (B47): no usable backend raises here — the worker never starts
        # unconfined under a "sandbox" record.
        sandbox.require_usable(backend)
        workspace = Path(task.workspace)
        # v109-F4: ``extra_writable`` is per-run (the project dependency-cache
        # root); every backend consumes this one list (Seatbelt subpaths,
        # bwrap/podman binds), so the door opens on all of them.
        writable = [
            out_path.parent,
            *git_metadata_writable_roots(workspace),
            *config.sandbox_writable_extra,
            *extra_writable,
        ]
        profile_path = sandbox.write_profile(
            log_path.parent / "sandbox.profile.sb",
            workspace=workspace,
            extra_writable=writable,
            extra_readable=git_metadata_readable_roots(workspace),
            network=network,
            proxy_port=proxy_port,
            unix_socket_path=network_proxy_unix_path if proxy_port is not None else None,
            backend=backend,
            skep_home=config.home.parent,
            # B125: only skep's own LLM-calling workers read the LLM key; an
            # external engine (worker_argv) authenticates from its env, and an
            # unregistered caste falls back to the coding worker.
            worker_llm_key=worker_argv is None
            and getattr(CASTES.get(task.worker_kind), "needs_provider", True),
            write_denied=write_denied,
            loopback_ports=run_loopback_ports(
                task.permissions.loopback_ports, task.permissions.provider_hosts, config.home
            ),
        )
        argv = sandbox.wrap_command(argv, profile_path, backend=backend)
    env = build_worker_env(task.permissions.env_allowlist, baseline=config.env_baseline)
    # Needed: the builtin worker reads its provider profile, LLM key and
    # plugin shelf from here (llm_plan.py, capabilities.py). The control-plane
    # secrets under it are hidden by the profile (ES-F4), not by omission.
    env["SKEP_HOME"] = str(config.home.parent)
    if extra_env:
        # Supervisor-injected, non-secret config (v39-F4: the routed provider's
        # endpoint/model) — same class as SKEP_HOME and the proxy env below.
        env.update(extra_env)
    if proxy_port is not None:
        env.update(_proxy_env(proxy_port))
    env.update(WORKER_GIT_ENV)  # last: nothing above may loosen it
    log_handle = log_path.open("ab")
    try:
        return subprocess.Popen(
            argv,
            env=env,
            cwd=task.workspace,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log_handle.close()
