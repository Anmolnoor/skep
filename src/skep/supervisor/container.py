"""Q1-B / G3: containerized worker isolation for Linux portability (v3, opt-in).

macOS gets its physical boundary from Seatbelt (ADR 0005) plus the D1 filtering
proxy (ADR 0011). Linux / CI gets it from a container: the worker runs inside a
container with only its workspace mounted, and its HTTP(S) tooling routed through
the *same* host-side ``FilteringProxy`` (reached at ``host.docker.internal``). So
D1 domain enforcement is identical across platforms — only the egress *pin*
differs: Seatbelt on macOS, an iptables egress-drop in the container's network
namespace on Linux (designed in the ADR; that drop is the heavy part deferred —
the bridge network here still permits direct egress, so the proxy is enforced but
not yet unbypassable in-container).

This module is just the seam — it builds the ``docker run`` argv. Running a
container is opt-in and never the gate (the acceptance bar must not require
Docker, Q10); the live proof is marked ``container`` and gated by SKEP_CONTAINER=1.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

CONTAINER_WORKSPACE = "/workspace"
HOST_PROXY_ALIAS = "host.docker.internal"


def docker_available() -> bool:
    """True when a usable Docker daemon is reachable on this host."""
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(["docker", "info"], capture_output=True, check=False)
    return probe.returncode == 0


def proxy_env(proxy_port: int) -> dict[str, str]:
    """Env routing the container's HTTP tooling through the host proxy (D1)."""
    url = f"http://{HOST_PROXY_ALIAS}:{proxy_port}"
    return {
        "HTTP_PROXY": url,
        "HTTPS_PROXY": url,
        "ALL_PROXY": url,
        "http_proxy": url,
        "https_proxy": url,
        "all_proxy": url,
        "NO_PROXY": "",
        "no_proxy": "",
    }


def build_run_argv(
    *,
    image: str,
    worker_argv: Sequence[str],
    workspace: Path,
    env: Mapping[str, str] | None = None,
    proxy_port: int | None = None,
    network: str = "bridge",
    workdir: str = CONTAINER_WORKSPACE,
) -> list[str]:
    """Build the ``docker run`` argv to execute a worker in a container.

    The workspace is the only host path mounted (rw); ``--add-host`` lets the
    container reach the host proxy; when ``proxy_port`` is set the worker's HTTP
    env points at it (so D1 enforcement is identical to the macOS path).
    """
    argv = [
        "docker",
        "run",
        "--rm",
        "--network",
        network,
        "--add-host",
        f"{HOST_PROXY_ALIAS}:host-gateway",
        "-v",
        f"{workspace}:{workdir}",
        "-w",
        workdir,
    ]
    merged: dict[str, str] = dict(env or {})
    if proxy_port is not None:
        merged.update(proxy_env(proxy_port))
    for name, value in merged.items():
        argv += ["-e", f"{name}={value}"]
    argv.append(image)
    argv += list(worker_argv)
    return argv
