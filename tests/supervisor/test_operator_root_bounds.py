# ruff: noqa: F811  (the `home` fixture is imported)
"""W15: binding a repo path makes it an operator root — the Queen reads all
of it. ``project setup`` used to bind ``~``, ``/`` or ``/home`` with no
check (only ``/workon`` refused ``~`` and ``/``). Every face that binds a
repo path now refuses the same too-broad paths (``fileio.too_broad_root``),
and the write-time check sees the would-be root, so a root covering a
secret-floor path is refused as §5.4 check 1."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from skep.cli import main
from skep.supervisor.serve import actions
from skep.supervisor.serve.registry import setup_project_record
from skep.supervisor.serve.tools import execute_mutation

from .test_policy_write_v3 import Home, home  # noqa: F401  (fixture)

BROAD: dict[str, Callable[[Home], Path]] = {
    "the home directory": lambda h: Path.home(),
    "/": lambda h: Path("/"),
    "/home": lambda h: Path("/home"),
    "an ancestor of home": lambda h: Path.home().parent,
    "the skep store": lambda h: h.config.home.parent,
}


def _setup(h: Home, path: Path) -> dict[str, Any]:
    return setup_project_record(
        run_store=h.store, root=h.root / "repos", project_id="q", name="q",
        strategy="trusted_local_dev", phase="build", repo_path=str(path),
        seed_default_schedules=False, seed_shell_commands=False,
    )  # fmt: skip


@pytest.mark.parametrize("kind", sorted(BROAD))
def test_setup_refuses_a_too_broad_root(home: Home, kind: str) -> None:
    Path.home().mkdir(parents=True, exist_ok=True)
    home.config.home.mkdir(parents=True, exist_ok=True)
    path = BROAD[kind](home)
    if not path.is_dir():
        pytest.skip(f"{path} does not exist on this machine")
    with pytest.raises(HTTPException) as refused:
        _setup(home, path)
    assert refused.value.status_code == 400
    assert "too broad" in str(refused.value.detail)
    assert home.store.get_project_policy("q") is None


def test_setup_refuses_a_root_over_a_secret_floor_path(home: Home) -> None:
    secrets = Path.home() / ".ssh"
    secrets.mkdir(parents=True)
    with pytest.raises(HTTPException) as refused:
        _setup(home, secrets)
    assert "check 1" in str(refused.value.detail) and ".ssh" in str(refused.value.detail)
    assert home.store.get_project_policy("q") is None


def test_a_project_subdirectory_is_still_bound(home: Home) -> None:
    project = Path.home() / "code" / "app"
    project.mkdir(parents=True)
    assert _setup(home, project)["project_id"] == "q"


def test_every_face_refuses(home: Home, capsys: pytest.CaptureFixture[str]) -> None:
    user = Path.home()
    user.mkdir(parents=True, exist_ok=True)
    # CLI
    code = main(["--home", str(home.root), "project", "setup", str(user), "--project-id", "q",
                 "--strategy", "trusted_local_dev"])  # fmt: skip
    assert code != 0 and "too broad" in capsys.readouterr().err
    # chat setup_project
    with pytest.raises(HTTPException, match="too broad"):
        execute_mutation(
            "setup_project",
            {"project_id": "q", "name": "q", "strategy": "trusted_local_dev",
             "repo_path": str(user)},
            store=home.store, holder=home.holder, runner=None,  # type: ignore[arg-type]
            actor="chat-user", chat_id=None,
        )  # fmt: skip
    # /workon: the ancestor of home, which it used to accept
    with pytest.raises(HTTPException, match="too broad"):
        actions.workon(home.holder, home.store, path=str(user.parent))
    assert home.store.get_project_policy("q") is None


def test_rest_refuses(config: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from .conftest import serve_client

    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    (tmp_path / "user").mkdir()
    response = serve_client(config).post(
        "/api/projects",
        json={"project_id": "q", "name": "q", "strategy": "trusted_local_dev", "phase": "build",
              "bindings": [{"kind": "repo_path", "value": str(tmp_path / "user")}]},
    )  # fmt: skip
    assert response.status_code == 400 and "too broad" in response.text


def test_rest_project_write_sees_the_would_be_root(
    config: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /api/projects binds repo paths too: it runs the same write check
    (check 1 on the would-be root) as project setup."""
    from .conftest import serve_client

    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    secrets = tmp_path / "user" / ".ssh"
    secrets.mkdir(parents=True)
    response = serve_client(config).post(
        "/api/projects",
        json={"project_id": "q", "name": "q", "strategy": "trusted_local_dev", "phase": "build",
              "bindings": [{"kind": "repo_path", "value": str(secrets)}]},
    )  # fmt: skip
    assert response.status_code == 400 and "check 1" in response.text, response.text
