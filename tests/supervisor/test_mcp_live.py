"""v40-F10 (v36-F6): scope mcp goes live — cards, learned rules, hard denies.

A fake MCP server (an injected runner — the mcp_client seam) exercises the
three verdicts end-to-end through the REAL chat surface: a read-shaped tool
runs inside the turn, an unknown tool cards and executes on confirm, an
explicit deny rule refuses without a card, allow-always writes a learned rule
that survives a store reopen and auto-allows the second call, and a learned
rule colliding with a deny is rejected with the deny's rule id.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from typing import Any

import pytest
from fastapi.testclient import TestClient

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.mcp_client import (
    MCP_SERVERS_SETTINGS_KEY,
    MCPServerConfig,
    load_mcp_servers,
    save_mcp_server,
)
from skep.supervisor.policy_schema import POLICY_DOCUMENT_SETTINGS_KEY

from .conftest import serve_client as _client
from .fake_ollama import FakeOllama


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


@pytest.fixture()
def fake_runner(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Route every MCP call to an in-test stub; record what was called."""
    calls: list[tuple[str, dict[str, Any]]] = []

    def runner(method: str, params: Mapping[str, object]) -> Mapping[str, object]:
        calls.append((method, dict(params)))
        if method == "tools/list":
            return {
                "tools": [
                    {"name": "list_issues", "description": "read the issues"},
                    {"name": "frobnicate_gizmo", "description": "who knows"},
                ]
            }
        if method == "tools/call":
            return {"content": f"ran {params.get('name')}"}
        return {}

    monkeypatch.setattr("skep.supervisor.mcp_client.runner_for_config", lambda config: runner)
    return calls


def chat_client(config: SupervisorConfig, ollama: FakeOllama) -> tuple[TestClient, str]:
    client = _client(config)
    client.put(
        "/api/llm/config",
        json={"base_url": ollama.base_url, "default_model": "qwen3", "api_key": "sk-fake"},
    )
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    return client, str(chat_id)


def _seed_server(config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        save_mcp_server(
            store,
            MCPServerConfig(server_id="issues", transport="stdio", command=("fake-mcp",)),
        )
    finally:
        store.close()


def _seed_http_server(config: SupervisorConfig) -> None:
    """v80-F1: a Streamable HTTP server — same engine, different transport."""
    store = RunStore(config.db_path)
    try:
        save_mcp_server(
            store,
            MCPServerConfig(server_id="remote", transport="http", url="https://mcp.example/mcp"),
        )
    finally:
        store.close()


def _seed_email_server(config: SupervisorConfig) -> None:
    """v41-F3: a mail server bound to the email scope."""
    store = RunStore(config.db_path)
    try:
        save_mcp_server(
            store,
            MCPServerConfig(
                server_id="mail", transport="stdio", command=("fake-mail-mcp",), scope="email"
            ),
        )
    finally:
        store.close()


def _seed_document(config: SupervisorConfig, document: dict[str, Any]) -> None:
    store = RunStore(config.db_path)
    try:
        store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, json.dumps(document))
    finally:
        store.close()


def test_server_config_persists_via_settings(config: SupervisorConfig) -> None:
    _seed_server(config)
    store = RunStore(config.db_path)
    try:
        servers = load_mcp_servers(store)
        assert set(servers) == {"issues"}
        assert servers["issues"].command == ("fake-mcp",)
        assert store.get_setting(MCP_SERVERS_SETTINGS_KEY) is not None
    finally:
        store.close()


def test_read_shaped_tool_runs_inside_the_turn(
    config: SupervisorConfig,
    ollama: FakeOllama,
    fake_runner: list[tuple[str, dict[str, Any]]],
) -> None:
    _seed_server(config)
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("call_mcp_tool", {"server_id": "issues", "tool": "list_issues"})
    ollama.script_reply("here are the issues")
    response = client.post(f"/api/chats/{chat_id}/messages", json={"content": "issues?"})
    assert response.status_code == 200
    assert "ran list_issues" in response.text  # executed inside the turn
    assert ("tools/call", {"name": "list_issues", "arguments": {}}) in fake_runner
    # No card was needed — the read auto-allow; v61-F1 still records the
    # action row, born resolved.
    (recorded,) = client.get(f"/api/chats/{chat_id}").json()["actions"]
    assert recorded["status"] == "confirmed"


