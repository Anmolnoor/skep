"""First-run setup status and completion flow."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.serve.setup import SETUP_COMPLETED_AT
from skep.supervisor.store import RunStore

from .conftest import serve_client as _client


def test_setup_status_reports_fresh_home_as_incomplete(config: SupervisorConfig) -> None:
    client = _client(config)

    status = client.get("/api/setup/status").json()

    assert status["complete"] is False
    assert status["marked_complete"] is False
    assert status["missing"] == [
        "llm",
        "default_model",
        "workspace_project",
        "policy",
    ]
    assert status["llm"]["ready"] is False
    assert status["default_model"]["ready"] is False
    assert status["workspace_project"]["ready"] is False
    assert status["policy"]["ready"] is False


def test_setup_completion_is_recomputed_even_with_stale_marker(
    repo: Path, config: SupervisorConfig
) -> None:
    store = RunStore(config.db_path)
    try:
        store.set_setting(SETUP_COMPLETED_AT, "2026-06-30T00:00:00Z")
    finally:
        store.close()

    client = _client(config)
    stale = client.get("/api/setup/status").json()
    assert stale["marked_complete"] is True
    assert stale["complete"] is False

    blocked = client.post("/api/setup/complete")
    assert blocked.status_code == 409
    assert "llm" in blocked.json()["detail"]

    client.put(
        "/api/llm/config",
        json={"base_url": "http://localhost:11434", "default_model": "llama3.2"},
    )
    saved = client.post(
        "/api/projects/setup",
        json={
            "project_id": "first-project",
            "name": "First Project",
            "pack": "public_free",
            "phase": "build",
            "repo_path": str(repo),
            "seed_default_schedules": False,
        },
    )
    assert saved.status_code == 201

    status = client.get("/api/setup/status").json()
    assert status["complete"] is True
    assert status["missing"] == []
    assert status["policy"]["project_execution_modes"] == ["workspace"]

    completed = client.post("/api/setup/complete").json()
    assert completed["complete"] is True
    assert completed["marked_complete"] is True
    assert completed["completed_at"]


def test_default_workspace_setup_is_opt_in(
    tmp_path: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home-dir"))
    client = _client(config)
    workspace = tmp_path / "home-dir" / "workspace"

    skipped = client.post("/api/setup/default-workspace", json={"apply": False}).json()
    assert skipped["applied"] is False
    assert skipped["workspace"] == str(workspace)
    assert not workspace.exists()

    applied = client.post("/api/setup/default-workspace", json={"apply": True}).json()
    assert applied["applied"] is True
    assert applied["workspace"] == str(workspace)
    assert (workspace / ".git").is_dir()
    assert applied["project"]["project_id"] == "workspace"
    assert applied["project"]["name"] == "Workspace"
    assert applied["project"]["pack_name"] == "trusted_local_dev"
    assert applied["project"]["phase"] == "build"
    assert applied["project"]["policy"]["default_execution_mode"] == "workspace"

    status = client.get("/api/setup/status").json()
    assert status["workspace_project"]["ready"] is True
    assert status["policy"]["ready"] is True
    assert status["default_workspace"]["path"] == str(workspace)

    shutil.rmtree(workspace)
    stale = client.get("/api/setup/status").json()
    assert stale["workspace_project"]["ready"] is False
    assert "workspace_project" in stale["missing"]
