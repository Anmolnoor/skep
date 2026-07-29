"""v71-F2: the browse scope — the browser arrives as an MCP server.

Page-state reads flow; every page act — navigation included (read_url
parity: each fetch is one operator decision) — cards until an allow rule is
learned; explicit deny rules refuse without a card. The generic keyword
ladder must never touch browse tools (every browser_* name contains
"browse" and would misclassify as network risk).
"""

from __future__ import annotations

import json
from pathlib import Path

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.mcp_client import (
    MCPServerConfig,
    MCPTool,
    classify_browse_action,
    mcp_scope_decision,
    mcp_tool_scope_action,
    save_mcp_server,
)
from skep.supervisor.policy_schema import POLICY_DOCUMENT_SETTINGS_KEY
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import execute_mutation


def test_classify_browse_action_read_vs_act() -> None:
    reads = (
        "browser_snapshot",
        "browser_take_screenshot",
        "browser_console_messages",
        "browser_network_requests",
        "browser_wait_for",
    )
    acts = (
        "browser_navigate",
        "browser_click",
        "browser_type",
        "browser_fill_form",
        "browser_press_key",
        "browser_select_option",
        "browser_evaluate",
        "browser_tabs",  # can create/close tabs — fail closed
        "browser_file_upload",
        "utterly_unknown",
    )
    for name in reads:
        assert classify_browse_action(name) == "read", name
    for name in acts:
        assert classify_browse_action(name) == "act", name


def _seed_browse_server(config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        save_mcp_server(
            store,
            MCPServerConfig(
                server_id="playwright",
                transport="stdio",
                command=("npx", "@playwright/mcp@latest"),
                scope="browse",
            ),
        )
    finally:
        store.close()


def _tool(name: str) -> MCPTool:
    return MCPTool(server_id="playwright", name=name, description="")


def test_browse_reads_flow_and_acts_card(config: SupervisorConfig) -> None:
    _seed_browse_server(config)
    store = RunStore(config.db_path)
    try:
        assert mcp_tool_scope_action(store, _tool("browser_snapshot")) == ("browse", "read")
        assert mcp_tool_scope_action(store, _tool("browser_navigate")) == ("browse", "act")

        snapshot = mcp_scope_decision(store, _tool("browser_snapshot"))
        assert snapshot.verdict == "allow"
        assert snapshot.reason == "browse.allow.read_risk"

        # Every act cards — navigation included, and the generic ladder's
        # "browse ⊂ browser_navigate → network risk" path must not fire.
        navigate = mcp_scope_decision(store, _tool("browser_navigate"))
        assert navigate.verdict == "require_approval"
        assert navigate.reason == "browse.require_approval.page_action"
        click = mcp_scope_decision(store, _tool("browser_click"))
        assert click.verdict == "require_approval"
    finally:
        store.close()


def test_browse_deny_rule_refuses_and_allow_rule_admits(
    config: SupervisorConfig,
) -> None:
    _seed_browse_server(config)
    store = RunStore(config.db_path)
    try:
        store.set_setting(
            POLICY_DOCUMENT_SETTINGS_KEY,
            json.dumps(
                {
                    "template": "strict",
                    "scopes": [
                        {
                            "scope": "browse",
                            "deny": [
                                {
                                    "rule_id": "no-js",
                                    "action": "act",
                                    "pattern": "playwright:browser_evaluate",
                                }
                            ],
                            "allow": [
                                {
                                    "rule_id": "free-navigation",
                                    "action": "act",
                                    "pattern": "playwright:browser_navigate",
                                }
                            ],
                        }
                    ],
                }
            ),
        )
        assert mcp_scope_decision(store, _tool("browser_evaluate")).verdict == "deny"
        assert mcp_scope_decision(store, _tool("browser_navigate")).verdict == "allow"
        # An unmatched act still cards — the rule admitted navigation only.
        assert mcp_scope_decision(store, _tool("browser_click")).verdict == "require_approval"
    finally:
        store.close()


def test_allow_mcp_tool_learns_a_browse_scope_rule(config: SupervisorConfig) -> None:
    _seed_browse_server(config)
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        result = execute_mutation(
            "allow_mcp_tool",
            {"server_id": "playwright", "tool": "browser_navigate"},
            store=store,
            holder=holder,
            runner=None,  # type: ignore[arg-type]  # allow_mcp_tool never dispatches
            actor="test",
        )
        assert result["scope"] == "browse"
        assert result["rule_id"] == "browse:playwright:browser_navigate"
        assert mcp_scope_decision(store, _tool("browser_navigate")).verdict == "allow"
    finally:
        store.close()


def test_register_mcp_server_accepts_scope_browse(config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        result = execute_mutation(
            "register_mcp_server",
            {
                "server_id": "playwright",
                "transport": "stdio",
                "command": ["npx", "@playwright/mcp@latest"],
                "scope": "browse",
            },
            store=store,
            holder=holder,
            runner=None,  # type: ignore[arg-type]  # registration never dispatches
            actor="test",
        )
        assert result["scope"] == "browse"
    finally:
        store.close()


def test_doctor_warns_when_a_browse_launcher_is_missing(tmp_path: Path) -> None:
    from skep.status import _browse_advisories

    home = tmp_path / "skep-home"
    db_path = home / "supervisor" / "supervisor.sqlite3"
    db_path.parent.mkdir(parents=True)
    store = RunStore(db_path)
    try:
        save_mcp_server(
            store,
            MCPServerConfig(
                server_id="playwright",
                transport="stdio",
                command=("definitely-not-on-path-xyz",),
                scope="browse",
            ),
        )
    finally:
        store.close()
    (advisory,) = _browse_advisories(home)
    assert "playwright" in advisory and "not on PATH" in advisory

    # A launcher that exists (sh) raises no advisory.
    store = RunStore(db_path)
    try:
        save_mcp_server(
            store,
            MCPServerConfig(
                server_id="playwright", transport="stdio", command=("sh",), scope="browse"
            ),
        )
    finally:
        store.close()
    assert _browse_advisories(home) == []


def test_list_mcp_tools_labels_browse_tools_by_browse_action(
    config: SupervisorConfig, monkeypatch: object
) -> None:
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    _seed_browse_server(config)

    def runner(method: str, params: object) -> dict[str, object]:
        assert method == "tools/list"
        return {
            "tools": [
                {"name": "browser_snapshot", "description": "read the page"},
                {"name": "browser_navigate", "description": "go to a url"},
            ]
        }

    monkeypatch.setattr(
        "skep.supervisor.mcp_client.runner_for_config", lambda config: runner
    )
    store = RunStore(config.db_path)
    try:
        from skep.supervisor.serve.tools import execute_read_tool

        holder = ConfigHolder(config, store)
        view = execute_read_tool(
            "list_mcp_tools", {"server_id": "playwright"}, store=store, holder=holder
        )
        by_name = {row["name"]: row for row in view["tools"]}
        assert by_name["browser_snapshot"]["risk"] == "read"
        assert by_name["browser_snapshot"]["policy"] == "allow"
        assert by_name["browser_navigate"]["risk"] == "act"  # never "network"
        assert by_name["browser_navigate"]["policy"] == "require_approval"
    finally:
        store.close()