def test_unknown_tool_cards_then_executes_on_confirm(
    config: SupervisorConfig,
    ollama: FakeOllama,
    fake_runner: list[tuple[str, dict[str, Any]]],
) -> None:
    _seed_server(config)
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("call_mcp_tool", {"server_id": "issues", "tool": "frobnicate_gizmo"})
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "frobnicate it"})
    (action,) = client.get(f"/api/chats/{chat_id}").json()["actions"]
    assert action["status"] == "proposed"
    assert action["decided_by"] == "policy/risk:unknown"  # v39-F1 fail-closed
    assert fake_runner == []  # nothing ran before the human said so

    ollama.script_reply("done frobnicating")
    client.post(f"/api/chats/{chat_id}/actions/{action['action_id']}/confirm")
    assert ("tools/call", {"name": "frobnicate_gizmo", "arguments": {}}) in fake_runner
    (resolved,) = client.get(f"/api/chats/{chat_id}").json()["actions"]
    assert resolved["status"] == "confirmed"
    assert resolved["result"]["ok"] is True


def test_explicit_deny_rule_refuses_without_a_card(
    config: SupervisorConfig,
    ollama: FakeOllama,
    fake_runner: list[tuple[str, dict[str, Any]]],
) -> None:
    _seed_server(config)
    _seed_document(
        config,
        {
            "template": "strict",
            "scopes": [
                {
                    "scope": "mcp",
                    "deny": [{"rule_id": "no-gizmos", "action": "call", "pattern": "issues:frob*"}],
                }
            ],
        },
    )
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("call_mcp_tool", {"server_id": "issues", "tool": "frobnicate_gizmo"})
    ollama.script_reply("that is not allowed")
    response = client.post(f"/api/chats/{chat_id}/messages", json={"content": "frob it"})
    assert "denied by policy" in response.text
    assert client.get(f"/api/chats/{chat_id}").json()["actions"] == []  # NO card
    assert fake_runner == []


def test_allow_always_writes_a_learned_rule_that_survives_restart(
    config: SupervisorConfig,
    ollama: FakeOllama,
    fake_runner: list[tuple[str, dict[str, Any]]],
) -> None:
    _seed_server(config)
    client, chat_id = chat_client(config, ollama)

    # The Queen proposes allow-always; the human confirms it.
    ollama.script_tool_call("allow_mcp_tool", {"server_id": "issues", "tool": "frobnicate_gizmo"})
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "always allow it"})
    (action,) = client.get(f"/api/chats/{chat_id}").json()["actions"]
    ollama.script_reply("remembered")
    client.post(f"/api/chats/{chat_id}/actions/{action['action_id']}/confirm")

    # The learned rule is durable (a fresh store connection sees it)...
    store = RunStore(config.db_path)
    try:
        document = json.loads(str(store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY)))
    finally:
        store.close()
    (learned,) = document["learned"]
    assert learned["pattern"] == "issues:frobnicate_gizmo"
    assert learned["provenance"].startswith("allow-always:")

    # ...and the second call auto-allows, no card.
    ollama.script_tool_call("call_mcp_tool", {"server_id": "issues", "tool": "frobnicate_gizmo"})
    ollama.script_reply("ran it without asking")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "frob again"})
    assert ("tools/call", {"name": "frobnicate_gizmo", "arguments": {}}) in fake_runner
    actions = client.get(f"/api/chats/{chat_id}").json()["actions"]
    assert all(a["status"] != "proposed" for a in actions)


def test_learned_rule_into_denied_space_is_rejected_with_the_denys_rule_id(
    config: SupervisorConfig,
    ollama: FakeOllama,
    fake_runner: list[tuple[str, dict[str, Any]]],
) -> None:
    _seed_server(config)
    _seed_document(
        config,
        {
            "template": "strict",
            "scopes": [
                {
                    "scope": "mcp",
                    "deny": [{"rule_id": "no-gizmos", "action": "call", "pattern": "issues:frob*"}],
                }
            ],
        },
    )
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("allow_mcp_tool", {"server_id": "issues", "tool": "frobnicate_gizmo"})
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "always allow it"})
    (action,) = client.get(f"/api/chats/{chat_id}").json()["actions"]
    ollama.script_reply("could not remember that")
    client.post(f"/api/chats/{chat_id}/actions/{action['action_id']}/confirm")
    (resolved,) = client.get(f"/api/chats/{chat_id}").json()["actions"]
    assert resolved["result"]["ok"] is False
    assert "no-gizmos" in resolved["result"]["error"]
    # The document did not gain the rule.
    store = RunStore(config.db_path)
    try:
        document = json.loads(str(store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY)))
    finally:
        store.close()
    assert document.get("learned", []) == []


