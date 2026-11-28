"""v135 Wave 2a (ADR 0069 section 1): the registration is the authority.

New fields with two defaults in two places, a supervisor-issued incarnation
that changes on every save, slug ids for strict servers, and a card that
shows the effective grant without starting anything.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from skep.supervisor import mcp_client, mcp_sandbox
from skep.supervisor.mcp_client import (
    MCPError,
    MCPServerConfig,
    load_mcp_servers,
    remove_mcp_server,
    save_mcp_server,
    warm_listed_tools,
)
from skep.supervisor.serve.cards import card_summary, high_tier, recorded_card
from skep.supervisor.serve.tools import execute_mutation
from skep.supervisor.store import RunStore


@pytest.fixture()
def store(tmp_path: Path) -> Any:
    db = tmp_path / "skep" / "supervisor" / "supervisor.sqlite3"
    db.parent.mkdir(parents=True)
    opened = RunStore(db)
    yield opened
    opened.close()


@pytest.fixture()
def no_launch(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Every way a server could start, made to record and refuse."""
    started: list[Any] = []

    def refuse(*args: Any, **kwargs: Any) -> Any:
        started.append(args)
        raise AssertionError(f"something started: {args!r}")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(mcp_client, "runner_for_config", refuse)
    return started


def _register(store: RunStore, **args: Any) -> Any:
    holder = SimpleNamespace(current=SimpleNamespace(home=store.db_path.parent))
    return execute_mutation(
        "register_mcp_server",
        {"transport": "stdio", **args},
        store=store,
        holder=holder,  # type: ignore[arg-type]
        runner=None,  # type: ignore[arg-type]
        actor="tester",
    )


# --- two defaults, in two places ----------------------------------------------


def test_a_new_registration_without_sandbox_is_strict(store: RunStore) -> None:
    result = _register(store, server_id="tool", command=["/usr/bin/true"])
    assert result["sandbox"] == "strict"
    assert load_mcp_servers(store)["tool"].sandbox == "strict"
    assert mcp_sandbox.registration_from_args({"server_id": "x"}).sandbox == "strict"


def test_a_stored_registration_without_the_field_loads_as_host(store: RunStore) -> None:
    old = [{"server_id": "Old Server.1", "transport": "stdio", "command": ["npx", "x@latest"]}]
    store.set_setting(mcp_client.MCP_SERVERS_SETTINGS_KEY, json.dumps(old))
    loaded = load_mcp_servers(store)["Old Server.1"]
    assert loaded.sandbox == "host" and loaded.incarnation == ""
    loaded.validate()  # an older id still loads and validates as host


def test_strict_refuses_what_it_cannot_confine(store: RunStore) -> None:
    cases = {
        "a bare command": ({"server_id": "a", "command": ["python3", "x.py"]}, "absolute path"),
        "npx": ({"server_id": "a", "command": ["npx", "@playwright/mcp@latest"]}, "resolves"),
        "a path id": ({"server_id": "../a", "command": ["/usr/bin/true"]}, "directory name"),
        "open egress": (
            {"server_id": "a", "command": ["/usr/bin/true"], "egress": "open"},
            "Wave 2b",
        ),
        "a wildcard host": (
            {"server_id": "a", "command": ["/usr/bin/true"], "allowed_hosts": ["*"]},
            "no wildcard",
        ),
        "a refused secret": (
            {"server_id": "a", "command": ["/usr/bin/true"], "secret_names": ["LD_PRELOAD"]},
            "refused as a secret name",
        ),
        "an unpinned install": (
            {"server_id": "a", "command": ["srv"], "install": "npm:some-server@latest"},
            "npm:some-server@<x.y.z>",
        ),
        "a range": (
            {"server_id": "a", "command": ["srv"], "install": "pypi:mcp-x>=1.0"},
            "pypi:mcp-x==<version>",
        ),
        "a skep-home read": (
            {"server_id": "a", "command": ["/usr/bin/true"], "read_paths": [str(store.db_path)]},
            "skep home",
        ),
    }
    for label, (args, says) in cases.items():
        with pytest.raises(ValueError, match=r"(?s).*" + says.replace("*", r"\*")) as refused:
            _register(store, **args)
        assert refused.value is not None, label
    assert load_mcp_servers(store) == {}


