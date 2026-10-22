"""P0-2 (ADR 0052 draft D2): today's policy answers, frozen — the P1 parity gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skep.supervisor.policy_golden import (
    HOME_PLACEHOLDER,
    SEED_VERSION,
    freeze,
    read_golden,
    snapshot,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "policy_golden"


def test_the_committed_golden_files_match_a_rebuild_from_the_seed(tmp_path: Path) -> None:
    """A resolver change that moves any effective value fails here with the
    diff. The operator refreshes seed + golden by script when they change
    policy on purpose; that diff in the PR is the review (I10)."""
    seed_path = FIXTURES / "seed.json"
    if not seed_path.is_file():
        pytest.skip("no seed committed yet — run scripts/policy_golden.py snapshot")
    seed = json.loads(seed_path.read_text())
    rebuilt = freeze(seed, tmp_path / "home")
    committed = read_golden(FIXTURES)
    assert set(rebuilt) == set(committed), "a golden file was added or removed; re-freeze"
    for name in sorted(rebuilt):
        assert rebuilt[name] == committed[name], (
            f"{name}.json drifted — if the change is intended, run "
            "`uv run python scripts/policy_golden.py freeze` and review the diff"
        )


def test_a_seeded_home_freezes_with_its_provenance_and_no_host_paths(tmp_path: Path) -> None:
    """The harness itself: one project with a group and an overlay freezes to
    a golden that names the layers, and no temp-home path leaks into it."""
    seed = {
        "version": SEED_VERSION,
        "settings": {"default_network": ["pypi.org"], "default_execution_mode": "workspace"},
        "policy_document": None,
        "operator_policy_document": None,
        "policy_groups": {"npm-api": {"default_network": ["registry.npmjs.org"]}},
        "projects": [
            {
                "project_id": "app",
                "name": "app",
                "strategy": "trusted_local_dev",
                "phase": "maintain",
                "policy": {
                    "policy_groups": ["npm-api"],
                    "verify_command": "pytest -q",
                    "default_max_actions": 7,
                },
                "pack_name": None,
                "pack_version": None,
                "bindings": [{"kind": "repo_path", "value": f"{HOME_PLACEHOLDER}/bound/app"}],
            }
        ],
        "templates": [],
    }
    golden = freeze(seed, tmp_path / "home")
    project = golden["project-app"]
    assert project["repo"] == f"{HOME_PLACEHOLDER}/bound/app"
    provenance = project["run_policy"]["_provenance"]
    assert provenance["default_max_actions"] == "project"
    assert provenance["default_network"] == "group:npm-api"
    assert provenance["auto_apply_branch"] == "phase:maintain"
    assert project["resolved"]["coding_engine"] == "builtin"
    assert project["resolved"]["verify_command"] == "pytest -q"
    assert "registry.npmjs.org" in project["resolved"]["network_resolved"]
    assert "shell" in project["scopes"] and "network" in project["scopes"]
    assert golden["operator"]["scopes"]["network"][0]["rule_id"] == "net:search"
    assert str(tmp_path) not in json.dumps(golden), "a host path leaked into the golden"


def test_snapshot_never_carries_secrets_or_host_paths(tmp_path: Path) -> None:
    """The seed is policy only: no llm_* setting, no provider row, no token;
    bindings and trusted roots ride under the placeholder."""
    from skep.supervisor import RunStore, SupervisorConfig

    home = tmp_path / "home"
    config = SupervisorConfig(home=home / "supervisor", worker_command=("x",))
    config.home.mkdir(parents=True)
    store = RunStore(config.db_path)
    try:
        store.set_setting("llm_base_url", "https://ollama.com")
        store.set_setting("llm_default_model", "glm-5.3")
        store.set_setting("default_network", ["pypi.org"])
        store.set_setting("trusted_workspace_roots", [str(tmp_path / "work")])
        store.add_project_policy(
            project_id="p", name="p", strategy="trusted_local_dev", phase="build", policy={}
        )
        store.add_project_binding(
            project_id="p", binding_kind="repo_path", binding_value=str(tmp_path / "work" / "p")
        )
    finally:
        store.close()
    seed = snapshot(home)
    text = json.dumps(seed)
    assert "llm_" not in text and "ollama" not in text
    assert str(tmp_path) not in text
    assert seed["settings"]["trusted_workspace_roots"] == [f"{HOME_PLACEHOLDER}/bound/work"]
    assert seed["projects"][0]["bindings"] == [
        {"kind": "repo_path", "value": f"{HOME_PLACEHOLDER}/bound/p"}
    ]
