"""Q1-B / G3: the container portability seam.

The hermetic tests pin the `docker run` argv shape and run everywhere. The live
proof (marked ``container``, gated by SKEP_CONTAINER=1 + a Docker daemon) shows D1
enforcement is *identical* inside a container: a worker reaches an allowlisted host
only through the same host-side filtering proxy, and a non-allowlisted host is
403'd — the macOS Seatbelt+proxy boundary, ported to Linux/containers.
"""

from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from skep.supervisor.container import (
    CONTAINER_WORKSPACE,
    HOST_PROXY_ALIAS,
    build_run_argv,
    docker_available,
)
from skep.supervisor.netproxy import FilteringProxy


def test_dockerfile_uses_in_repo_default_worker() -> None:
    text = (Path(__file__).parents[2] / "Dockerfile").read_text(encoding="utf-8")

    assert "SKEP_HOME=/data/skep" in text
    assert "SKEP_WORKER_CMD" not in text
    assert 'ENTRYPOINT ["skep", "serve"' in text
    assert "bubblewrap" in text


def test_local_image_build_uses_skep_context_only() -> None:
    root = Path(__file__).parents[2]
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    makefile = (root / "Makefile").read_text(encoding="utf-8")
    dockerignore = (root / "Dockerfile.dockerignore").read_text(encoding="utf-8")

    assert "context: ." in compose
    assert "dockerfile: Dockerfile" in compose
    assert "docker build -f Dockerfile -t ghcr.io/anmolnoor/skep:dev ." in makefile
    assert "node_modules" in dockerignore
    assert ".claude" in dockerignore
    assert "output" in dockerignore
    assert "dist" in dockerignore


def test_default_smoke_target_uses_first_party_suite() -> None:
    makefile = (Path(__file__).parents[2] / "Makefile").read_text(encoding="utf-8")

    assert "uv run pytest tests/smoke -m smoke" in makefile


def test_build_run_argv_mounts_workspace_and_routes_proxy(tmp_path: Path) -> None:
    argv = build_run_argv(
        image="python:3.12-slim",
        worker_argv=["python", "-m", "skep.workers.audit", "--headless"],
        workspace=tmp_path,
        proxy_port=9999,
    )
    assert argv[:3] == ["docker", "run", "--rm"]
    # workspace is the only mount, at the container workdir
    assert f"{tmp_path}:{CONTAINER_WORKSPACE}" in argv
    assert argv[argv.index("-w") + 1] == CONTAINER_WORKSPACE
    # the host proxy is reachable and wired into the worker's HTTP env
    assert f"{HOST_PROXY_ALIAS}:host-gateway" in argv
    assert f"HTTPS_PROXY=http://{HOST_PROXY_ALIAS}:9999" in argv
    # image precedes the worker argv
    image_at = argv.index("python:3.12-slim")
    assert argv[image_at + 1 :] == ["python", "-m", "skep.workers.audit", "--headless"]


def test_build_run_argv_without_proxy_has_no_proxy_env(tmp_path: Path) -> None:
    argv = build_run_argv(image="alpine", worker_argv=["true"], workspace=tmp_path)
    assert not any(part.startswith("HTTPS_PROXY=") for part in argv)


# --- opt-in live proof -------------------------------------------------------


class _OriginHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"ORIGIN-OK"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture()
def origin_port() -> Iterator[int]:
    # The proxy runs on the host and connects to the origin there, so 127.0.0.1 is
    # the right bind: the container never touches the origin directly.
    server = HTTPServer(("127.0.0.1", 0), _OriginHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def _wget_in_container(proxy: FilteringProxy, url: str, workspace: Path) -> str:
    # `-Y on` forces BusyBox wget through the proxy (no localhost bypass).
    argv = build_run_argv(
        image="alpine:latest",
        worker_argv=["wget", "-Y", "on", "-q", "-T", "8", "-O", "-", url],
        workspace=workspace,
        proxy_port=proxy.port,
    )
    proc = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=90)
    return proc.stdout


@pytest.mark.container
@pytest.mark.skipif(
    os.environ.get("SKEP_CONTAINER") != "1" or not docker_available(),
    reason="opt-in: set SKEP_CONTAINER=1 with a running Docker daemon",
)
def test_container_d1_enforcement_via_host_proxy(tmp_path: Path, origin_port: int) -> None:
    # The container's wget targets the host origin by name through the host proxy;
    # the proxy resolves "localhost" on the host side and decides by the allowlist.
    url = f"http://localhost:{origin_port}/"

    allow = FilteringProxy(("localhost",)).start()
    try:
        out = _wget_in_container(allow, url, tmp_path)
        assert "ORIGIN-OK" in out, f"allowlisted host should be reachable via the proxy: {out!r}"
        assert allow.allowed_count >= 1
    finally:
        allow.stop()

    deny = FilteringProxy(("pypi.org",)).start()  # the origin host is NOT allowed
    try:
        out = _wget_in_container(deny, url, tmp_path)
        assert "ORIGIN-OK" not in out, "non-allowlisted host must be blocked by the proxy"
        assert deny.denied_count >= 1
    finally:
        deny.stop()
