"""The strict wall for supervisor-launched helpers (Phase 07 7.1; Phase 09 R1).

Forged tools (``mcp_client.forged_lane``) and script schedules
(``scheduler.run_schedule_script``) run inside this one wall, so neither
drifts from the other.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path


@contextlib.contextmanager
def walled(
    argv: list[str],
    *,
    skep_home: Path,
    hosts: tuple[str, ...],
    workspace: Path,
    writable: tuple[Path, ...] = (),
    read_allowlist: tuple[Path, ...] = (),
    read_files: tuple[Path, ...] = (),
) -> Iterator[tuple[list[str], dict[str, str]]]:
    """``argv`` wrapped in the wall, and the env it runs with, for the life
    of the block. Reads: the ADR 0068 strict allowlist (system dirs, detected
    toolchains incl. ``<skep home>/tools/.venvs``, the workspace,
    ``read_allowlist``) and ``read_files`` as single files (R2, S7), never
    the operator's other files; the secret masks
    land last. Writes: ``workspace`` (+ ``writable``). Env: PATH and LANG,
    HOME and TMPDIR in the workspace. Egress: deny-all when ``hosts`` is
    empty, else pinned to them through a FilteringProxy (D1) — never
    allow-all: an entry that is not a concrete host (``*``) refuses. No
    backend, or a wall that cannot be built, raises
    ``sandbox.SandboxUnavailableError`` (fail closed, I12)."""
    from . import sandbox
    from .netproxy import FilteringProxy, control_plane_ports, host_entry_problem
    from .spawner import proxy_env

    for host in hosts:  # R2 (N1): `*` would make the wall allow-all, past the proxy
        if (problem := host_entry_problem(host)) is not None:
            raise sandbox.SandboxUnavailableError(f"the wall's hosts: {problem}")
    probe = sandbox.availability()
    if not probe.usable:
        raise sandbox.SandboxUnavailableError(f"no usable sandbox backend ({probe.reason})")
    with contextlib.ExitStack() as stack:
        proxy = None
        if hosts:
            socket_path = None
            if probe.backend == "bubblewrap":  # the netns reaches the proxy over a unix socket
                from .dispatch import proxy_socket_root

                socket_dir = tempfile.mkdtemp(prefix="skep-fx-", dir=proxy_socket_root())
                stack.callback(shutil.rmtree, socket_dir, ignore_errors=True)
                socket_path = str(Path(socket_dir) / "p.sock")
            proxy = FilteringProxy(
                hosts,
                unix_socket_path=socket_path,
                denied_loopback_ports=control_plane_ports(skep_home / "supervisor"),
            ).start()
            stack.callback(proxy.stop)
        try:
            profile = sandbox.write_profile(
                workspace / "sandbox.profile",
                workspace=workspace,
                extra_writable=writable,
                network=(
                    sandbox.NetworkPolicy(allow=tuple(hosts)) if hosts else sandbox.DENY_ALL_NETWORK
                ),
                proxy_port=None if proxy is None else proxy.port,
                unix_socket_path=None if proxy is None else proxy.unix_socket_path,
                skep_home=skep_home,
                read_allowlist=read_allowlist,
                read_files=read_files,
            )
            wrapped = sandbox.wrap_command(argv, profile)
        except sandbox.SandboxAllowlistUnsupported as exc:
            raise sandbox.SandboxUnavailableError(str(exc)) from exc
        env = {name: os.environ[name] for name in ("PATH", "LANG") if name in os.environ}
        env.update(HOME=str(workspace), **sandbox.in_wall_tmp_env(workspace))
        if proxy is not None:
            env.update(proxy_env(proxy.port))
        yield wrapped, env
