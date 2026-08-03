from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.contracts_io import DEFAULT_BUDGET, DEFAULT_PERMISSIONS, mint_task, read_result
from skep.supervisor.dispatch import run_task
from skep.worker_contract import Permissions, TaskState, VerificationOutcome
from skep.workers.claude_code import run_claude_code_task


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def test_claude_code_adapter_runs_claude_and_writes_contract_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "README.md").write_text("# target\n", encoding="utf-8")
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "test@example.com")
    _git(workspace, "config", "user.name", "Test")
    _git(workspace, "add", "README.md")
    _git(workspace, "commit", "-qm", "seed")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_claude = bin_dir / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "from pathlib import Path\n"
        "ok = sys.argv[1:4] == ['--permission-mode', 'bypassPermissions', '--print']\n"
        "if not ok or len(sys.argv) != 5:\n"
        "    raise SystemExit(12)\n"
        "Path('claude_created.py').write_text('print(\"from claude\")\\n', encoding='utf-8')\n"
        "print('edited workspace')\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    task = mint_task(
        workspace=workspace,
        instructions="Create a Python file through Claude Code.",
        permissions=DEFAULT_PERMISSIONS,
        budget=DEFAULT_BUDGET,
    )
    task_path = tmp_path / "task.json"
    task_path.write_text(task.model_dump_json(indent=2), encoding="utf-8")
    out_path = tmp_path / "result.json"

    exit_code = run_claude_code_task(task_path, out_path)

    assert exit_code == 0
    result = read_result(out_path)
    assert result.status is TaskState.COMPLETED
    assert result.verification.outcome is VerificationOutcome.PASSED
    assert result.changed_files == ["claude_created.py"]
    assert result.usage is not None
    assert result.usage.provider_calls == 1
    assert {artifact.kind for artifact in result.artifacts} == {"event_log", "patch"}
    patch_artifact = next(artifact for artifact in result.artifacts if artifact.kind == "patch")
    patch_text = (workspace / patch_artifact.path).read_text(encoding="utf-8")
    assert "claude_created.py" in patch_text
    assert 'print("from claude")' in patch_text


def test_supervisor_can_spawn_claude_code_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "README.md").write_text("# target\n", encoding="utf-8")
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "test@example.com")
    _git(workspace, "config", "user.name", "Test")
    _git(workspace, "add", "README.md")
    _git(workspace, "commit", "-qm", "seed")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_claude = bin_dir / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        "Path('spawned_by_supervisor.py').write_text('print(\"spawned\")\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parents[2] / "src"))

    config = SupervisorConfig(
        home=tmp_path / "home",
        worker_command=(sys.executable, "-m", "skep.workers.claude_code"),
        env_baseline=("PATH", "HOME", "PYTHONPATH"),
        sandbox=False,
    )

    outcome = run_task(workspace, "Create a Python file through Claude Code.", config=config)

    assert outcome.record.state == "completed"
    assert outcome.record.worker_version == "claude-code-adapter-0.1.0"
    assert outcome.record.verification_outcome == "passed"
    assert not (workspace / "spawned_by_supervisor.py").exists()


def test_supervisor_passes_claude_code_command_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "README.md").write_text("# target\n", encoding="utf-8")
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "test@example.com")
    _git(workspace, "config", "user.name", "Test")
    _git(workspace, "add", "README.md")
    _git(workspace, "commit", "-qm", "seed")

    bin_dir = tmp_path / "custom-bin"
    bin_dir.mkdir()
    custom_claude = bin_dir / "custom-claude"
    custom_claude.write_text(
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        "Path('custom_env_claude.py').write_text('print(\"custom\")\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    custom_claude.chmod(0o755)
    monkeypatch.setenv("SKEP_CLAUDE_CODE_CMD", str(custom_claude))
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parents[2] / "src"))

    config = SupervisorConfig(
        home=tmp_path / "home",
        worker_command=(sys.executable, "-m", "skep.workers.claude_code"),
        env_baseline=("PATH", "HOME", "PYTHONPATH"),
        sandbox=False,
    )
    permissions = Permissions(
        read=["workspace"],
        write=["workspace"],
        network=[],
        env_allowlist=["SKEP_CLAUDE_CODE_CMD"],
    )

    outcome = run_task(
        workspace,
        "Create a Python file through a custom Claude Code executable.",
        config=config,
        permissions=permissions,
        execution_mode="workspace",
    )

    assert outcome.record.state == "completed"
    assert outcome.record.worker_version == "claude-code-adapter-0.1.0"
    assert outcome.record.verification_outcome == "passed"
    assert not (workspace / "custom_env_claude.py").exists()
