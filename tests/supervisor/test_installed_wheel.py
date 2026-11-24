"""Phase 10 R1 (spec 10.1.3, Test 10.1): zero egress and real 401/404 on the
INSTALLED wheel, not the source tree.

The source-tree crawl (test_serve_ui) cannot see a packaging miss: a module or
font the wheel forgot still answers 200 from ``src/``. This builds the wheel,
installs it into a throwaway venv, serves the UI from that install with a
throwaway home, and crawls it as a browser would. Offline: the wheel builds
with ``UV_OFFLINE`` (only a cold uv cache — a fresh checkout, the release
gate's private ``UV_CACHE_DIR`` — fetches the build backend once), installs
``--no-deps``, and its runtime deps come from this test's own environment
through a ``.pth`` — the venv's own site-packages (the wheel's ``skep``)
always wins over it.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
import pytest

from .test_serve_ui import _LOADS

# Absolute URLs the served UI may spell without loading them — each a name or
# text, never a request (the crawl below proves nothing loads off-daemon).
# Anything else absolute in the served HTML/JS/CSS fails the test.
_SPELLED_NOT_LOADED = {
    "http://www.w3.org/2000/svg",  # createElementNS's namespace: a name
    "https://ollama.com",  # an input placeholder
    "http://localhost:11434",  # an input placeholder (the local ollama default)
    "https://ollama.com/settings",  # a link the operator clicks, never fetched
    "https://github.com/you/project.git",  # an input placeholder
}
_ABSOLUTE = re.compile(r"https?://[^\s\"'`)<>]+")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _step(*args: str | Path, offline: bool = True) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "UV_OFFLINE": "1"} if offline else dict(os.environ)
    return subprocess.run(
        [str(a) for a in args], capture_output=True, text=True, timeout=180, env=env
    )


def _run(*args: str | Path) -> str:
    """One offline step; its stdout. A failure shows uv's own words."""
    done = _step(*args)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _build_wheel(uv: str, dist: Path, root: Path) -> None:
    """Offline when uv's cache holds the build backend. The release gate
    failed on v2.0.0's tag because its private cache did not: a cold cache
    fetches the backend, once, and every later step stays offline."""
    build = (uv, "build", "--wheel", "--out-dir", dist, root)
    done = _step(*build)
    if done.returncode != 0 and "network was disabled" in done.stderr:
        done = _step(*build, offline=False)
    assert done.returncode == 0, done.stderr


def _install_wheel(tmp_path: Path, uv: str) -> Path:
    """Build the wheel and install it into a fresh venv; its python."""
    root = Path(__file__).resolve().parents[2]
    dist = tmp_path / "dist"
    _build_wheel(uv, dist, root)
    (wheel,) = dist.glob("skep-*.whl")
    venv = tmp_path / "venv"
    _run(uv, "venv", "--python", sys.executable, venv)
    python = venv / "bin" / "python"
    _run(uv, "pip", "install", "--no-deps", "--python", python, wheel)
    purelib = _run(python, "-I", "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])")
    deps = [p for p in sys.path if p.endswith("site-packages")]
    (Path(purelib) / "_skep_test_deps.pth").write_text("\n".join(deps) + "\n")
    return python


def test_the_installed_wheel_serves_every_asset_and_keeps_real_api_statuses(
    tmp_path: Path,
) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not installed: it builds and installs the wheel under test")
    python = _install_wheel(tmp_path, uv)
    located = _run(python, "-I", "-c", "import skep; print(skep.__file__)")
    static = Path(located).parent / "supervisor" / "serve" / "static"
    assert Path(located).is_relative_to(tmp_path / "venv"), located  # never src/

    home = tmp_path / "home"
    home.mkdir()
    port = _free_port()
    env = {**os.environ, "HOME": str(home), "SKEP_HOME": str(home / ".skep")}
    log = (tmp_path / "serve.log").open("w")
    serve = subprocess.Popen(
        [str(python), "-I", "-m", "skep", "serve", "--port", str(port)],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(base_url=base, timeout=5.0) as client:
            deadline = time.monotonic() + 30
            while True:
                assert serve.poll() is None, (tmp_path / "serve.log").read_text()
                try:
                    client.get("/api/status")
                    break
                except httpx.TransportError:
                    assert time.monotonic() < deadline, (tmp_path / "serve.log").read_text()
                    time.sleep(0.1)

            # The crawl: index -> stylesheet and modules -> imports -> fonts.
            seen: set[str] = set()
            spelled: set[str] = set()
            queue = ["/"]
            while queue:
                path = queue.pop()
                if path in seen:
                    continue
                seen.add(path)
                response = client.get(path)
                assert response.status_code == 200, path
                if not path.endswith((".html", ".js", ".css", "/")):
                    continue  # a font: a leaf
                spelled.update(_ABSOLUTE.findall(response.text))
                for pattern in _LOADS:
                    for ref in re.findall(pattern, response.text, flags=re.MULTILINE):
                        parts = urlsplit(ref)
                        assert not parts.scheme and not parts.netloc, f"{path} loads {ref}"
                        queue.append(urljoin(path, ref))
            assert spelled <= _SPELLED_NOT_LOADED, sorted(spelled - _SPELLED_NOT_LOADED)
            shipped = {
                "/static/" + p.relative_to(static).as_posix()
                for p in static.rglob("*")
                if p.suffix in {".js", ".css", ".woff2"}
            }
            assert shipped and shipped <= seen, sorted(shipped - seen)
            assert client.get("/static/app.js").content == (static / "app.js").read_bytes()

            # Real API statuses: no SPA fallback swallows a miss.
            assert client.get("/api/invalid").status_code == 401
            token = (home / ".skep" / "supervisor" / "serve-token").read_text().strip()
            missing = client.get("/api/invalid", headers={"X-Skep-Token": token})
            assert missing.status_code == 404
            assert missing.headers["content-type"].startswith("application/json")
            assert client.get("/no-such-page").status_code == 404
    finally:
        serve.terminate()
        try:
            serve.wait(timeout=15)
        except subprocess.TimeoutExpired:
            serve.kill()
            serve.wait()
        log.close()
