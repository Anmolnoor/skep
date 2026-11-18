"""v123-F1 (DD-7): a worker schedule carries a role, and its workers are held
to it — the role is a delegator, ``schedule:<name>@<role>`` (ADR 0055 D4), so
it only ever tightens what the project allows. Headless: asks are dropped.
Temp homes, the builder baseline approved (``test_role_policy._home``)."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.scheduler import make_schedule, run_due
from skep.supervisor.serve import actions
from skep.supervisor.serve.chat import COMMAND_ACTOR
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import execute_mutation

from .test_delegation import _custom_role
from .test_role_policy import _home

NOW = "2026-06-11T09:00:00Z"
HOSTS = ["pypi.org", "example.com"]
PROJECT = {
    "allowed_shell_commands": [["uv", "sync"], ["git", "status"]],
    "default_network": ["pypi.org", "example.com"],
    "auto_dispatch_allowed": True,
}


@pytest.fixture
def config(tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> SupervisorConfig:
    config = _home(tmp_path, repo, capsys)
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="app",
            name="app",
            strategy="trusted_local_dev",
            phase="build",
            policy=PROJECT,
        )
    finally:
        store.close()
    return config


def _tick(
    config: SupervisorConfig, repo: Path, *, role: str | None, name: str = "nightly", **kw: Any
) -> tuple[dict[str, Any], str]:
    """Add one due schedule, tick, and return what the dispatch got and the
    tick's state ({} when nothing dispatched)."""
    captured: dict[str, Any] = {}

    def capture(repo_arg: Path, instructions: str, **kwargs: Any) -> Any:
        captured.update(kwargs)
        raise RuntimeError("stop after capture")

    store = RunStore(config.db_path)
    try:
        store.add_schedule(
            make_schedule(
                name=name,
                repo=repo,
                instructions="tidy up",
                interval_seconds=86400,
                start_at="2026-06-11T00:00:00Z",
                role=role,
                **kw,
            )
        )
        (result,) = run_due(
            store=store,
            config=config,
            now=NOW,
            dispatch=capture,
            holder=ConfigHolder(config, store),
        )
    finally:
        store.close()
    return captured, result.state


def _ledger(config: SupervisorConfig) -> list[dict[str, Any]]:
    log = config.audit_dir / "policy" / "decisions.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_a_stricter_role_drops_what_the_project_allows_and_the_card_names_the_schedule(
    config: SupervisorConfig, repo: Path
) -> None:
    _custom_role(config, "ops", policy_layer="operator")
    plain, _ = _tick(config, repo, role=None, name="plain", network=HOSTS)
    held, _ = _tick(config, repo, role="ops", network=HOSTS)
    assert "example.com" in plain["permissions"].network
    assert held["permissions"].network == [  # example.com dropped, nothing else
        host for host in plain["permissions"].network if host != "example.com"
    ]
    before, after = plain["permissions"].shell_allowlist, held["permissions"].shell_allowlist
    assert ["uv", "sync"] in before and ["uv", "sync"] not in after
    assert ["git", "status"] in after and all(prefix in before for prefix in after)
    assert held["permissions"].network_ask == [] and held["permissions"].shell_ask == []
    decision = held["dispatch_decision"]
    assert decision.delegation == {"chain": ["schedule:nightly@ops"], "role": None, "team": None}
    assert "delegated by schedule:nightly@ops; beyond the delegator, dropped: " in decision.detail
    assert "network.connect example.com" in decision.detail
    assert "shell.run uv sync" in decision.detail
    drops = _ledger(config)
    dropped = {" ".join(p) for p in before if p not in after} | {"example.com"}
    assert {row["value"] for row in drops} == dropped
    assert all(row["delegated"] == "dropped" for row in drops)
    assert all(row["delegation_chain"] == ["schedule:nightly@ops"] for row in drops)
    # where the role is what refused, the row names the schedule's hop
    uv = next(row for row in drops if row["value"] == "uv sync")
    assert uv["decided_by"].startswith("delegation:schedule:nightly@ops/role:ops/")


