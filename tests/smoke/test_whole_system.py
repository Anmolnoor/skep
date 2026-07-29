"""Default whole-system smoke: serve API -> first-party worker -> approval branch.

This stays in the normal `make smoke` path: deterministic, offline, and no
external worker checkout.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from skep.supervisor.cli_cmds import build_config
from skep.supervisor.serve.app import create_app
from skep.supervisor.serve.auth import TOKEN_FILE

pytestmark = pytest.mark.smoke


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _create_repo(path: Path) -> Path:
    path.mkdir()
    (path / "README.md").write_text("# smoke target\n", encoding="utf-8")
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "smoke@example.com")
    _git(path, "config", "user.name", "Smoke")
    _git(path, "add", "README.md")
    _git(path, "commit", "-qm", "seed")
    return path


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


def _assert_no_extra_worktrees(repo: Path, worktrees_root: Path) -> None:
    leftovers = list(worktrees_root.iterdir()) if worktrees_root.is_dir() else []
    assert leftovers == [], f"leftover skep worktrees: {leftovers}"
    listed = _git(repo, "worktree", "list", "--porcelain").stdout
    assert listed.count("worktree ") == 1, f"git still tracks extra worktrees:\n{listed}"


def test_whole_system_smoke_serves_dispatches_reverifies_and_applies_patch(
    tmp_path: Path,
) -> None:
    repo = _create_repo(tmp_path / "repo")
    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    config = build_config(tmp_path / "home", None)
    app = create_app(config, sse_poll_seconds=0.01, start_ticker=False)
    token = (config.home / TOKEN_FILE).read_text(encoding="utf-8").strip()

    with TestClient(app) as client:
        assert client.get("/api/status").status_code == 401

        headers = {"X-Skep-Token": token}
        assert client.get("/api/status", headers=headers).json()["status"] == "ok"

        accepted = client.post(
            "/api/runs",
            headers=headers,
            json={
                "repo": str(repo),
                "instructions": "Create a simple hello world in Python.",
                "execution_mode": "workspace",
            },
        )
        assert accepted.status_code == 202
        task_id = accepted.json()["task_id"]

        events, final_state = _stream_run_events(client, task_id, headers=headers)
        event_types = [event["type"] for event in events]
        assert final_state == "completed"
        assert "task.start" in event_types
        assert "plan.created" in event_types
        assert "verify.result" in event_types
        assert "task.terminal" in event_types
        assert "reverify.result" in event_types

        detail = client.get(f"/api/runs/{task_id}", headers=headers).json()
        assert detail["run"]["state"] == "completed"
        assert detail["run"]["verification_outcome"] == "passed"
        assert detail["run"]["worker_version"] == "coding-minimal-0.1.0"
        assert detail["usage"]["provider_calls"] == 0
        assert detail["reverification"]["confirmed"] is True
        assert detail["reverification"]["outcome"] == "passed"
        assert "hello.py" in detail["reverification"]["commands"][0]

        artifacts = {artifact["kind"]: artifact for artifact in detail["artifacts"]}
        assert {"event_log", "patch"} <= set(artifacts)
        patch_text = Path(artifacts["patch"]["path"]).read_text(encoding="utf-8")
        assert "hello.py" in patch_text
        assert 'print("Hello, world!")' in patch_text
        assert client.get(f"/api/runs/{task_id}/diff", headers=headers).text == patch_text

        created_review = client.post(f"/api/runs/{task_id}/approvals", headers=headers)
        assert created_review.status_code == 201
        review_id = created_review.json()["review_id"]

        approved = client.post(
            f"/api/approvals/{review_id}/approve",
            headers=headers,
            json={"actor": "whole-system-smoke"},
        )
        assert approved.status_code == 200
        assert approved.json() == {"action": "applied", "branch": f"skep/{task_id}"}

        branch = f"skep/{task_id}"
        branch_file = _git(repo, "show", f"{branch}:hello.py").stdout
        assert branch_file == 'print("Hello, world!")\n'
        branch_log = _git(repo, "log", "-1", "--format=%B", branch).stdout
        assert "Approved-by: whole-system-smoke" in branch_log
        assert not (repo / "hello.py").exists()
        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head_before

        applied = client.get(f"/api/runs/{task_id}", headers=headers).json()
        assert applied["applied_branch"] == branch
        assert [approval["status"] for approval in applied["approvals"]] == ["approved"]
        assert applied["approvals"][0]["resolved_by"] == "whole-system-smoke"

    _assert_no_extra_worktrees(repo, repo.parent / ".skep" / "worktrees")
