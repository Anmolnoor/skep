"""D9 (operator decision 2026-11-13): a role or team run the operator starts
on a home with no approved baseline asks first. There every network grant is
an ask nobody answers, so a sandboxed `uv sync` met a 403 pointing at a
dispatch card that did not exist. `skep run --role` / `skep team` now print
the hosts and ask y/n on a terminal (yes answers them for that run only, the
dispatch card's own pre-answer), refuse off a terminal unless `--answer-host`
lists them; `POST /api/runs` with a role and `POST /api/teams` refuse with a
400 listing them unless `answer_hosts` names a subset of what the run asks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from skep.cli import main
from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.netproxy import admit_target
from skep.supervisor.policy_resolver import TRUSTED_DEV_REGISTRY_HOSTS

from .conftest import serve_client
from .test_deferred_asks_v7 import _zero_state

PROJECT_HOSTS = ["files.pythonhosted.org", "pypi.org"]
# what a trusted_local_dev role run asks about here: the project's hosts and
# the trusted-dev registries (v23-F5), every one an ask with no baseline
REGISTRY = sorted({*PROJECT_HOSTS, *TRUSTED_DEV_REGISTRY_HOSTS})


@pytest.fixture()
def config(tmp_path: Path, repo: Path) -> SupervisorConfig:
    config = _zero_state(tmp_path, repo)
    store = RunStore(config.db_path)
    try:
        record = store.get_project_policy("app")
        assert record is not None
        store.add_project_policy(
            project_id="app",
            name="app",
            strategy="trusted_local_dev",
            phase="build",
            policy={"default_network": PROJECT_HOSTS},
        )
    finally:
        store.close()
    return config


@pytest.fixture()
def captured(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """What reached the runner, without running a worker."""
    from skep.supervisor.serve.jobs import Dispatcher

    calls: list[dict[str, Any]] = []

    def submit(self: Any, repo: Path, instructions: str, **kwargs: Any) -> str:
        calls.append(kwargs)
        return f"task-{len(calls)}"

    monkeypatch.setattr(Dispatcher, "submit", submit)
    return calls


def test_rest_role_run_refuses_listing_the_hosts_then_answers_a_subset(
    config: SupervisorConfig, repo: Path, captured: list[dict[str, Any]]
) -> None:
    body = {"repo": str(repo), "instructions": "fix it", "role": "builder"}
    with serve_client(config) as client:
        refused = client.post("/api/runs", json=body)
        assert refused.status_code == 400, refused.text
        detail = refused.json()["detail"]
        assert all(host in detail for host in REGISTRY) and "answer_hosts" in detail
        assert "dispatch card" not in detail
        beyond = client.post("/api/runs", json={**body, "answer_hosts": ["evil.example"]})
        assert beyond.status_code == 400 and "evil.example" in beyond.json()["detail"]
        assert captured == []
        ok = client.post("/api/runs", json={**body, "answer_hosts": ["pypi.org"]})
        assert ok.status_code == 202, ok.text
    (call,) = captured
    permissions = call["permissions"]
    assert "pypi.org" in permissions.network
    assert permissions.network_ask == [h for h in REGISTRY if h != "pypi.org"]  # still asks
    assert call["dispatch_decision"].constraints["manifest_preanswered"]["network"] == ["pypi.org"]


def test_rest_run_without_a_role_is_unchanged(
    config: SupervisorConfig, repo: Path, captured: list[dict[str, Any]]
) -> None:
    with serve_client(config) as client:
        ok = client.post("/api/runs", json={"repo": str(repo), "instructions": "fix it"})
        assert ok.status_code == 202, ok.text


def test_rest_team_refuses_listing_the_hosts_then_answers_them(
    config: SupervisorConfig, repo: Path, captured: list[dict[str, Any]]
) -> None:
    body: dict[str, Any] = {
        "manager": "builder",
        "tasks": [{"repo": str(repo), "instructions": "fix it"}],
    }
    with serve_client(config) as client:
        refused = client.post("/api/teams", json=body)
        assert refused.status_code == 400, refused.text
        assert all(host in refused.json()["detail"] for host in REGISTRY)
        ok = client.post("/api/teams", json={**body, "answer_hosts": REGISTRY})
        assert ok.status_code == 202, ok.text
    (call,) = captured
    assert set(REGISTRY) <= set(call["permissions"].network)
    assert call["permissions"].network_ask == []


def test_cli_role_run_off_a_terminal_refuses_naming_the_flag(
    config: SupervisorConfig, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        ["--home", str(config.home.parent), "run", str(repo), "fix it", "--role", "builder"]
    )
    err = capsys.readouterr().err
    assert code != 0
    assert all(host in err for host in REGISTRY) and "--answer-host" in err


def test_cli_role_run_on_a_terminal_asks_and_yes_answers_for_that_run(
    config: SupervisorConfig, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import cli_cmds

    seen: dict[str, Any] = {}

    def fake_run_task(repo: Path, instructions: str, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise ValueError("stop here")  # the dispatch refusal path: nothing recorded

    monkeypatch.setattr(cli_cmds, "run_task", fake_run_task)
    monkeypatch.setattr(cli_cmds, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")
    main(["--home", str(config.home.parent), "run", str(repo), "fix it", "--role", "builder"])
    assert set(REGISTRY) <= set(seen["permissions"].network)
    assert seen["permissions"].network_ask == []
    answered = seen["dispatch_decision"].constraints["manifest_preanswered"]["network"]
    assert sorted(answered) == REGISTRY


def test_cli_role_run_with_the_flag_answers_only_what_it_lists(
    config: SupervisorConfig, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import cli_cmds

    seen: dict[str, Any] = {}

    def fake_run_task(repo: Path, instructions: str, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise ValueError("stop here")

    monkeypatch.setattr(cli_cmds, "run_task", fake_run_task)
    home = str(config.home.parent)
    args = ["--home", home, "run", str(repo), "fix it", "--role", "builder"]
    assert main([*args, "--answer-host", "evil.example"]) != 0  # not something it asks
    main([*args, "--answer-host", "pypi.org"])
    assert "pypi.org" in seen["permissions"].network
    assert seen["permissions"].network_ask == [h for h in REGISTRY if h != "pypi.org"]


def test_the_proxy_403_for_an_unanswered_ask_names_no_dispatch_card() -> None:
    status, reason, _ = admit_target("pypi.org", 443, (), denied_ports=(), asks=("pypi.org",))
    assert status == 403 and "dispatch card" not in reason
    assert "answer_hosts" in reason and "--answer-host" in reason


def test_cli_team_refuses_off_a_terminal_and_a_no_at_the_prompt(
    config: SupervisorConfig,
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    captured: list[dict[str, Any]],
) -> None:
    from skep.supervisor import cli_cmds

    home = str(config.home.parent)
    team = ["--home", home, "team", "--manager", "builder", "--task", str(repo), "fix it"]
    assert main(team) != 0
    err = capsys.readouterr().err
    assert all(host in err for host in REGISTRY) and "--answer-host" in err
    monkeypatch.setattr(cli_cmds, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "n")
    assert main(team) != 0
    assert "pypi.org" in capsys.readouterr().out
    assert captured == []  # nothing dispatched either way


def test_a_team_answer_reaches_every_member_rest_and_cli(
    config: SupervisorConfig, repo: Path, captured: list[dict[str, Any]]
) -> None:
    """ES-INT (D9 x D23): ``submit_team`` takes one manifest per member (D23);
    D9's one team answer reaches it as a copy per member, so the second
    member's asks are answered too, not only the first's."""
    tasks = [{"repo": str(repo), "instructions": "one"}, {"repo": str(repo), "instructions": "two"}]
    with serve_client(config) as client:
        body = {"manager": "builder", "tasks": tasks, "answer_hosts": ["pypi.org"]}
        ok = client.post("/api/teams", json=body)
        assert ok.status_code == 202, ok.text
    home = str(config.home.parent)
    team = ["--home", home, "team", "--manager", "builder"]
    members = ["--task", str(repo), "one", "--task", str(repo), "two"]
    main([*team, *members, "--answer-host", "pypi.org"])  # exit 3: the fake runs are "gone"
    assert len(captured) == 4
    for call in captured:
        assert "pypi.org" in call["permissions"].network
        assert "pypi.org" not in call["permissions"].network_ask
        assert call["dispatch_decision"].constraints["manifest_preanswered"]["network"] == [
            "pypi.org"
        ]
