"""v123-F2 (DD-8, ADR 0050): a schedule's role is a field of the existing
create verb — `skep schedule add --role`, REST `role` on POST /api/schedules,
the chat's own role on propose_schedule. The parity test follows chat arms
only, so this pins the CLI and REST faces directly; all three run one check
(``actions.check_schedule_role``)."""

from __future__ import annotations

from pathlib import Path

import pytest

from skep.cli import main
from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.serve.actions import SCHEDULE_ROLE_NOTE

from .conftest import serve_client
from .test_role_policy import _home


@pytest.fixture
def config(tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> SupervisorConfig:
    return _home(tmp_path, repo, capsys)


def _stored_role(config: SupervisorConfig, name: str) -> str | None:
    store = RunStore(config.db_path)
    try:
        schedule = store.get_schedule(name)
    finally:
        store.close()
    assert schedule is not None
    return schedule.role


def test_the_cli_sets_the_role_and_lists_it(
    config: SupervisorConfig, tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = ["--home", str(tmp_path)]
    assert main([*home, "schedule", "add", "nightly", str(repo), "tidy", "--every", "1d",
                 "--role", "builder"]) == 0  # fmt: skip
    assert _stored_role(config, "nightly") == "builder"
    capsys.readouterr()
    assert main([*home, "schedule", "list"]) == 0
    listed = capsys.readouterr().out
    assert "role builder" in listed and SCHEDULE_ROLE_NOTE in listed


@pytest.mark.parametrize(
    ("extra", "refusal"),
    [
        (["--role", "assistant", "--caste", "researcher"],
         "role assistant cannot run a researcher: it denies workspace writes"),
        (["--role", "nobody"], "no role 'nobody'"),
        (["--role", "builder", "--caste", "note"], "a note schedule takes no role"),
    ],
)  # fmt: skip
def test_the_cli_refuses_a_role_that_cannot_hold(
    config: SupervisorConfig,
    tmp_path: Path,
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    extra: list[str],
    refusal: str,
) -> None:
    code = main(["--home", str(tmp_path), "schedule", "add", "s", str(repo), "x",
                 "--every", "1d", *extra])  # fmt: skip
    assert code != 0
    assert refusal in capsys.readouterr().err
    store = RunStore(config.db_path)
    try:
        assert store.get_schedule("s") is None
    finally:
        store.close()


def test_rest_sets_the_role_and_the_view_says_what_it_narrows(
    config: SupervisorConfig, repo: Path
) -> None:
    client = serve_client(config)
    made = client.post(
        "/api/schedules",
        json={"name": "nightly", "repo": str(repo), "every": "1d", "instructions": "x",
              "role": "builder"},
    )  # fmt: skip
    assert made.status_code == 201, made.text
    assert (made.json()["role"], made.json()["role_note"]) == ("builder", SCHEDULE_ROLE_NOTE)
    (listed,) = client.get("/api/schedules").json()["schedules"]
    assert listed["role"] == "builder"
    assert _stored_role(config, "nightly") == "builder"


@pytest.mark.parametrize(
    ("body", "refusal"),
    [
        ({"caste": "researcher", "role": "assistant"}, "role assistant cannot run a researcher"),
        ({"role": "nobody"}, "no role 'nobody'"),
        ({"caste": "script", "role": "builder"}, "a script schedule takes no role"),
    ],
)
def test_rest_refuses_a_role_that_cannot_hold(
    config: SupervisorConfig, repo: Path, body: dict[str, str], refusal: str
) -> None:
    client = serve_client(config)
    refused = client.post(
        "/api/schedules",
        json={"name": "s", "repo": str(repo), "every": "1d", "instructions": "x", **body},
    )
    assert refused.status_code == 400
    assert refusal in refused.json()["detail"]
    assert client.get("/api/schedules").json()["schedules"] == []


def test_a_template_schedule_takes_a_role_on_both_faces(
    config: SupervisorConfig, tmp_path: Path, repo: Path
) -> None:
    client = serve_client(config)
    saved = client.post(
        "/api/templates",
        json={"name": "tidy", "worker_kind": "coding", "instructions": "tidy {area}",
              "params": [{"name": "area"}]},
    )  # fmt: skip
    assert saved.status_code in (200, 201), saved.text
    made = client.post(
        "/api/schedules",
        json={"name": "t", "repo": str(repo), "every": "1d", "template": "tidy",
              "params": {"area": "docs"}, "role": "operator"},
    )  # fmt: skip
    assert made.status_code == 201, made.text
    assert _stored_role(config, "t") == "operator"
    add = ["schedule", "add", "t2", str(repo), "--every", "1d", "--template", "tidy"]
    assert main(["--home", str(tmp_path), *add, "--param", "area=src", "--role", "operator"]) == 0
    assert _stored_role(config, "t2") == "operator"
