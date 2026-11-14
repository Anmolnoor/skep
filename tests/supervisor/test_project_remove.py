"""ES-F235 (ADR 0050): unregistering a project has an operator face.

Doctor advised `skep repo remove`, a command that never existed; the only way
to drop a project was ``DELETE /api/projects/<id>``, which called the store
directly. Now one verb (``actions.remove_project``) answers the REST route,
`skep project remove` and the command deck's /unregister card — and it drops
skep's record of the project only, never the folder.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skep.cli import main
from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.cli_cmds import build_config

from .conftest import serve_client


def _seed(db_path: Path, repo: Path) -> None:
    store = RunStore(db_path)
    try:
        store.add_project_policy(
            project_id="gone", name="gone", strategy="trusted_local_dev", phase="build", policy={}
        )
        store.add_project_binding(
            project_id="gone", binding_kind="repo_path", binding_value=str(repo)
        )
    finally:
        store.close()


def _projects(db_path: Path) -> list[str]:
    store = RunStore(db_path)
    try:
        return [p.project_id for p in store.list_project_policies()]
    finally:
        store.close()


def test_cli_project_remove_unregisters_and_never_touches_the_folder(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "personal"
    # before any store exists the refusal is the same clean one
    assert main(["--home", str(home), "project", "remove", "gone"]) != 0
    assert "no project named 'gone'" in capsys.readouterr().err
    db_path = build_config(home, None).db_path
    db_path.parent.mkdir(parents=True)
    _seed(db_path, repo)
    before = sorted(p.name for p in repo.iterdir())

    assert main(["--home", str(home), "project", "remove", "gone"]) == 0
    assert "gone" in capsys.readouterr().out
    assert _projects(db_path) == []
    assert sorted(p.name for p in repo.iterdir()) == before  # the folder is untouched

    # an unknown project is refused cleanly, naming where to look
    assert main(["--home", str(home), "project", "remove", "gone"]) != 0
    err = capsys.readouterr().err
    assert "no project named 'gone'" in err and "skep project list" in err


def test_rest_delete_and_the_deck_card_route_through_the_one_verb(
    config: SupervisorConfig, repo: Path
) -> None:
    client = serve_client(config)
    _seed(config.db_path, repo)
    assert client.delete("/api/projects/ghost").status_code == 404

    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    proposed = client.post(
        f"/api/chats/{chat_id}/commands",
        json={"tool": "remove_project", "args": {"project_id": "gone"}},
    )
    assert proposed.status_code == 201
    assert _projects(config.db_path) == ["gone"]  # a card proposes, nothing ran
    action_id = proposed.json()["action_id"]
    confirmed = client.post(f"/api/chats/{chat_id}/commands/{action_id}/confirm").json()
    assert confirmed["ok"] is True and confirmed["result"]["removed"] is True
    assert _projects(config.db_path) == []
    assert repo.is_dir()

    _seed(config.db_path, repo)
    assert client.delete("/api/projects/gone").json()["removed"] is True
    assert _projects(config.db_path) == []
