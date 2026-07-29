"""v17 Step 3: MCP tools as policy-gated capabilities (risk + v19-F1 batch gate)."""

from __future__ import annotations

from skep.supervisor.mcp_client import (
    MCPGrants,
    MCPTool,
    classify_mcp_risk,
    mcp_capability_decision,
    mcp_plan_preflight,
)


def _tool(name: str, server: str = "srv") -> MCPTool:
    return MCPTool(server_id=server, name=name, description="")


def test_risk_classification() -> None:
    assert classify_mcp_risk(_tool("read_file")) == "read"
    assert classify_mcp_risk(_tool("list_issues")) == "read"
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
