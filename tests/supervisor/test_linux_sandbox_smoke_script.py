from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_release_checklist_points_to_linux_sandbox_smoke_script() -> None:
    script = ROOT / "scripts" / "linux-sandbox-smoke.sh"
    checklist = (ROOT / "docs" / "release-checklist.md").read_text(encoding="utf-8")

    assert script.is_file()
    assert "scripts/linux-sandbox-smoke.sh" in checklist


def test_linux_sandbox_smoke_script_runs_sandbox_mode_on_disposable_repo() -> None:
    script = (ROOT / "scripts" / "linux-sandbox-smoke.sh").read_text(encoding="utf-8")

    assert "uname -s" in script
    assert "command -v bwrap" in script
    assert "mktemp -d" in script
    assert 'mktemp -d "$ROOT/.skep-linux-sandbox.XXXXXX"' in script
    assert "${TMPDIR:-/tmp}/skep-linux-sandbox" not in script
    assert "--execution-mode sandbox" in script
    assert 'execution_mode == "sandbox"' in script
    assert "LINUX SANDBOX SMOKE PASS" in script


def test_linux_sandbox_smoke_uses_supported_default_worker_task() -> None:
    script = (ROOT / "scripts" / "linux-sandbox-smoke.sh").read_text(encoding="utf-8")

    assert "Create a simple hello world in Python." in script
    assert "sandbox_smoke.txt" not in script


def test_linux_sandbox_smoke_workspace_is_ignored() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

    assert ".skep-linux-sandbox.*" in gitignore
