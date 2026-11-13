"""v17 Step 3: MCP tools as policy-gated capabilities (risk + v19-F1 batch gate)."""

from __future__ import annotations

from pathlib import Path

import pytest

from skep.supervisor.mcp_client import (
    MCPGrants,
    MCPTool,
    classify_mcp_risk,
    mcp_capability_decision,
    mcp_plan_preflight,
)


def _tool(name: str, server: str = "srv", *, read_only: bool | None = True) -> MCPTool:
    return MCPTool(server_id=server, name=name, description="", read_only=read_only)


def test_risk_classification() -> None:
    assert classify_mcp_risk(_tool("read_file")) == "read"
    assert classify_mcp_risk(_tool("list_issues")) == "read"
    assert classify_mcp_risk(_tool("getUser")) == "read"  # camelCase leading verb
    assert classify_mcp_risk(_tool("create_issue")) == "write"
    assert classify_mcp_risk(_tool("fetch_url")) == "network"
    assert classify_mcp_risk(_tool("send_email")) == "external_side_effect"
    # v39-F1: a name the heuristic cannot place is NOT read — it fails closed.
    assert classify_mcp_risk(_tool("frobnicate_gizmo")) == "unknown"


def test_unknown_tool_requires_approval_never_auto_allows() -> None:
    """v39-F1 (the v36-F6 kernel): an unclassifiable tool cannot ride the
    read auto-allow. It needs an approval, or an explicit 'unknown' grant."""
    decision = mcp_capability_decision(_tool("frobnicate_gizmo"), MCPGrants())
    assert decision.verdict == "require_approval"
    assert decision.reason == "mcp.require_approval.risk_not_allowed"
    assert decision.risk == "unknown"

    # Granting the risk is a visible policy act — then, and only then, it runs.
    granted = mcp_capability_decision(
        _tool("frobnicate_gizmo"), MCPGrants(allowed_risks=("unknown",))
    )
    assert granted.verdict == "allow_with_constraints"

    gate = mcp_plan_preflight([_tool("frobnicate_gizmo")], MCPGrants())
    assert gate.needs_gate is True
    assert gate.required_approvals == ("mcp.srv.frobnicate_gizmo",)


def test_read_tool_is_auto_allowed() -> None:
    decision = mcp_capability_decision(_tool("read_file"), MCPGrants())
    assert decision.verdict == "allow"
    assert decision.reason == "mcp.allow.read_risk"


def test_write_needs_grant_external_needs_approval() -> None:
    write = mcp_capability_decision(_tool("create_issue"), MCPGrants())
    assert write.verdict == "require_approval"
    granted = mcp_capability_decision(_tool("create_issue"), MCPGrants(allowed_risks=("write",)))
    assert granted.verdict == "allow_with_constraints"

    external = mcp_capability_decision(
        _tool("send_email"), MCPGrants(allowed_risks=("external_side_effect",))
    )
    # An external side effect always needs approval — a risk grant is not enough.
    # (allowed_risks admits it, so it is allow_with_constraints only when granted.)
    assert external.verdict == "allow_with_constraints"
    ungranted = mcp_capability_decision(_tool("send_email"), MCPGrants())
    assert ungranted.reason == "mcp.require_approval.external_side_effect"


def test_project_can_deny_or_allowlist_servers_and_tools() -> None:
    denied = mcp_capability_decision(
        _tool("read_file", server="bad"), MCPGrants(denied_servers=("bad",))
    )
    assert denied.verdict == "deny" and denied.reason == "mcp.deny.server_denied"

    not_allowed = mcp_capability_decision(
        _tool("read_file", server="other"), MCPGrants(allowed_servers=("srv",))
    )
    assert not_allowed.reason == "mcp.deny.server_not_allowed"

    tool_allowed = mcp_capability_decision(
        _tool("create_issue"), MCPGrants(allowed_tools=("mcp.srv.create_issue",))
    )
    assert tool_allowed.verdict == "allow_with_constraints"


