from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from skep.supervisor import SupervisorConfig
from skep.supervisor.serve import create_app
from skep.supervisor.serve.app import TERMINAL_STATES
from skep.supervisor.serve.auth import TOKEN_FILE

FAKE_WORKER = Path(__file__).with_name("fake_worker.py")


def serve_client(config: SupervisorConfig, **app_kwargs: Any) -> TestClient:
    """An authenticated TestClient over a fresh serve app (v5)."""
    app_kwargs.setdefault("sse_poll_seconds", 0.05)
    app = create_app(config, **app_kwargs)
    token = (config.home / TOKEN_FILE).read_text().strip()
    return TestClient(app, headers={"X-Skep-Token": token})


def wait_terminal(client: TestClient, task_id: str, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = client.get(f"/api/runs/{task_id}").json()["run"]
        if run["state"] in TERMINAL_STATES:
            return dict(run)
        time.sleep(0.05)
    raise AssertionError(f"run {task_id} never reached a terminal state")


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "existing.py").write_text("value = 0\n")
    git(repo, "add", "existing.py")
    git(repo, "commit", "-qm", "seed")
    return repo


@pytest.fixture()
def config(tmp_path: Path) -> SupervisorConfig:
    return SupervisorConfig(
        home=tmp_path / "skep-home",
        worker_command=(sys.executable, str(FAKE_WORKER)),
        grace_seconds=0.5,
        heartbeat_seconds=0.1,
        poll_seconds=0.01,
    )