def test_a_role_wider_than_the_project_changes_nothing(
    config: SupervisorConfig, repo: Path
) -> None:
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="app",
            name="app",
            strategy="trusted_local_dev",
            phase="build",
            policy={
                "allowed_shell_commands": [["git", "status"]],
                "default_network": ["pypi.org"],
                "auto_dispatch_allowed": True,
            },
        )
    finally:
        store.close()
    plain, _ = _tick(config, repo, role=None, name="plain")
    held, _ = _tick(config, repo, role="builder")
    assert held["permissions"] == plain["permissions"]
    assert held["dispatch_decision"].delegation["chain"] == ["schedule:nightly@builder"]


def test_a_role_null_schedule_ticks_as_today(config: SupervisorConfig, repo: Path) -> None:
    plain, _ = _tick(config, repo, role=None, network=HOSTS)
    assert plain["dispatch_decision"].delegation is None
    assert "example.com" in plain["permissions"].network
    assert ["uv", "sync"] in plain["permissions"].shell_allowlist
    assert _ledger(config) == []


def test_a_role_that_no_longer_resolves_blocks_the_tick(
    config: SupervisorConfig, repo: Path
) -> None:
    _custom_role(config, "ops", policy_layer="operator")
    from skep.supervisor.roles import roles_dir

    (roles_dir(config.home) / "ops.json").unlink()
    captured, state = _tick(config, repo, role="ops")
    assert captured == {}
    assert state.startswith("policy_blocked: schedule 'nightly' runs as role 'ops'")
    assert "no role 'ops'" in state and "re-create the schedule" in state


def test_a_refused_narrowing_blocks_the_tick(config: SupervisorConfig, repo: Path) -> None:
    """A role denied workspace writes, stored before this check (or by hand),
    refuses the dispatch at tick time — never a worker without its role."""
    captured, state = _tick(config, repo, role="assistant")
    assert captured == {}
    assert state.startswith("policy_blocked: delegated by schedule:nightly@assistant")
    assert "filesystem.write workspace exceeds the delegator" in state


