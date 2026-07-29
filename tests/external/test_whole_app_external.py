"""Opt-in whole-app integration test with live external dependencies.

This is intentionally excluded from normal test runs. It spends live LLM budget,
requires outbound network, and requires Docker.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from skep.supervisor.cli_cmds import build_config
from skep.supervisor.container import docker_available
from skep.supervisor.serve.app import create_app
from skep.supervisor.serve.auth import TOKEN_FILE
from tests.fixtures.toy_repo import create_toy_repo

pytestmark = pytest.mark.external_app

OLLAMA_BASE_URL = "https://ollama.com"
OLLAMA_API_BASE_URL = "https://ollama.com/api"
OLLAMA_MODEL = "glm-5.2:cloud"


def _enabled() -> bool:
    return os.environ.get("SKEP_WHOLE_APP_EXTERNAL") == "1"


def _require_enabled() -> None:
    if not _enabled():
        pytest.skip("opt-in: set SKEP_WHOLE_APP_EXTERNAL=1")


def _ollama_api_key() -> str:
    _require_enabled()
    key = os.environ.get("OLLAMA_API_KEY", "").strip()
    if not key:
        pytest.fail("OLLAMA_API_KEY is required for the whole-app external test", pytrace=False)
    return key


@pytest.fixture()
def toy_repo(tmp_path: Path) -> Path:
    return create_toy_repo(tmp_path / "toyrepo")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _repo_head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _assert_no_extra_worktrees(repo: Path, worktrees_root: Path) -> None:
    leftovers = list(worktrees_root.iterdir()) if worktrees_root.is_dir() else []
    assert leftovers == [], f"leftover skep worktrees: {leftovers}"
    listed = _git(repo, "worktree", "list", "--porcelain").stdout
    assert listed.count("worktree ") == 1, f"git still tracks extra worktrees:\n{listed}"


def _stream_run_events(
    client: TestClient, task_id: str, *, headers: dict[str, str]
) -> tuple[list[dict[str, Any]], str | None]:
    events: list[dict[str, Any]] = []
    final_state = None
    with client.stream("GET", f"/api/runs/{task_id}/events?stream=1", headers=headers) as stream:
        event_name = ""
        for line in stream.iter_lines():
            if line.startswith("event: "):
                event_name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                payload = json.loads(line.removeprefix("data: "))
                if event_name == "done":
                    final_state = payload["state"]
                else:
                    events.append(payload)
                event_name = ""
    return events, final_state


def test_live_ollama_network_and_model_key() -> None:
    key = _ollama_api_key()

    response = httpx.post(
        f"{OLLAMA_API_BASE_URL}/chat",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": OLLAMA_MODEL,
            "stream": False,
            "think": False,
            "messages": [
                {
                    "role": "user",
                    "content": "Reply with exactly LIVE_LLM_OK and no other text.",
                }
            ],
        },
        timeout=120.0,
    )

    assert response.status_code == 200, response.text[:300]
    content = str(response.json().get("message", {}).get("content", ""))
    assert "LIVE_LLM_OK" in content


def test_docker_daemon_executes_a_container() -> None:
    _require_enabled()
    assert docker_available(), "Docker daemon is not reachable"

    proc = subprocess.run(
        ["docker", "run", "--rm", "alpine:latest", "sh", "-c", "echo DOCKER_OK"],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "DOCKER_OK"


def test_whole_app_live_llm_dispatch_reverify_and_approve(
    tmp_path: Path,
    toy_repo: Path,
) -> None:
    key = _ollama_api_key()
    head_before = _repo_head(toy_repo)
    config = build_config(tmp_path / "home", None)
    app = create_app(config, sse_poll_seconds=0.1, start_ticker=False)
    token = (config.home / TOKEN_FILE).read_text(encoding="utf-8").strip()

    with TestClient(app) as client:
        headers = {"X-Skep-Token": token}
        configured = client.put(
            "/api/llm/config",
            headers=headers,
            json={
                "base_url": OLLAMA_BASE_URL,
                "protocol": "ollama",
                "default_model": OLLAMA_MODEL,
                "api_key": key,
            },
        )
        assert configured.status_code == 200
        assert configured.json()["api_key_set"] is True

        probe = client.post("/api/llm/test", headers=headers, json={})
        assert probe.status_code == 200
        assert probe.json()["ok"] is True, probe.json()

        accepted = client.post(
            "/api/runs",
            headers=headers,
            json={
                "repo": str(toy_repo),
                "instructions": (
                    "Create a file named live_llm_smoke.py containing exactly "
                    'print("LIVE_LLM_SMOKE_OK"). Return only valid Skep JSON. Use '
                    "filesystem.write to create the file, then verify with shell.run using "
                    '{"argv": ["python", "live_llm_smoke.py"], "purpose": "verify"} '
                    'and set verify.expected_stdout to "LIVE_LLM_SMOKE_OK\\n".'
                ),
                "execution_mode": "workspace",
                "max_provider_calls": 1,
                "wall_clock_seconds": 300,
            },
        )
        assert accepted.status_code == 202, accepted.text
        task_id = accepted.json()["task_id"]

        events, final_state = _stream_run_events(client, task_id, headers=headers)
        event_types = [event["type"] for event in events]
        assert final_state == "completed"
        assert "task.start" in event_types
        assert "plan.created" in event_types
        assert "verify.result" in event_types
        assert "reverify.result" in event_types

        detail = client.get(f"/api/runs/{task_id}", headers=headers).json()
        assert detail["run"]["state"] == "completed"
        assert detail["run"]["verification_outcome"] == "passed"
        assert detail["run"]["worker_version"] == "coding-minimal-0.1.0"
        assert detail["usage"]["provider_calls"] == 1
        assert detail["reverification"]["confirmed"] is True

        artifacts = {artifact["kind"]: artifact for artifact in detail["artifacts"]}
        patch_text = Path(artifacts["patch"]["path"]).read_text(encoding="utf-8")
        assert "live_llm_smoke.py" in patch_text
        assert "LIVE_LLM_SMOKE_OK" in patch_text

        review = client.post(f"/api/runs/{task_id}/approvals", headers=headers)
        assert review.status_code == 201
        review_id = review.json()["review_id"]
        approved = client.post(
            f"/api/approvals/{review_id}/approve",
            headers=headers,
            json={"actor": "whole-app-external-test"},
        )
        assert approved.status_code == 200
        assert approved.json() == {"action": "applied", "branch": f"skep/{task_id}"}

        generated = _git(toy_repo, "show", f"skep/{task_id}:live_llm_smoke.py").stdout
        assert generated == 'print("LIVE_LLM_SMOKE_OK")\n'

    assert not (toy_repo / "hello.py").exists()
    assert _repo_head(toy_repo) == head_before
    _assert_no_extra_worktrees(toy_repo, toy_repo.parent / ".skep" / "worktrees")
