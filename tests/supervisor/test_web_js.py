"""T3-1: the web UI's render code runs under ``node --test`` (tests/web/).

The UI is no-build (no npm), so the JS suite uses node's built-in runner and a
fake DOM in ``tests/web/harness.mjs``. Wrapped here so the pytest gate and CI
run it with everything else. Without node a local run skips; under CI
(GitHub sets ``CI``) a missing node is a failure, never a silent skip.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

WEB_TESTS = Path(__file__).parents[1] / "web"


def test_web_ui_js_suite() -> None:
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("node is required in CI to run tests/web/")
        pytest.skip("node not installed — tests/web/ needs it")
    files = sorted(str(p) for p in WEB_TESTS.glob("*.test.mjs"))
    assert files, "no tests/web/*.test.mjs found"
    proc = subprocess.run(
        [node, "--test", *files], capture_output=True, text=True, timeout=120, check=False
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
