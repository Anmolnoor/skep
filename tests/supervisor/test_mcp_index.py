"""Phase 06 (6.2): the MCP index names each server's tools — from the cache.

`- mail (stdio)` hid every tool behind a list_mcp_tools round-trip. The index
now renders the ES-F160 discovery cache (`mcp_client._LISTED`): tool names,
'*' on each call that is not a read. Rendering never spawns a server (zero
per-turn subprocesses); only list_mcp_tools and the startup warm fill the
cache. Phase 06 kept forged servers (`forge-*`) out of the warm while their
runtime egress was open; Phase 07.1 walled every forged exchange, so the
warm lists them too (test_forge_runtime_confinement).
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Mapping
from typing import Any

import pytest
from fastapi.testclient import TestClient

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.mcp_client import (
    FORGED_SERVER_PREFIX,
    MCPServerConfig,
    remove_mcp_server,
    save_mcp_server,
    warm_listed_tools,
)
from skep.supervisor.serve.app import create_app
from skep.supervisor.serve.chat import mcp_index_block
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import execute_read_tool

MAIL_TOOLS = [
    {"name": "list_messages", "annotations": {"readOnlyHint": True}},
    {"name": "read_message", "annotations": {"readOnlyHint": True}},
    {"name": "search_mail", "annotations": {"readOnlyHint": True}},
    {"name": "send_mail", "description": "send one message"},
]
MAIL = MCPServerConfig(server_id="mail", transport="stdio", command=("mail-mcp",), scope="email")


@pytest.fixture()
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every server runner_for_config builds — i.e. every process a listing
    would spawn — recorded by server id."""
    ids: list[str] = []

    def runner_for(config: MCPServerConfig, **_: Any) -> Any:
        ids.append(config.server_id)

        def run(method: str, params: Mapping[str, object]) -> Mapping[str, object]:
            assert method == "tools/list"
            return {"tools": MAIL_TOOLS}

        return run

    monkeypatch.setattr("skep.supervisor.mcp_client.runner_for_config", runner_for)
    return ids


def _list(store: RunStore, config: SupervisorConfig, server_id: str) -> Any:
    return execute_read_tool(
        "list_mcp_tools", {"server_id": server_id}, store=store, holder=ConfigHolder(config, store)
    )


def test_the_index_names_cached_tools_and_spawns_nothing(
    config: SupervisorConfig, spawned: list[str]
) -> None:
    store = RunStore(config.db_path)
    try:
        save_mcp_server(store, MAIL)
        # Nothing cached yet: the id alone, and rendering spawned nothing.
        assert mcp_index_block(store).splitlines()[1] == "- mail (stdio)"
        assert spawned == []
        assert "error" not in _list(store, config, "mail")  # the explicit refresh
        assert spawned == ["mail"]
        for _ in range(3):  # every turn renders; none spawns
            block = mcp_index_block(store)
        assert spawned == ["mail"]
        assert "- mail (stdio): list_messages, read_message, search_mail, send_mail*" in block
        assert "list_mcp_tools" in block.splitlines()[0]  # the detail verb stays named
    finally:
        store.close()


def test_register_change_and_unregister_invalidate_and_list_refreshes(
    config: SupervisorConfig, spawned: list[str]
) -> None:
    store = RunStore(config.db_path)
    try:
        save_mcp_server(store, MAIL)
        _list(store, config, "mail")
        assert "send_mail*" in mcp_index_block(store)
        # Changed config (re-register): the old names are dropped, never shown stale.
        save_mcp_server(store, dataclasses.replace(MAIL, command=("mail-mcp", "--v2")))
        assert mcp_index_block(store).splitlines()[1] == "- mail (stdio)"
        _list(store, config, "mail")
        assert "send_mail*" in mcp_index_block(store)
        # list_mcp_tools always re-lists (the explicit refresh), even on a hit.
        _list(store, config, "mail")
        assert spawned == ["mail", "mail", "mail"]
        remove_mcp_server(store, "mail")
        assert mcp_index_block(store) == ""
    finally:
        store.close()


def test_the_startup_warm_lists_plain_and_forged_servers(
    config: SupervisorConfig, spawned: list[str]
) -> None:
    forged = f"{FORGED_SERVER_PREFIX}weather"
    store = RunStore(config.db_path)
    try:
        save_mcp_server(store, MAIL)
        save_mcp_server(
            store, MCPServerConfig(server_id=forged, transport="stdio", command=("python3", "t.py"))
        )
        assert warm_listed_tools(store) == ["mail", forged]
        assert spawned == ["mail", forged]
        lines = mcp_index_block(store).splitlines()
        assert "- mail (stdio): list_messages, read_message, search_mail, send_mail*" in lines
        # Phase 07: a forged tool's readOnlyHint is untrusted — every call asks.
        assert f"- {forged} (stdio): list_messages*, read_message*, search_mail*, send_mail*" in (
            lines
        )
    finally:
        store.close()


def test_serve_startup_warms_in_the_background_forged_included(
    config: SupervisorConfig, spawned: list[str]
) -> None:
    store = RunStore(config.db_path)
    try:
        save_mcp_server(
            store,
            MCPServerConfig(
                server_id=f"{FORGED_SERVER_PREFIX}x", transport="stdio", command=("python3",)
            ),
        )
        save_mcp_server(store, MAIL)
    finally:
        store.close()
    with TestClient(create_app(config, start_ticker=False)):
        deadline = time.monotonic() + 10
        while len(spawned) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
    assert spawned == [f"{FORGED_SERVER_PREFIX}x", "mail"]


def test_a_failing_server_is_skipped_by_the_warm(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def runner_for(config: MCPServerConfig, **_: Any) -> Any:
        def run(method: str, params: Mapping[str, object]) -> Mapping[str, object]:
            raise OSError("no such binary")

        return run

    monkeypatch.setattr("skep.supervisor.mcp_client.runner_for_config", runner_for)
    store = RunStore(config.db_path)
    try:
        save_mcp_server(store, MAIL)
        assert warm_listed_tools(store) == []  # a failed listing is never kept
        assert mcp_index_block(store).splitlines()[1] == "- mail (stdio)"
    finally:
        store.close()
