"""v55-F4 (ADR 0036): copy one project's policy overlay onto another.

"Set this project up like that one" is one carded verb — the target keeps
its own identity (name/strategy/phase/pack) and its repo bindings; only the
PROJECT_POLICY_KEYS overlay moves.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from skep.supervisor.serve.actions import copy_project_policy
from skep.supervisor.store import RunStore


def _store_with_two_projects(tmp_path: Path) -> RunStore:
    store = RunStore(tmp_path / "s.sqlite3")
    store.add_project_policy(
        project_id="alpha",
        name="alpha project",
        strategy="trusted_local_dev",
        phase="maintain",
        policy={
            "default_execution_mode": "workspace",
            "default_network": ["example.com"],
            "allowed_shell_commands": [["pytest"], ["uv", "run", "pytest"]],
            "auto_dispatch_allowed": True,
        },
        pack_name="trusted_local_dev",
        pack_version="1",
    )
    store.add_project_policy(
        project_id="beta",
        name="beta project",
        strategy="public_free",
        phase="build",
        policy={"default_network": []},
    )
    store.add_project_binding(
        project_id="beta", binding_kind="repo_slug", binding_value="beta-repo"
    )
    return store


def test_copy_moves_the_overlay_and_preserves_dst_identity(tmp_path: Path) -> None:
    store = _store_with_two_projects(tmp_path)
    try:
        result = copy_project_policy(store, src="alpha", dst="beta")
        project = result["project"]
        # The overlay is alpha's...
        assert project["policy"]["default_network"] == ["example.com"]
        assert project["policy"]["allowed_shell_commands"] == [
            ["pytest"],
            ["uv", "run", "pytest"],
        ]
        assert result["copied_keys"] == sorted(
            [
                "default_execution_mode",
                "default_network",
                "allowed_shell_commands",
                "auto_dispatch_allowed",
            ]
        )
        # ...while beta stays beta: identity, phase, pack, and bindings intact.
        assert project["project_id"] == "beta"
        assert project["name"] == "beta project"
        assert project["strategy"] == "public_free"
        assert project["phase"] == "build"
        assert project["bindings"] == [{"kind": "repo_slug", "value": "beta-repo"}]
    finally:
        store.close()


def test_copy_unknown_projects_404(tmp_path: Path) -> None:
    store = _store_with_two_projects(tmp_path)
    try:
        with pytest.raises(HTTPException) as missing_src:
            copy_project_policy(store, src="nope", dst="beta")
        assert missing_src.value.status_code == 404
        with pytest.raises(HTTPException) as missing_dst:
            copy_project_policy(store, src="alpha", dst="nope")
        assert missing_dst.value.status_code == 404
    finally:
        store.close()


def test_copy_project_policy_is_a_carded_chat_tool() -> None:
    from skep.supervisor.serve.tools import MUTATING_TOOL_NAMES, tool_description

    assert "copy_project_policy" in MUTATING_TOOL_NAMES
    description = tool_description("copy_project_policy")
    assert "bindings" in description
