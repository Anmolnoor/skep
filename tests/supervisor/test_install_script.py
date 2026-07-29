"""v27-F3: scripts/install.sh — OS-detect, uv-first, honest about bubblewrap."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "install.sh"


def test_install_script_is_executable_strict_bash() -> None:
    assert SCRIPT.is_file()
    assert SCRIPT.stat().st_mode & stat.S_IXUSR
    text = SCRIPT.read_text(encoding="utf-8")
    assert "set -euo pipefail" in text
    syntax = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr


def test_dry_run_narrates_the_source_checkout_path_and_changes_nothing() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "--dry-run"], capture_output=True, text=True, cwd=ROOT
    )
    assert result.returncode == 0, result.stderr
    assert "source checkout detected" in result.stdout
    assert "would run: uv sync" in result.stdout
    # The first-run story is part of the install.
    assert "setup --personal" in result.stdout
    assert "skep serve" in result.stdout or "uv run skep serve" in result.stdout


def test_unsupported_os_is_refused() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "--dry-run"],
        capture_output=True,
        text=True,
        env={**os.environ, "SKEP_INSTALL_OS": "SunOS"},
    )
    assert result.returncode == 1
    assert "unsupported OS" in result.stderr


def test_unknown_argument_is_refused() -> None:
    result = subprocess.run(["bash", str(SCRIPT), "--yolo"], capture_output=True, text=True)
    assert result.returncode == 2
    assert "unknown argument" in result.stderr