def test_plan_preflight_gates_once_per_plan_not_per_call() -> None:
    tools = [_tool("read_file"), _tool("create_issue"), _tool("update_file"), _tool("send_email")]
    gate = mcp_plan_preflight(tools, MCPGrants())
    # read is auto-allowed; the other three need approval -> ONE gate, three in list.
    assert gate.needs_gate is True
    assert gate.required_approvals == (
        "mcp.srv.create_issue",
        "mcp.srv.update_file",
        "mcp.srv.send_email",
    )
    assert gate.denied == ()


def test_plan_preflight_blocks_on_denied_tool() -> None:
    gate = mcp_plan_preflight(
        [_tool("read_file", server="bad")], MCPGrants(denied_servers=("bad",))
    )
    assert gate.denied == ("mcp.bad.read_file",)


def test_b09_destructive_names_never_classify_as_read() -> None:
    """ES-F (B09, P0): risk came from a substring of the name — tar*get*,
    bud*get*, for*get*, wid*get* all read as "get" and auto-ran on the host.
    Even a server that marks them read-only cannot make them reads: the
    LEADING verb must be read-shaped too."""
    for name in (
        "budget_transfer",
        "target_account_purge",
        "widget_destroy",
        "forget_all",
        "purge_target",
        "run_query",  # a query runner is not a read, whatever it claims
        "account_status_reset",
        "lookup_contacts",
    ):
        assert classify_mcp_risk(_tool(name)) != "read", name
        assert mcp_capability_decision(_tool(name), MCPGrants()).verdict != "allow", name


def test_b09_read_verb_without_the_server_hint_is_not_a_read() -> None:
    """A read-shaped name alone is not enough: the server must mark the tool
    readOnlyHint, and an unannotated (or unknown) tool asks."""
    for read_only in (False, None):
        tool = _tool("list_users", read_only=read_only)
        assert classify_mcp_risk(tool) != "read"
        assert mcp_capability_decision(tool, MCPGrants()).verdict == "require_approval"


def test_b09_list_tools_reads_the_read_only_annotation() -> None:
    from skep.supervisor.mcp_client import MCPClient, MCPServerConfig

    def runner(method: str, params: object) -> dict[str, object]:
        return {
            "tools": [
                {"name": "list_users", "annotations": {"readOnlyHint": True}},
                {"name": "list_groups", "annotations": {"readOnlyHint": "yes"}},
                {"name": "get_x"},
            ]
        }

    config = MCPServerConfig(server_id="srv", transport="stdio", command=("x",))
    tools = MCPClient(config, runner=runner).list_tools()
    assert [t.read_only for t in tools] == [True, False, False]


def test_b09_unreachable_server_cannot_vouch_for_a_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import RunStore
    from skep.supervisor.mcp_client import MCPServerConfig, mcp_scope_decision, save_mcp_server

    def broken(config: object, **_: object) -> object:
        def runner(method: str, params: object) -> object:
            raise OSError("server down")

        return runner

    from skep.supervisor import SupervisorConfig
    from skep.supervisor.serve.settings import ConfigHolder

    from .conftest import predating_home

    monkeypatch.setattr("skep.supervisor.mcp_client.runner_for_config", broken)
    store = RunStore(predating_home(tmp_path / "s.sqlite3"))
    holder = ConfigHolder(SupervisorConfig(home=tmp_path, worker_command=("x",)), store)
    try:
        save_mcp_server(store, MCPServerConfig(server_id="srv", transport="stdio", command=("x",)))
        decision = mcp_scope_decision(store, holder, _tool("list_users", read_only=None))
        assert (decision.verdict, decision.decided_by) == ("require_approval", "default/unmatched")
    finally:
        store.close()


def test_w10_a_mutation_verb_anywhere_in_the_name_is_never_a_read() -> None:
    """W10 (PR #70): `get_and_transfer` was a read — the server set
    readOnlyHint and the name led with `get`. A mutation verb anywhere in the
    name makes the tool not-read whatever the hint says (more calls card)."""
    for name in ("get_and_transfer", "list_then_delete", "read_and_send", "getAndPay",
                 "search_purge", "show_submit", "find_update", "get_post_and_create"):  # fmt: skip
        assert classify_mcp_risk(_tool(name, read_only=True)) != "read", name
    for name in ("get_user", "list_issues", "read_file", "search_docs", "get_settings"):
        assert classify_mcp_risk(_tool(name, read_only=True)) == "read", name
