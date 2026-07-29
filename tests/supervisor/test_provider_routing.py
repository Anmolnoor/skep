"""v39-F4: pack-aware provider routing is consulted at dispatch (closes v14-5).

The routing engine (v14 Step 5) existed with zero callers. Now run_task
resolves it for every project-bound run: the decision is recorded on the
'dispatched' transition, and an ollama-protocol profile exports
SKEP_OLLAMA_URL/SKEP_OLLAMA_MODEL into the worker env (the vars the
first-party ollama worker already reads). Routing never widens egress: a
non-local provider is used only when its host is already in the run's
network grant.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.providers import ProviderHealth, ProviderProfile

from .conftest import serve_client, wait_terminal


def _seed_project(config: SupervisorConfig, repo: Path) -> None:
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="routed-project",
            name="routed project",
            strategy="trusted_local_dev",
            phase="build",
            policy={"default_execution_mode": "workspace"},
        )
        store.add_project_binding(
            project_id="routed-project",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
    finally:
        store.close()


def _seed_provider(
    config: SupervisorConfig,
    *,
    provider_id: str,
    cost_class: str,
    base_url: str,
    healthy: bool = True,
) -> None:
    store = RunStore(config.db_path)
    try:
        store.upsert_provider_profile(
            ProviderProfile(
                provider_id=provider_id,
                protocol="ollama",
                base_url=base_url,
                model="qwen3:8b",
                cost_class=cost_class,
            )
        )
        store.record_provider_health(
            ProviderHealth(
                provider_id=provider_id,
                reachable=healthy,
                model_found=healthy,
                latency_ms=5,
                error=None,
                checked_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        )
    finally:
        store.close()


def _dispatch(client: Any, repo: Path, instructions: str) -> str:
    response = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": instructions,
            "execution_mode": "workspace",
        },
    )
    assert response.status_code == 202
    return str(response.json()["task_id"])


def _routing_detail(client: Any, task_id: str) -> dict[str, Any] | None:
    detail = client.get(f"/api/runs/{task_id}").json()
    for transition in detail["transitions"]:
        raw = transition["detail"]
        if isinstance(raw, dict) and "provider_routing" in raw:
            return dict(raw["provider_routing"])
    return None


def test_local_profile_routes_and_exports_worker_env(repo: Path, config: SupervisorConfig) -> None:
    _seed_project(config, repo)
    _seed_provider(
        config, provider_id="local-ollama", cost_class="local", base_url="http://127.0.0.1:11434"
    )
    client = serve_client(config)
    task_id = _dispatch(client, repo, "Fix the bug. MODE:envdump")
    assert wait_terminal(client, task_id)["state"] == "completed"

    routing = _routing_detail(client, task_id)
    assert routing == {
        "provider_id": "local-ollama",
        "reason": "routing.preferred_local_healthy",
    }
    dump = json.loads((config.results_dir / f"envdump-{task_id}.json").read_text(encoding="utf-8"))
    assert dump["SKEP_OLLAMA_URL"] == "http://127.0.0.1:11434"
    assert dump["SKEP_OLLAMA_MODEL"] == "qwen3:8b"


def test_remote_profile_without_network_grant_is_blocked_not_silently_used(
    repo: Path, config: SupervisorConfig
) -> None:
    _seed_project(config, repo)
    _seed_provider(
        config, provider_id="paid-remote", cost_class="paid", base_url="https://llm.example.com"
    )
    client = serve_client(config)
    task_id = _dispatch(client, repo, "Fix the bug. MODE:envdump")
    assert wait_terminal(client, task_id)["state"] == "completed"

    routing = _routing_detail(client, task_id)
    assert routing == {
        "provider_id": None,
        "reason": "routing.remote_blocked_by_policy",
    }
    dump = json.loads((config.results_dir / f"envdump-{task_id}.json").read_text(encoding="utf-8"))
    assert "SKEP_OLLAMA_URL" not in dump
    assert "SKEP_OLLAMA_MODEL" not in dump


def test_unbound_run_records_no_routing(repo: Path, config: SupervisorConfig) -> None:
    """No project, no strategy — routing is a project concern (v14's pack hook)."""
    _seed_provider(
        config, provider_id="local-ollama", cost_class="local", base_url="http://127.0.0.1:11434"
    )
    client = serve_client(config)
    task_id = _dispatch(client, repo, "Fix the bug. MODE:happy")
    assert wait_terminal(client, task_id)["state"] == "completed"
    assert _routing_detail(client, task_id) is None