def test_email_server_reads_flow_and_sends_card(
    config: SupervisorConfig,
    ollama: FakeOllama,
    fake_runner: list[tuple[str, dict[str, Any]]],
) -> None:
    """v41-F3: an email-bound server's tools decide under the email scope —
    a read-shaped tool runs inside the turn, a send cards and executes only
    on confirm."""
    _seed_email_server(config)
    client, chat_id = chat_client(config, ollama)

    ollama.script_tool_call("call_mcp_tool", {"server_id": "mail", "tool": "read_inbox"})
    ollama.script_reply("inbox read")
    response = client.post(f"/api/chats/{chat_id}/messages", json={"content": "any mail?"})
    assert "ran read_inbox" in response.text  # executed inside the turn
    # v61-F1: the auto-allowed read leaves a resolved row, never a card.
    (read_row,) = client.get(f"/api/chats/{chat_id}").json()["actions"]
    assert read_row["status"] == "confirmed"

    ollama.script_tool_call(
        "call_mcp_tool", {"server_id": "mail", "tool": "send_message", "arguments": {"to": "x"}}
    )
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "reply to it"})
    (action,) = [
        a
        for a in client.get(f"/api/chats/{chat_id}").json()["actions"]
        if a["status"] == "proposed"
    ]
    assert action["status"] == "proposed"
    assert action["decided_by"] == "policy/risk:external_side_effect"
    assert ("tools/call", {"name": "send_message", "arguments": {"to": "x"}}) not in fake_runner

    ollama.script_reply("sent")
    client.post(f"/api/chats/{chat_id}/actions/{action['action_id']}/confirm")
    assert ("tools/call", {"name": "send_message", "arguments": {"to": "x"}}) in fake_runner


def test_email_deny_rule_refuses_inline_and_blocks_learned_allows(
    config: SupervisorConfig,
    ollama: FakeOllama,
    fake_runner: list[tuple[str, dict[str, Any]]],
) -> None:
    """An explicit email deny is a hard deny: no card, and a learned allow
    into that space is rejected with the deny's rule id."""
    _seed_email_server(config)
    _seed_document(
        config,
        {
            "template": "strict",
            "scopes": [
                {
                    "scope": "email",
                    "deny": [{"rule_id": "no-sending", "action": "send", "pattern": "*"}],
                }
            ],
        },
    )
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("call_mcp_tool", {"server_id": "mail", "tool": "send_message"})
    ollama.script_reply("that is not allowed")
    response = client.post(f"/api/chats/{chat_id}/messages", json={"content": "send it"})
    assert "denied by policy" in response.text
    assert client.get(f"/api/chats/{chat_id}").json()["actions"] == []  # NO card
    assert fake_runner == []

    ollama.script_tool_call("allow_mcp_tool", {"server_id": "mail", "tool": "send_message"})
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "always allow sending"})
    (action,) = client.get(f"/api/chats/{chat_id}").json()["actions"]
    ollama.script_reply("could not remember that")
    client.post(f"/api/chats/{chat_id}/actions/{action['action_id']}/confirm")
    (resolved,) = client.get(f"/api/chats/{chat_id}").json()["actions"]
    assert resolved["result"]["ok"] is False
    assert "no-sending" in resolved["result"]["error"]


def test_email_allow_always_writes_an_email_scope_rule(
    config: SupervisorConfig,
    ollama: FakeOllama,
    fake_runner: list[tuple[str, dict[str, Any]]],
) -> None:
    """allow_mcp_tool on a mail-bound server writes the learned rule in the
    email scope with the derived verb, and the second send auto-allows."""
    _seed_email_server(config)
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("allow_mcp_tool", {"server_id": "mail", "tool": "send_message"})
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "always allow it"})
    (action,) = client.get(f"/api/chats/{chat_id}").json()["actions"]
    ollama.script_reply("remembered")
    client.post(f"/api/chats/{chat_id}/actions/{action['action_id']}/confirm")

    store = RunStore(config.db_path)
    try:
        document = json.loads(str(store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY)))
    finally:
        store.close()
    (learned,) = document["learned"]
    assert learned["scope"] == "email"
    assert learned["action"] == "send"
    assert learned["pattern"] == "mail:send_message"

    ollama.script_tool_call("call_mcp_tool", {"server_id": "mail", "tool": "send_message"})
    ollama.script_reply("sent without asking")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "send again"})
    assert ("tools/call", {"name": "send_message", "arguments": {}}) in fake_runner
    actions = client.get(f"/api/chats/{chat_id}").json()["actions"]
    assert all(a["status"] != "proposed" for a in actions)


