"""Smoke: login, first-run setup, then dispatch through the configured project."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from skep.supervisor import SupervisorConfig
from skep.supervisor.serve.app import TERMINAL_STATES, create_app
from skep.supervisor.serve.auth import TOKEN_FILE
from tests.supervisor.fake_ollama import FakeOllama

pytestmark = pytest.mark.smoke

FAKE_WORKER = Path(__file__).parents[1] / "supervisor" / "fake_worker.py"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _repo(path: Path) -> Path:
    path.mkdir()
    (path / "existing.py").write_text("value = 0\n", encoding="utf-8")
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "smoke@example.com")
    _git(path, "config", "user.name", "Smoke")
    _git(path, "add", "existing.py")
    _git(path, "commit", "-qm", "seed")
    return path


def _wait_terminal(client: TestClient, task_id: str, headers: dict[str, str]) -> dict[str, Any]:
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        run = client.get(f"/api/runs/{task_id}", headers=headers).json()["run"]
        if run["state"] in TERMINAL_STATES:
            return dict(run)
        time.sleep(0.05)
    raise AssertionError(f"run {task_id} never reached a terminal state")


def test_login_setup_complete_then_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home-dir"))
    config = SupervisorConfig(
        home=tmp_path / "home",
        worker_command=(sys.executable, str(FAKE_WORKER)),
        grace_seconds=0.5,
        heartbeat_seconds=0.1,
        poll_seconds=0.01,
    )
    app = create_app(config, sse_poll_seconds=0.01, start_ticker=False)
    token = (config.home / TOKEN_FILE).read_text(encoding="utf-8").strip()
    headers = {"X-Skep-Token": token}
    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        with TestClient(app) as client:
            assert client.get("/api/status", headers=headers).status_code == 200
            assert client.get("/api/setup/status", headers=headers).json()["complete"] is False

            probe = client.post(
                "/api/llm/test",
                headers=headers,
                json={"base_url": ollama.base_url, "api_key": "sk-fake"},
            )
            assert probe.json() == {"ok": True, "models": 2}
            client.put(
                "/api/llm/config",
                headers=headers,
                json={
                    "base_url": ollama.base_url,
                    "api_key": "sk-fake",
                    "default_model": "llama3.2",
                },
            )

            project = client.post(
                "/api/setup/default-workspace",
                headers=headers,
                json={"apply": True},
            )
            assert project.status_code == 200
            workspace = Path(project.json()["workspace"])
            (workspace / "existing.py").write_text("value = 0\n", encoding="utf-8")
            _git(workspace, "config", "user.email", "smoke@example.com")
            _git(workspace, "config", "user.name", "Smoke")
            _git(workspace, "add", "existing.py")
            _git(workspace, "commit", "-qm", "seed smoke file")
            complete = client.post("/api/setup/complete", headers=headers)
            assert complete.status_code == 200
            assert complete.json()["complete"] is True

            created = client.post(
                "/api/runs",
                headers=headers,
                json={"repo": str(workspace), "instructions": "Fix the bug. MODE:happy"},
            )
            assert created.status_code == 202
            run = _wait_terminal(client, created.json()["task_id"], headers)
            assert run["state"] == "completed"
    finally:
        ollama.stop()