def test_sandbox_fields_mean_nothing_on_http_or_host(store: RunStore) -> None:
    with pytest.raises(ValueError, match="http server"):
        _register(store, server_id="h", transport="http", url="http://x/mcp", sandbox="strict")
    with pytest.raises(ValueError, match="sandbox='host'"):
        _register(store, server_id="h", command=["x"], sandbox="host", allowed_hosts=["a.b"])
    # review S12: an http server omits them (as the tool description says) and
    # passes; one that names any of them, even sandbox='host', is refused
    _register(store, server_id="web", transport="http", url="http://x/mcp")
    assert load_mcp_servers(store)["web"].sandbox == "host"
    for field, value in (("sandbox", "host"), ("read_paths", ["/srv"]), ("egress", "none")):
        with pytest.raises(ValueError, match=f"{field}.*http server"):
            _register(store, server_id="h", transport="http", url="http://x/mcp", **{field: value})
    assert "error" not in mcp_sandbox.grant_view(
        mcp_sandbox.registration_from_args({"server_id": "w", "transport": "http", "url": "u"}),
        store.db_path.parent.parent,
    )


def test_a_pinned_install_with_its_entry_point_name_is_accepted(store: RunStore) -> None:
    _register(store, server_id="ev", command=["mcp-ev", "stdio"], install="npm:@s/ev@1.2.3")
    _register(store, server_id="py", command=["mcp-py"], install="pypi:mcp-py[x]==0.4.1")
    servers = load_mcp_servers(store)
    assert servers["ev"].install == ("npm", "@s/ev@1.2.3")
    assert servers["py"].install == ("pypi", "mcp-py[x]==0.4.1")
    with pytest.raises(ValueError, match="entry point"):
        _register(store, server_id="ev", command=["/abs/x"], install="npm:@s/ev@1.2.3")


# --- the incarnation ---------------------------------------------------------------


def test_the_incarnation_changes_on_every_save(store: RunStore) -> None:
    config = MCPServerConfig("tool", "stdio", command=("/usr/bin/true",), sandbox="strict")
    first = save_mcp_server(store, config).incarnation
    second = save_mcp_server(store, config).incarnation
    remove_mcp_server(store, "tool")
    third = save_mcp_server(store, config).incarnation
    assert len({first, second, third}) == 3 and all(len(i) == 16 for i in (first, second, third))
    assert load_mcp_servers(store)["tool"].incarnation == third
    # a caller-supplied incarnation never survives the save
    assert save_mcp_server(store, dataclasses.replace(config, incarnation="mine")).incarnation != (
        "mine"
    )


def test_a_save_waits_for_a_running_call(store: RunStore) -> None:
    """1.4: registration changes are serialized against validation and dispatch."""
    config = MCPServerConfig("tool", "stdio", command=("/usr/bin/true",), sandbox="strict")
    order: list[str] = []
    with mcp_sandbox.REGISTRY_GATE.reading():

        def save() -> None:
            save_mcp_server(store, config)
            order.append("saved")

        saver = threading.Thread(target=save)
        saver.start()
        time.sleep(0.2)
        order.append("call done")
    saver.join(5)
    assert order == ["call done", "saved"]


# --- the card comes before any launch -----------------------------------------------


def test_the_registration_card_shows_the_grant_and_starts_nothing(
    store: RunStore, no_launch: list[Any]
) -> None:
    args = {
        "server_id": "weather",
        "transport": "stdio",
        "command": ["weather-mcp", "--stdio"],
        "install": "npm:weather-mcp@2.0.1",
        "allowed_hosts": ["api.weather.test"],
        "read_paths": ["/srv/data"],
        "secret_names": ["WEATHER_KEY"],
    }
    record = recorded_card(
        "register_mcp_server", args, high_tier("register_mcp_server", args), store
    )
    assert record is not None
    grant = record["mcp_grant"]
    tree = store.db_path.parent.parent / "mcp" / "weather" / "<new incarnation>"
    assert grant["argv"] == [str(tree / "node_modules" / ".bin" / "weather-mcp"), "--stdio"]
    assert grant["mode"] == "strict" and grant["egress"] == "hosts"
    assert grant["hosts"] == ["api.weather.test"] and grant["read_paths"] == ["/srv/data"]
    assert grant["secret_names"] == ["WEATHER_KEY"] and grant["secrets_unset"] == ["WEATHER_KEY"]
    assert grant["install"] == "npm:weather-mcp@2.0.1"
    summary = card_summary("register_mcp_server", args, store=store, recorded=record)
    assert "no package scripts" in summary["risk"]
    assert "skep mcp secret set weather WEATHER_KEY" in summary["risk"]
    assert "only api.weather.test" in summary["risk"]
    assert summary["tier"] == "high" and "serve token" not in summary["high"]
    assert no_launch == []


