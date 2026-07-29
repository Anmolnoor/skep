from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from skep.supervisor.projects import ProjectBinding, validate_project_definition
from skep.supervisor.store import RunStore

V9_GOLDEN_FIXTURE = Path(__file__).parents[1] / "fixtures" / "project_policy_v9_golden.json"


def _load_v9_golden_policy(repo_path: Path | None = None) -> dict[str, Any]:
    payload = cast(dict[str, Any], json.loads(V9_GOLDEN_FIXTURE.read_text(encoding="utf-8")))
    if repo_path is not None:
        payload["bindings"] = [{"kind": "repo_path", "value": str(repo_path)}]
    return payload


def test_project_policy_roundtrip_and_binding_lookup(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        store.add_project_policy(
            project_id="project-1",
            name="demo project",
            strategy="trusted_local_dev",
            phase="build",
            policy={
                "default_execution_mode": "workspace",
                "default_network": ["example.com"],
                "allowed_shell_commands": [["pytest"]],
                "default_wall_clock_seconds": 321,
                "default_max_iterations": 7,
                "default_max_actions": 11,
                "default_max_provider_calls": 13,
            },
        )
        store.add_project_binding(
            project_id="project-1",
            binding_kind="repo_path",
            binding_value=str(tmp_path / "repo"),
        )

        policy = store.get_project_policy("project-1")
        assert policy is not None
        assert policy.name == "demo project"
        assert policy.strategy == "trusted_local_dev"
        assert policy.phase == "build"
        assert policy.policy["default_execution_mode"] == "workspace"
        assert policy.policy["default_network"] == ["example.com"]

        matched = store.project_for_binding("repo_path", str(tmp_path / "repo"))
        assert matched is not None
        assert matched.project_id == "project-1"

        bindings = store.project_bindings("project-1")
        assert len(bindings) == 1
        assert bindings[0].binding_kind == "repo_path"
        assert bindings[0].binding_value == str(tmp_path / "repo")
    finally:
        store.close()


def test_v9_golden_project_policy_roundtrip_preserves_every_field(tmp_path: Path) -> None:
    payload = _load_v9_golden_policy(tmp_path / "repo")
    definition = validate_project_definition(
        project_id=str(payload["project_id"]),
        name=str(payload["name"]),
        strategy=str(payload["strategy"]),
        phase=str(payload["phase"]),
        policy=dict(cast(dict[str, Any], payload["policy"])),
        bindings=[
            ProjectBinding(kind=str(binding["kind"]), value=str(binding["value"]))
            for binding in cast(list[dict[str, Any]], payload["bindings"])
        ],
    )
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        store.add_project_policy(
            project_id=definition.project_id,
            name=definition.name,
            strategy=definition.strategy,
            phase=definition.phase,
            policy=definition.policy,
        )
        for binding in definition.bindings:
            store.add_project_binding(
                project_id=definition.project_id,
                binding_kind=binding.kind,
                binding_value=binding.value,
            )

        stored = store.get_project_policy(definition.project_id)
        assert stored is not None
        assert stored.name == definition.name
        assert stored.strategy == definition.strategy
        assert stored.phase == definition.phase
        assert stored.policy == definition.policy
        assert [
            {"kind": binding.binding_kind, "value": binding.binding_value}
            for binding in store.project_bindings(definition.project_id)
        ] == [{"kind": binding.kind, "value": binding.value} for binding in definition.bindings]
    finally:
        store.close()


def test_project_definition_rejects_unknown_policy_keys() -> None:
    with pytest.raises(ValueError, match="unknown project policy fields"):
        validate_project_definition(
            project_id="project-1",
            name="demo project",
            strategy="trusted_local_dev",
            phase="build",
            policy={"wizard_mode": True},
            bindings=[ProjectBinding(kind="repo_path", value="/tmp/repo")],
        )


@pytest.mark.parametrize(
    ("policy", "match"),
    [
        ({"default_execution_mode": "daemon"}, "default_execution_mode must be one of"),
        ({"default_network": "example.com"}, "default_network must be a list of strings"),
        (
            {"allowed_plugin_risks": ["wizard_mode"]},
            "allowed_plugin_risks must only contain",
        ),
        (
            {"allowed_shell_commands": [["bash", "-lc"]]},
            "shell command prefix \\['bash', '-lc'\\] is too broad",
        ),
        ({"auto_dispatch_allowed": "yes"}, "auto_dispatch_allowed must be a boolean"),
        (
            {"default_wall_clock_seconds": 0},
            "default_wall_clock_seconds must be an integer >= 1",
        ),
        (
            {"default_max_provider_calls": -1},
            "default_max_provider_calls must be an integer non-negative",
        ),
    ],
)
def test_project_definition_rejects_invalid_policy_values(
    policy: dict[str, object], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        validate_project_definition(
            project_id="project-1",
            name="demo project",
            strategy="trusted_local_dev",
            phase="build",
            policy=policy,
            bindings=[ProjectBinding(kind="repo_path", value="/tmp/repo")],
        )


def test_store_rejects_invalid_project_policy_on_write(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        with pytest.raises(ValueError, match="default_execution_mode must be one of"):
            store.add_project_policy(
                project_id="project-1",
                name="demo project",
                strategy="trusted_local_dev",
                phase="build",
                policy={"default_execution_mode": "daemon"},
            )
    finally:
        store.close()


def test_store_rejects_invalid_project_binding_on_write(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        store.add_project_policy(
            project_id="project-1",
            name="demo project",
            strategy="trusted_local_dev",
            phase="build",
            policy={},
        )
        with pytest.raises(ValueError, match="binding kind must be one of"):
            store.add_project_binding(
                project_id="project-1",
                binding_kind="template_slug",
                binding_value="demo",
            )
    finally:
        store.close()
