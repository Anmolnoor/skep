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

INPUT_STAGES = "script-inputs"  # under <skep home>/supervisor (R4: S6/N5)


def input_stage_root(skep_home: Path) -> Path:
    """Where bubblewrap stages script inputs: private (0700), in the skep
    home rather than a shared $TMPDIR, outside every run's bound roots."""
    root = skep_home / "supervisor" / INPUT_STAGES
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root


def sweep_dead_input_stages(skep_home: Path) -> list[Path]:
    """Remove the stages a dead process left (``<pid>-*``: a crash skips
    ``walled``'s cleanup); a live owner's stays, as does a name that is not
    a stage. The removed paths."""
    root = skep_home / "supervisor" / INPUT_STAGES
    if not root.is_dir():
        return []
    removed: list[Path] = []
    for stage in root.iterdir():
        owner = stage.name.partition("-")[0]
        if not owner.isdigit():
            continue
        try:
            os.kill(int(owner), 0)
        except ProcessLookupError:
            shutil.rmtree(stage, ignore_errors=True)
            removed.append(stage)
        except PermissionError:
            pass  # alive, another user's
    return removed


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
    domain_patterns: bool = False,
) -> Iterator[tuple[list[str], dict[str, str]]]:
    """``argv`` wrapped in the wall, and the env it runs with, for the life
    of the block. Reads: the ADR 0068 strict allowlist (system dirs,
    detected toolchains incl. ``<skep home>/tools/.venvs``, the workspace,
    ``read_allowlist``) and ``read_files`` as single files (R2, S7), never
    the operator's other files; the secret masks land last, and an input
    under one refuses (R3), as does anything but an ordinary file (R4:
    ``sandbox.open_read_file``). On bubblewrap each input is a snapshot
    copied at launch into a private 0700 dir under the skep home
    (``input_stage_root``; ``<pid>-*``) that this block removes — and the
    next serve start sweeps if a crash skipped that — capped per file and in
    all (``sandbox.READ_FILE_MAX_BYTES``). Writes: ``workspace`` (+
    ``writable``). Env: PATH and LANG, HOME and TMPDIR in the workspace.
    Egress: deny-all when ``hosts`` is empty, else pinned to them through a
    FilteringProxy (D1) — never allow-all: an entry that is not a concrete
    host (``*``, ``*.example.com``) refuses; ``domain_patterns`` (the forged
    lane only, R3) admits ``*.example.com``. No backend, or a wall that
    cannot be built, raises ``sandbox.SandboxUnavailableError`` (fail
    closed, I12)."""
    from . import sandbox
    from .netproxy import (
        FilteringProxy,
        control_plane_ports,
        forge_host_entry_problem,
        host_entry_problem,
    )
    from .spawner import proxy_env

    # R2 (N1): `*` would make the wall allow-all, past the proxy. R3 (S15):
    # concrete hosts only; the forged lane alone opts into `*.domain`.
    check = forge_host_entry_problem if domain_patterns else host_entry_problem
    for host in hosts:
        if (problem := check(host)) is not None:
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
        stage = None
        if read_files and probe.backend == "bubblewrap":  # R3 (S2): private, outside, removed
            stage = Path(
                tempfile.mkdtemp(prefix=f"{os.getpid()}-", dir=input_stage_root(skep_home))
            )
            stack.callback(shutil.rmtree, stage, ignore_errors=True)
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
                read_files_stage=stage,
            )
            wrapped = sandbox.wrap_command(argv, profile)
        except sandbox.SandboxAllowlistUnsupported as exc:
            raise sandbox.SandboxUnavailableError(str(exc)) from exc
        env = {name: os.environ[name] for name in ("PATH", "LANG") if name in os.environ}
        env.update(HOME=str(workspace), **sandbox.in_wall_tmp_env(workspace))
        if proxy is not None:
            env.update(proxy_env(proxy.port))
        yield wrapped, env