def test_a_host_card_is_high_and_says_it_can_read_the_serve_token(
    store: RunStore, no_launch: list[Any]
) -> None:
    args = {"server_id": "legacy", "transport": "stdio", "command": ["npx", "x@latest"],
            "sandbox": "host"}  # fmt: skip
    for tool, tool_args in (("register_mcp_server", args), ("setup_browser", {})):
        summary = card_summary(tool, tool_args, store=store)
        assert summary["tier"] == "high"
        assert "unsandboxed" in summary["high"] and "serve token" in summary["high"]
        assert "answer its own cards" in summary["high"]
        assert "unsandboxed" in summary["risk"]
    browser = card_summary("setup_browser", {}, store=store)["mcp_grant"]
    assert browser["argv"] == ["npx", "@playwright/mcp@latest"] and browser["mode"] == "host"
    assert no_launch == []


def test_latest_is_allowed_only_for_host(store: RunStore) -> None:
    with pytest.raises(ValueError, match="resolves a package"):
        _register(store, server_id="pw", command=["npx", "@playwright/mcp@latest"], scope="browse")
    _register(store, server_id="pw", command=["npx", "@playwright/mcp@latest"], scope="browse",
              sandbox="host")  # fmt: skip


def test_an_invalid_registration_card_says_why_without_starting(
    store: RunStore, no_launch: list[Any]
) -> None:
    args = {"server_id": "a", "transport": "stdio", "command": ["python3", "x.py"]}
    summary = card_summary("register_mcp_server", args, store=store)
    assert summary["risk"].startswith("it cannot be registered as shown:")
    assert no_launch == []


def test_nothing_launches_for_a_pending_card_including_at_startup(
    store: RunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned: list[str] = []

    def runner_for(config: MCPServerConfig, **_: Any) -> Any:
        spawned.append(config.server_id)
        return lambda method, params: {"tools": []}

    monkeypatch.setattr(mcp_client, "runner_for_config", runner_for)
    chat_id = store.create_chat(title="t", model=None).chat_id
    args = {"server_id": "pending", "transport": "stdio", "command": ["/usr/bin/true"]}
    store.add_chat_action(
        chat_id, tool="register_mcp_server", args=args,
        card=recorded_card("register_mcp_server", args, None, store),
    )  # fmt: skip
    save_mcp_server(store, MCPServerConfig("committed", "stdio", command=("/usr/bin/true",)))
    assert warm_listed_tools(store, timeout=5) == ["committed"]
    assert spawned == ["committed"]


def test_setup_browser_saves_host_and_lists_only_after_the_verdict(
    store: RunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    listed: list[MCPServerConfig] = []

    def refresh(store_: RunStore, config: MCPServerConfig) -> tuple[Any, ...]:
        listed.append(config)
        raise MCPError("npx not found")

    monkeypatch.setattr(mcp_client, "refresh_listed_tools", refresh)
    holder = SimpleNamespace(current=SimpleNamespace(home=store.db_path.parent))
    result = execute_mutation(
        "setup_browser", {}, store=store, holder=holder,  # type: ignore[arg-type]
        runner=None, actor="tester",  # type: ignore[arg-type]
    )  # fmt: skip
    saved = load_mcp_servers(store)["browser"]
    assert saved.sandbox == "host" and "npx" in result["handshake_failed"]
    assert listed == [saved]  # the committed registration, with its incarnation


def test_the_host_disclosure_survives_a_doubly_tainted_chat(
    store: RunStore, no_launch: list[Any]
) -> None:
    """Review S15: the trifecta reason replaces the HIGH line, so the host
    grant line itself says it can read the serve token and self-approve."""
    from skep.supervisor.store import ChatTaint

    tainted = ChatTaint(private=1, untrusted=2)
    args = {"server_id": "loose", "transport": "stdio", "command": ["/usr/bin/x"],
            "sandbox": "host"}  # fmt: skip
    for tool, tool_args in (("register_mcp_server", args), ("setup_browser", {})):
        summary = card_summary(tool, tool_args, store=store, taint=tainted)
        assert "serve token" not in summary["high"]  # the trifecta reason won the HIGH line
        assert "serve token" in summary["risk"] and "answer its own cards" in summary["risk"]
    assert no_launch == []