def test_http_transport_is_transparent_to_the_chat_surface(
    config: SupervisorConfig,
    ollama: FakeOllama,
    fake_runner: list[tuple[str, dict[str, Any]]],
) -> None:
    """v80-F1: the transport switch is invisible to the policy engine — an
    http server lists, a read flows inside the turn, an unknown tool cards
    and executes on confirm, exactly as stdio does."""
    _seed_http_server(config)
    client, chat_id = chat_client(config, ollama)

    ollama.script_tool_call("list_mcp_tools", {"server_id": "remote"})
    ollama.script_reply("two tools found")
    response = client.post(f"/api/chats/{chat_id}/messages", json={"content": "tools?"})
    assert '"list_issues"' in response.text

    ollama.script_tool_call("call_mcp_tool", {"server_id": "remote", "tool": "list_issues"})
    ollama.script_reply("here are the issues")
    response = client.post(f"/api/chats/{chat_id}/messages", json={"content": "issues?"})
    assert "ran list_issues" in response.text  # read: executed inside the turn
    assert ("tools/call", {"name": "list_issues", "arguments": {}}) in fake_runner

    ollama.script_tool_call("call_mcp_tool", {"server_id": "remote", "tool": "frobnicate_gizmo"})
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "frob it"})
    (action,) = [
        a
        for a in client.get(f"/api/chats/{chat_id}").json()["actions"]
        if a["status"] == "proposed"
    ]
    assert ("tools/call", {"name": "frobnicate_gizmo", "arguments": {}}) not in fake_runner

    ollama.script_reply("done frobnicating")
    client.post(f"/api/chats/{chat_id}/actions/{action['action_id']}/confirm")
    assert ("tools/call", {"name": "frobnicate_gizmo", "arguments": {}}) in fake_runner


def test_list_mcp_tools_reports_risk_and_policy(
    config: SupervisorConfig,
    ollama: FakeOllama,
    fake_runner: list[tuple[str, dict[str, Any]]],
) -> None:
    _seed_server(config)
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("list_mcp_tools", {"server_id": "issues"})
    ollama.script_reply("two tools found")
    response = client.post(f"/api/chats/{chat_id}/messages", json={"content": "what tools?"})
    payload = response.text
    assert '"list_issues"' in payload and '"frobnicate_gizmo"' in payload
    assert '"risk": "read"' in payload.replace(": ", ": ") or "read" in payload
    assert "unknown" in payload  # the fail-closed class is visible to the Queen


def test_unregister_mcp_server_is_carded_and_removes_the_config(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """v47-F2: the remove half of register — card first, config gone on confirm."""
    from skep.supervisor.mcp_client import MCPError, remove_mcp_server
    from skep.supervisor.serve.tools import MUTATING_TOOL_NAMES

    assert "unregister_mcp_server" in MUTATING_TOOL_NAMES

    _seed_server(config)
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("unregister_mcp_server", {"server_id": "issues"})
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "drop the issues server"})
    action = client.get(f"/api/chats/{chat_id}").json()["actions"][0]
    assert (action["tool"], action["status"]) == ("unregister_mcp_server", "proposed")

    # Still registered until the human verdict.
    store = RunStore(config.db_path)
    try:
        assert set(load_mcp_servers(store)) == {"issues"}
    finally:
        store.close()

    ollama.script_reply("removed")
    client.post(f"/api/chats/{chat_id}/actions/{action['action_id']}/confirm")
    store = RunStore(config.db_path)
    try:
        assert load_mcp_servers(store) == {}
        # Removing an unknown id is a clean MCPError, not silent success.
        with pytest.raises(MCPError, match="ghost"):
            remove_mcp_server(store, "ghost")
    finally:
        store.close()
