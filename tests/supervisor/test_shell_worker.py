from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.contracts_io import DEFAULT_BUDGET, DEFAULT_PERMISSIONS, mint_task, read_result
from skep.supervisor.dispatch import run_task
from skep.worker_contract import Permissions, TaskState, VerificationOutcome
from skep.workers.shell_worker import run_shell_worker_task


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def test_shell_worker_runs_configured_agent_and_writes_contract_result(
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

    fake_agent = tmp_path / "fake-agent.py"
    fake_agent.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "prompt = sys.argv[-1]\n"
        "Path('shell_created.py').write_text(prompt + '\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SKEP_SHELL_WORKER_CMD", f"{sys.executable} {fake_agent}")

    task = mint_task(
        workspace=workspace,
        instructions="Create a Python file through a generic shell worker.",
        permissions=DEFAULT_PERMISSIONS,
        budget=DEFAULT_BUDGET,
    )
    task_path = tmp_path / "task.json"
    task_path.write_text(task.model_dump_json(indent=2), encoding="utf-8")
    out_path = tmp_path / "result.json"

    exit_code = run_shell_worker_task(task_path, out_path)

    assert exit_code == 0
    result = read_result(out_path)
    assert result.status is TaskState.COMPLETED
    assert result.verification.outcome is VerificationOutcome.PASSED
    assert result.changed_files == ["shell_created.py"]
    assert result.usage is not None
    assert result.usage.provider_calls == 1
    assert {artifact.kind for artifact in result.artifacts} == {"event_log", "patch"}
    assert (workspace / "shell_created.py").read_text(encoding="utf-8") == (
        "Create a Python file through a generic shell worker.\n"
    )


def test_supervisor_can_spawn_shell_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "README.md").write_text("# target\n", encoding="utf-8")
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "test@example.com")
    _git(workspace, "config", "user.name", "Test")
    _git(workspace, "add", "README.md")
    _git(workspace, "commit", "-qm", "seed")

    fake_agent = tmp_path / "fake-agent.py"
    fake_agent.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "Path('spawned_by_shell_worker.py').write_text(sys.argv[-1] + '\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SKEP_SHELL_WORKER_CMD", f"{sys.executable} {fake_agent}")
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parents[2] / "src"))

    config = SupervisorConfig(
        home=tmp_path / "home",
        worker_command=(sys.executable, "-m", "skep.workers.shell_worker"),
        env_baseline=("PATH", "HOME", "PYTHONPATH"),
        sandbox=False,
    )
    permissions = Permissions(
        read=["workspace"],
        write=["workspace"],
        network=[],
        env_allowlist=["SKEP_SHELL_WORKER_CMD"],
    )

    outcome = run_task(
        workspace,
        "Create a Python file through a shell prompt wrapper.",
        config=config,
        permissions=permissions,
        execution_mode="workspace",
    )

    assert outcome.record.state == "completed"
    assert outcome.record.worker_version == "shell-worker-0.1.0"
    assert outcome.record.verification_outcome == "passed"
    assert not (workspace / "spawned_by_shell_worker.py").exists()


def test_worker_docs_list_shell_worker_as_supported() -> None:
    docs = (Path(__file__).parents[2] / "docs" / "workers.md").read_text(encoding="utf-8")

    assert "Generic shell worker" in docs
    assert "python -m skep.workers.shell_worker" in docs
