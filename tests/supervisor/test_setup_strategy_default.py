"""v127-F10: setup works on a new repo without an unlisted `--strategy`.

`skep project setup <new repo> --engine codex` failed "project setup
requires a pack or strategy" — a flag the quick start never names. Every
face now takes the project's stored strategy, else `trusted_local_dev` for a
new project, and says which strategy it chose and what it allows.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from skep.cli import main
from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.serve.jobs import Dispatcher
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import execute_mutation

from .conftest import serve_client


def _stored_strategy(db: Path, project_id: str = "fresh") -> str:
    store = RunStore(db)
    try:
        record = store.get_project_policy(project_id)
    finally:
        store.close()
    assert record is not None
    return record.strategy


@pytest.mark.parametrize("verb", ["setup", "preview"])
def test_the_cli_defaults_a_new_project_to_trusted_local_dev(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], verb: str
) -> None:
    home = tmp_path / "home"
    code = main(["--home", str(home), "project", verb, str(repo), "--project-id", "fresh",
                 "--engine", "codex"])  # fmt: skip
    out = capsys.readouterr()
    assert code == 0, out.err
    assert "(trusted_local_dev/build)" in out.out
    assert "trusted_local_dev" in out.out and "auto_dispatch_allowed=True" in out.out
    if verb == "setup":
        assert _stored_strategy(home / "supervisor" / "supervisor.sqlite3") == "trusted_local_dev"


@pytest.mark.parametrize("route", ["setup", "preview"])
def test_rest_defaults_a_new_project_to_trusted_local_dev(
    repo: Path, config: SupervisorConfig, route: str
) -> None:
    response = serve_client(config).post(
        f"/api/projects/{route}",
        json={"project_id": "fresh", "name": "fresh", "repo_path": str(repo)},
    )
    assert response.status_code in (200, 201), response.text
    body = response.json()
    assert (body.get("project") or body)["strategy"] == "trusted_local_dev"
    assert any("auto_dispatch_allowed=True" in note for note in body["notes"]), body["notes"]


def _chat_setup(config: SupervisorConfig, repo: Path, **args: Any) -> dict[str, Any]:
    store = RunStore(config.db_path)
    try:
        return cast(
            dict[str, Any],
            execute_mutation(
                "setup_project",
                {"project_id": "fresh", "name": "fresh", "repo_path": str(repo), **args},
                store=store,
                holder=ConfigHolder(config, store),
                runner=cast(Dispatcher, None),
                actor="chat-user",
            ),
        )
    finally:
        store.close()


def test_the_chat_tool_needs_no_strategy(repo: Path, config: SupervisorConfig) -> None:
    from skep.supervisor.serve.tools import MUTATING_TOOL_SPECS

    (tool,) = [t for t in MUTATING_TOOL_SPECS if t["function"]["name"] == "setup_project"]
    assert "strategy" not in tool["function"]["parameters"]["required"]
    assert _chat_setup(config, repo)["strategy"] == "trusted_local_dev"


def test_an_existing_public_free_project_keeps_its_strategy(
    repo: Path, config: SupervisorConfig
) -> None:
    _chat_setup(config, repo, strategy="public_free")
    assert _chat_setup(config, repo)["strategy"] == "public_free"
    response = serve_client(config).post(
        "/api/projects/setup",
        json={"project_id": "fresh", "name": "fresh", "repo_path": str(repo)},
    )
    assert response.json()["strategy"] == "public_free"
    assert _stored_strategy(config.db_path) == "public_free"


@pytest.mark.parametrize("field", ["strategy", "pack"])
def test_an_unknown_name_lists_the_valid_strategies_and_packs(
    repo: Path, config: SupervisorConfig, field: str
) -> None:
    response = serve_client(config).post(
        "/api/projects/setup",
        json={"project_id": "fresh", "name": "fresh", "repo_path": str(repo), field: "nope"},
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "trusted_local_dev" in detail and "public_free" in detail and "packs:" in detail
