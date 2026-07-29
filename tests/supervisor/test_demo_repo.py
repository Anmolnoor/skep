from __future__ import annotations

import runpy
import subprocess
import sys
from pathlib import Path

from skep.worker_contract import CONTRACT_VERSION


def test_demo_repo_seed_has_a_working_test_suite() -> None:
    demo = Path(__file__).resolve().parents[2] / "examples" / "skep-demo"

    assert (demo / "README.md").is_file()
    assert (demo / "app.py").is_file()
    assert (demo / "tests" / "test_app.py").is_file()

    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=demo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_demo_repo_readme_invites_visitors_to_try_skep_here() -> None:
    readme = (
        Path(__file__).resolve().parents[2] / "examples" / "skep-demo" / "README.md"
    ).read_text(encoding="utf-8")

    assert "try skep on this repo" in readme.lower()


def test_demo_worker_uses_current_contract_version() -> None:
    demo_worker = Path(__file__).resolve().parents[2] / "scripts" / "demo_worker.py"

    module = runpy.run_path(str(demo_worker))

    assert module["CONTRACT_VERSION"] == CONTRACT_VERSION


def test_record_demo_uses_project_python_for_demo_worker() -> None:
    script = (Path(__file__).resolve().parents[2] / "scripts" / "record-demo-gif.sh").read_text(
        encoding="utf-8"
    )

    assert "WORKER_SCRIPT=" in script
    assert "DEMO_WORKER_CMD=" in script
    assert (
        'uv --project $(shell_quote "$ROOT") run python $(shell_quote "$WORKER_SCRIPT")'
    ) in script
    assert '--worker-cmd "$DEMO_WORKER_CMD"' in script
    assert '--worker-cmd "$WORKER_CMD"' not in script
