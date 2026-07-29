"""V11 public_free acceptance smoke: pack setup, seeds, and policy evidence."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skep.supervisor.cli_cmds import build_config
from skep.supervisor.packs import builtin_policy_packs
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


def _create_public_repo(path: Path) -> Path:
    path.mkdir()
    (path / "README.md").write_text("# public free smoke\n", encoding="utf-8")
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "smoke@example.com")
    _git(path, "config", "user.name", "Smoke")
    _git(path, "add", "README.md")
    _git(path, "commit", "-qm", "seed public repo")
    return path


def test_public_free_setup_seeds_and_dispatch_evidence(tmp_path: Path) -> None:
    repo = _create_public_repo(tmp_path / "public-free-repo")
    pack = builtin_policy_packs()["public_free"]
    assert pack.provider_defaults.get("required_paid_provider") is False

    config = build_config(tmp_path / "home", None)
    app = create_app(config, sse_poll_seconds=0.01, start_ticker=False)
    token = (config.home / TOKEN_FILE).read_text(encoding="utf-8").strip()
    headers = {"X-Skep-Token": token}

    with TestClient(app) as client:
        preview = client.post(
            "/api/projects/preview",
            headers=headers,
            json={
                "project_id": "public-free-smoke",
                "name": "Public Free Smoke",
                "pack": "public_free",
                "phase": "build",
                "repo_path": str(repo),
            },
        )
        assert preview.status_code == 200
        preview_body = preview.json()
        assert preview_body["pack"] == {
            "name": "public_free",
            "version": "1",
            "status": "supported",
        }
        assert preview_body["effective_policy"]["default_network"] == []
        assert preview_body["sample_dispatch_decision"]["reason"] == (
            "dispatch.auto_allowed.project_policy_match"
        )

        setup = client.post(
            "/api/projects/setup",
            headers=headers,
            json={
                "project_id": "public-free-smoke",
                "name": "Public Free Smoke",
                "pack": "public_free",
                "phase": "build",
                "repo_path": str(repo),
            },
        )
        assert setup.status_code == 201
        setup_body = setup.json()
        assert setup_body["pack_name"] == "public_free"
        assert setup_body["pack_version"] == "1"
        assert len(setup_body["seeded_templates"]) == 4
        assert len(setup_body["seeded_schedules"]) == 3

        packs = client.get("/api/projects/packs", headers=headers).json()["packs"]
        public_free = next(pack for pack in packs if pack["name"] == "public_free")
        assert public_free["status"] == "supported"

        run = client.post(
            "/api/runs",
            headers=headers,
            json={
                "repo": str(repo),
                "instructions": "Create a simple hello world in Python.",
            },
        )
        assert run.status_code == 202
        task_id = run.json()["task_id"]

        with client.stream(
            "GET", f"/api/runs/{task_id}/events?stream=1", headers=headers
        ) as stream:
            final_state = None
            for line in stream.iter_lines():
                if line.startswith("data: "):
                    payload = line.removeprefix("data: ")
                    if '"state":' in payload:
                        final_state = payload
            assert final_state is not None

        detail = client.get(f"/api/runs/{task_id}", headers=headers).json()
        assert detail["run"]["state"] == "completed"
        assert detail["project_context"]["project_id"] == "public-free-smoke"
        assert detail["project_context"]["strategy"] == "public_free"
        assert detail["project_context"]["phase"] == "build"
        assert detail["dispatch_decision"]["reason"] == (
            "dispatch.auto_allowed.project_policy_match"
        )
