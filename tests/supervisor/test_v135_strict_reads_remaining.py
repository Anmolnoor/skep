"""v135 Wave 1 (ADR 0068 follow-up, ADR 0069 §3): the last denylist-only lanes.

Before: the ``run_code`` fast lane and ``diagnose_run`` wrote denylist-only
profiles, so either could read ``~/Downloads``, ``~/Documents``, ``~/.ssh``
(past the secret floor's list) and every sibling checkout. Now the fast lane
reads the strict allowlist wherever the backend enforces one, and
``diagnose_run`` takes the wall the run's dispatch recorded — the one G10
already takes, through the same reader, with the same refusals (I2, I12).

The real-wall tests run where bubblewrap or Seatbelt is the backend (Linux
CI, the macOS job); every canary sits under a FAKE home in ``tmp_path``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import sysconfig
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import skep
from skep.supervisor import sandbox
from skep.supervisor.serve import fastlane
from skep.supervisor.serve.fastlane import run_code_fast

from .conftest import git

needs_strict_wall = pytest.mark.skipif(
    sandbox.availability().backend not in sandbox.STRICT_READ_BACKENDS,
    reason="real sandbox proof (runs where bwrap or sandbox-exec is the backend)",
)

# {path: "read" | the error's name}; a directory counts as read when listed.
_PROBE = """
import json, os, sys
seen = {}
for path in sys.argv[1:]:
    try:
        os.listdir(path) if os.path.isdir(path) else open(path).read()
        seen[path] = "read"
    except OSError as exc:
        seen[path] = type(exc).__name__
print(json.dumps(seen))
"""
_TINY_TEST = "def test_tiny():\n    assert 1 + 1 == 2\n"


@pytest.fixture(autouse=True)
def clear_sandbox_probe_cache() -> Iterator[None]:
    sandbox.availability.cache_clear()
    yield
    sandbox.availability.cache_clear()


@pytest.fixture(autouse=True)
def no_other_homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from skep.supervisor import secret_floor

    monkeypatch.setattr(secret_floor, "HOME_DIRS", (f"{tmp_path}/no-homes/*",))
    for name in (
        *sandbox._SOCKET_ENV,
        "UV_PYTHON_INSTALL_DIR",
        "UV_TOOL_DIR",
        "CARGO_HOME",
        "RUSTUP_HOME",
        "NVM_DIR",
        "PYENV_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "fakehome"
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture()
def canaries(fake_home: Path) -> list[Path]:
    """The plan's four: Downloads, Documents, .ssh and a sibling repo."""
    made = []
    for rel in (
        "Downloads/canary-downloads.txt",
        "Documents/canary-documents.txt",
        ".ssh/id_ed25519",
        "code/sibling-repo/canary-sibling.txt",
    ):
        path = fake_home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"CANARY {rel}\n", encoding="utf-8")
        made.append(path)
    git(fake_home / "code" / "sibling-repo", "init", "-q")
    return made


def _reads(seen: dict[str, str]) -> list[str]:
    return [path for path, verdict in seen.items() if verdict == "read"]


# --- the fast lane -----------------------------------------------------------


def _probe_script(paths: list[Path]) -> str:
    return f"import sys\nsys.argv[1:] = {[str(p) for p in paths]!r}\n{_PROBE}"


@needs_strict_wall
def test_the_fast_lane_reads_no_home_canary(canaries: list[Path], tmp_path: Path) -> None:
    result = run_code_fast("python", _probe_script(canaries), skep_home=tmp_path / "skep")
    assert result is not None and result["exit_code"] == 0, result
    seen = json.loads(result["output"])
    assert len(seen) == 4 and _reads(seen) == [], seen


@needs_strict_wall
def test_the_fast_lane_still_runs_python_and_pytest(fake_home: Path, tmp_path: Path) -> None:
    code = (
        "import pathlib, subprocess, sys\n"
        f"pathlib.Path('test_tiny.py').write_text({_TINY_TEST!r})\n"
        "proc = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', "
        "'no:cacheprovider', 'test_tiny.py'], capture_output=True, text=True)\n"
        "print(proc.stdout, proc.stderr)\n"
        "raise SystemExit(proc.returncode)\n"
    )
    result = run_code_fast("python", code, skep_home=tmp_path / "skep")
    assert result is not None and result["exit_code"] == 0, result
    assert "1 passed" in result["output"]


@needs_strict_wall
def test_the_fast_lane_starts_an_interpreter_that_lives_in_a_venv_under_home(
    canaries: list[Path], fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `uv tool install` or pipx skep: its python is a venv under $HOME.
    The wall binds that venv (never the home) and the script still runs."""
    venv = fake_home / ".local" / "share" / "uv" / "tools" / "skep"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    # skep importable from it, as installed (the wall's Landlock step runs
    # `python -m skep.landlock`): this checkout's packages, chained by a .pth
    purelib = Path(sysconfig.get_path("purelib", vars={"base": venv, "platbase": venv}))
    chained = [p for p in sys.path if Path(p).name in ("site-packages", "dist-packages")]
    (purelib / "skep-under-test.pth").write_text(
        "\n".join([*chained, str(Path(skep.__file__).parents[1])]) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(sys, "executable", str(venv / "bin" / "python"))
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(sys, "exec_prefix", str(venv))
    code = f"import sys\nprint(sys.prefix)\n{_probe_script(canaries)}"
    result = run_code_fast("python", code, skep_home=tmp_path / "skep")
    assert result is not None and result["exit_code"] == 0, result
    prefix, probe = result["output"].splitlines()
    assert Path(prefix).resolve() == venv.resolve()
    assert _reads(json.loads(probe)) == []


@pytest.mark.parametrize(
    ("backend", "expected"), [("bubblewrap", ()), ("seatbelt", ()), ("podman", None)]
)
def test_the_fast_lane_asks_for_the_strict_wall_where_the_backend_holds_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend: str, expected: Any
) -> None:
    """podman cannot confine reads to an allowlist: it keeps denylist-only
    reads rather than lose the fast lane (``write_profile`` would refuse)."""
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        fastlane, "availability", lambda: sandbox.SandboxAvailability(True, backend=backend)
    )
    monkeypatch.setattr(fastlane, "write_profile", lambda path, **kw: seen.update(kw) or path)
    monkeypatch.setattr(fastlane, "wrap_command", lambda argv, profile: ["true"])
    run_code_fast("python", "print(1)", skep_home=tmp_path / "skep")
    assert seen["read_allowlist"] == expected