def test_an_unbound_run_decides_its_hosts_under_the_role(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    from .conftest import git

    other = tmp_path / "unbound"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    _custom_role(config, "ops", policy_layer="operator")
    held, _ = _tick(config, other, role="ops", network=["pypi.org", "example.com"])
    assert held["permissions"].network == ["pypi.org"]
    assert held["dispatch_decision"].delegation["chain"] == ["schedule:nightly@ops"]


def test_an_old_schedules_table_migrates_to_role_null(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    with contextlib.closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(
            "CREATE TABLE schedules (name TEXT PRIMARY KEY, repo TEXT NOT NULL, ref TEXT,"
            " worker_kind TEXT NOT NULL, instructions TEXT NOT NULL, network_json TEXT NOT NULL,"
            " env_allow_json TEXT NOT NULL, interval_seconds INTEGER NOT NULL,"
            " enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, last_run_at TEXT,"
            " next_run_at TEXT NOT NULL, last_task_id TEXT, last_state TEXT,"
            " template_name TEXT, params_json TEXT NOT NULL DEFAULT '{}', chat_id TEXT,"
            " once INTEGER NOT NULL DEFAULT 0, chain TEXT, last_output TEXT)"
        )
        conn.execute(
            "INSERT INTO schedules (name, repo, worker_kind, instructions, network_json,"
            " env_allow_json, interval_seconds, created_at, next_run_at)"
            " VALUES ('old', '/r', 'coding', 'x', '[]', '[]', 60, 't', 't')"
        )
    store = RunStore(db)
    try:
        old = store.get_schedule("old")
        assert old is not None and old.role is None
        store.add_schedule(make_schedule(name="new", repo="/r", instructions="x",
                                         interval_seconds=60, role="builder"))  # fmt: skip
        new = store.get_schedule("new")
        assert new is not None and new.role == "builder"
    finally:
        store.close()


def _check(config: SupervisorConfig, **kw: Any) -> None:
    store = RunStore(config.db_path)
    try:
        actions.check_schedule_role(
            ConfigHolder(config, store), store, make_schedule(interval_seconds=60, **kw)
        )
    finally:
        store.close()


@pytest.mark.parametrize("kind", ["script", "prompt", "note", "digest"])
def test_a_repo_less_kind_refuses_a_role(config: SupervisorConfig, kind: str) -> None:
    with pytest.raises(ValueError, match=f"a {kind} schedule takes no role") as refused:
        _check(config, name="s", repo="", instructions="x", worker_kind=kind, role="builder")
    assert "coding, audit, researcher or document" in str(refused.value)


def test_an_unknown_role_refuses(config: SupervisorConfig, repo: Path) -> None:
    with pytest.raises(ValueError, match="no role 'nobody'"):
        _check(config, name="s", repo=repo, instructions="x", role="nobody")


def test_a_role_denied_workspace_writes_refuses_a_worker_schedule(
    config: SupervisorConfig, repo: Path
) -> None:
    with pytest.raises(
        ValueError, match="role assistant cannot run a researcher: it denies workspace writes"
    ):
        _check(
            config, name="s", repo=repo, instructions="x", worker_kind="researcher",
            role="assistant",
        )  # fmt: skip
    _check(config, name="s", repo=repo, instructions="x", role="builder")  # passes
    _check(config, name="s", repo=repo, instructions="x", role=None)  # no role: nothing to check


def _propose(
    config: SupervisorConfig, args: dict[str, Any], *, chat_role: str | None, actor: str
) -> dict[str, Any]:
    store = RunStore(config.db_path)
    try:
        chat_id = store.create_chat(title="c", model=None).chat_id
        if chat_role is not None:
            store.set_chat_role(chat_id, chat_role)
        return dict(
            execute_mutation(
                "propose_schedule",
                {"name": "nightly", "every": "1d", **args},
                store=store,
                holder=ConfigHolder(config, store),
                runner=None,  # type: ignore[arg-type]
                actor=actor,
                chat_id=chat_id,
            )
        )
    finally:
        store.close()


def test_a_builder_chat_coding_schedule_keeps_only_what_its_role_allows(
    config: SupervisorConfig, repo: Path
) -> None:
    view = _propose(
        config, {"repo": str(repo), "instructions": "tidy up", "network": HOSTS},
        chat_role="builder", actor="chat-user",
    )  # fmt: skip
    assert view["role"] == "builder"
    assert view["role_note"] == actions.SCHEDULE_ROLE_NOTE
    captured: dict[str, Any] = {}

    def capture(repo_arg: Path, instructions: str, **kwargs: Any) -> Any:
        captured.update(kwargs)
        raise RuntimeError("stop after capture")

    store = RunStore(config.db_path)
    try:
        run_due(store=store, config=config, now="2099-01-01T00:00:00Z", dispatch=capture)
    finally:
        store.close()
    # the builder baseline's own grants the builder role holds; the named
    # example.com it only asks for, and a schedule drops asks (v123-F3)
    assert ["uv", "sync"] in captured["permissions"].shell_allowlist
    assert "pypi.org" in captured["permissions"].network
    assert "example.com" not in captured["permissions"].network
    assert captured["dispatch_decision"].delegation["chain"] == ["schedule:nightly@builder"]
    note = captured["dispatch_decision"].detail
    assert "delegated by schedule:nightly@builder; beyond the delegator, dropped: " in note
    assert note.endswith("network.connect example.com")


def test_a_chat_cannot_name_another_role(config: SupervisorConfig, repo: Path) -> None:
    with pytest.raises(ValueError, match="runs in this chat's role builder;"):
        _propose(
            config, {"repo": str(repo), "instructions": "x", "role": "operator"},
            chat_role="builder", actor="chat-user",
        )  # fmt: skip


def test_a_role_less_chat_is_an_assistant_and_its_worker_schedule_says_so(
    config: SupervisorConfig, repo: Path
) -> None:
    with pytest.raises(ValueError, match="role assistant cannot run a coding"):
        _propose(
            config, {"repo": str(repo), "instructions": "x"}, chat_role=None, actor="chat-user"
        )


def test_a_chat_proposed_prompt_schedule_is_still_accepted_role_less(
    config: SupervisorConfig,
) -> None:
    view = _propose(
        config, {"caste": "prompt", "instructions": "summarize"},
        chat_role="builder", actor="chat-user",
    )  # fmt: skip
    assert view["role"] is None and "role_note" not in view


def test_an_operator_deck_command_stores_no_role(config: SupervisorConfig, repo: Path) -> None:
    view = _propose(
        config, {"repo": str(repo), "instructions": "x"}, chat_role="builder", actor=COMMAND_ACTOR
    )
    assert view["role"] is None


def test_the_card_line_is_absent_when_the_confirm_would_refuse_the_role(
    config: SupervisorConfig, repo: Path
) -> None:
    from skep.supervisor.serve.tools import schedule_role_line

    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        chat_id = store.create_chat(title="c", model=None, role="builder").chat_id
        args = {"name": "n", "repo": str(repo), "every": "1d", "instructions": "x"}
        assert schedule_role_line(store, holder, args, chat_id) == (
            f"runs as role builder: {actions.SCHEDULE_ROLE_NOTE}"
        )
        assert schedule_role_line(store, holder, {**args, "role": "operator"}, chat_id) is None
        assert schedule_role_line(store, holder, {**args, "caste": "note"}, chat_id) is None
        assert schedule_role_line(store, holder, args, None) is None  # no chat, no role
    finally:
        store.close()


def test_a_chat_created_researcher_schedule_keeps_its_hosts_and_its_chat(
    config: SupervisorConfig, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v123-F3 (DD-9): a research schedule from chat stores its hosts and its
    chat; at tick its role drops the hosts it denies, and the terminal notice
    reaches the chat."""
    from types import SimpleNamespace

    from skep.supervisor.serve import run_status

    store = RunStore(config.db_path)
    try:
        chat_id = store.create_chat(title="c", model=None, role="builder").chat_id
        execute_mutation(
            "propose_schedule",
            {"name": "digest-web", "every": "1d", "repo": str(repo), "caste": "researcher",
             "instructions": "what changed in uv", "network": HOSTS},
            store=store, holder=ConfigHolder(config, store),
            runner=None,  # type: ignore[arg-type]
            actor="chat-user", chat_id=chat_id,
        )  # fmt: skip
        stored = store.get_schedule("digest-web")
        assert stored is not None
        assert (stored.network, stored.chat_id, stored.role) == (HOSTS, chat_id, "builder")
        captured: dict[str, Any] = {}

        def dispatch(repo_arg: Path, instructions: str, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return SimpleNamespace(record=SimpleNamespace(task_id="t-1", state="failed"))

        monkeypatch.setattr(
            run_status, "run_terminal_text", lambda *a, **kw: ("research failed: boom", "error")
        )
        run_due(store=store, config=config, now="2099-01-01T00:00:00Z", dispatch=dispatch)
        assert captured["worker_kind"] == "researcher"
        assert "pypi.org" in captured["permissions"].network
        assert "example.com" not in captured["permissions"].network
        said = [m.content for m in store.chat_messages(chat_id)]
        assert "[digest-web] research failed: boom" in said
    finally:
        store.close()


def test_the_card_says_which_hosts_the_role_will_drop(
    config: SupervisorConfig, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor.serve.tools import schedule_role_line

    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        chat_id = store.create_chat(title="c", model=None, role="builder").chat_id
        args = {"name": "n", "repo": str(repo), "every": "1d", "instructions": "x"}
        kept = f"runs as role builder: {actions.SCHEDULE_ROLE_NOTE}"
        line = schedule_role_line(store, holder, {**args, "network": HOSTS}, chat_id)
        assert line == f"{kept}; drops hosts: example.com"
        assert schedule_role_line(store, holder, {**args, "network": ["pypi.org"]}, chat_id) == kept

        def refuse(*a: Any, **kw: Any) -> Any:
            raise HTTPException(status_code=403, detail="baseline tampered")

        monkeypatch.setattr(actions, "delegate_permissions", refuse)
        # a narrowing that refuses names no hosts: the confirm says why
        assert schedule_role_line(store, holder, {**args, "network": HOSTS}, chat_id) == kept
    finally:
        store.close()
