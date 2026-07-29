"""v70-F3 (ADR 0040): the worker_protocol policy knob reaches dispatch.

The react protocol shipped in v69 with contract + run_task plumbing only —
the field test had to drive run_task directly. This closes the loop: a
project policy overlay (`worker_protocol: react`) resolves through
resolve_run_policy, rides submit_run/Dispatcher into the task envelope, and
the resolver is the validation point (overlays are free-form merges).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.policy_resolver import (
    PolicyResolutionError,
    ResolvedRunPolicy,
    resolve_run_policy,
)

from .conftest import serve_client, wait_terminal


def _resolve(store: RunStore, config: object, repo: Path) -> ResolvedRunPolicy:
    return resolve_run_policy(
        store=store,
        config=config,  # type: ignore[arg-type]
        repo=repo,
        caste="coding",
        network=None,
        env_allowlist=None,
        wall_clock_seconds=None,
        max_iterations=None,
        max_actions=None,
        max_provider_calls=None,
        execution_mode="sandbox",
    )


def _bind_project(store: RunStore, repo: Path, policy: dict[str, object]) -> None:
    store.add_project_policy(
        project_id="proto-project",
        name="protocol project",
        strategy="trusted_local_dev",
        phase="build",
        policy=policy,
    )
    store.add_project_binding(
        project_id="proto-project", binding_kind="repo_path", binding_value=str(repo)
    )


def test_worker_protocol_resolves_from_the_project_overlay(tmp_path: Path, repo: Path) -> None:
    config = build_config(tmp_path / "home", None)
    store = RunStore(config.db_path)
    try:
        assert _resolve(store, config, repo).worker_protocol == "plan"  # default
        _bind_project(store, repo, {"worker_protocol": "react"})
        assert _resolve(store, config, repo).worker_protocol == "react"
    finally:
        store.close()


def test_worker_protocol_garbage_fails_closed_with_the_teach(tmp_path: Path, repo: Path) -> None:
    config = build_config(tmp_path / "home", None)
    store = RunStore(config.db_path)
    try:
        _bind_project(store, repo, {"worker_protocol": "vibes"})
        with pytest.raises(PolicyResolutionError, match=r"'plan' or 'react'"):
            _resolve(store, config, repo)
    finally:
        store.close()


def test_react_knob_rides_the_serve_dispatch_into_the_envelope(
    repo: Path, config: SupervisorConfig
) -> None:
    """POST /api/runs on a react-knob project mints a react task envelope —
    asserted on the audited task.json (the fake worker ignores the field)."""
    seeded = RunStore(config.db_path)
    try:
        _bind_project(
            seeded,
            repo,
            {"default_execution_mode": "workspace", "worker_protocol": "react"},
        )
    finally:
        seeded.close()

    client = serve_client(config)
    response = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "workspace",
        },
    )
    task_id = str(response.json()["task_id"])
    wait_terminal(client, task_id)

    envelope = json.loads((config.audit_dir / task_id / "task.json").read_text(encoding="utf-8"))
    assert envelope["planning_protocol"] == "react"
    assert envelope["contract_version"] == "0.3.5"


def test_per_run_protocol_overrides_the_policy_default(
    repo: Path, config: SupervisorConfig
) -> None:
    """v87-F5: a fetch-then-synthesize task can request react per run — a
    plan-mode worker writes its whole plan before the fetched data exists
    and can only fabricate the deliverable."""
    client = serve_client(config)
    response = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "Fetch then summarize. MODE:happy",
            "execution_mode": "workspace",
            "protocol": "react",
        },
    )
    task_id = str(response.json()["task_id"])
    wait_terminal(client, task_id)

    envelope = json.loads((config.audit_dir / task_id / "task.json").read_text(encoding="utf-8"))
    assert envelope["planning_protocol"] == "react"


def test_garbage_per_run_protocol_is_rejected(repo: Path, config: SupervisorConfig) -> None:
    client = serve_client(config)
    response = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "x. MODE:happy",
            "execution_mode": "workspace",
            "protocol": "yolo",
        },
    )
    assert response.status_code == 422  # pydantic pins the enum at the edge
