"""v17 Step 2: an MCP (Model Context Protocol) client skeleton.

Discovers the tools an MCP server offers and captures their JSON schemas, then
calls them — all through an *injected runner* (a callable that speaks the MCP
JSON-RPC transport), so the client is hermetic and unit-testable without a live
server. Two transports run here (stdio + Streamable HTTP); the runner is what
actually moves bytes.

MCP tools do NOT get to run freely: Step 3 maps each discovered tool to a
policy-gated ``mcp.<server>.<tool>`` capability with a risk classification. This
module only discovers and calls; authorization lives in the capability engine.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .autonomy import AutonomyDecision
    from .policy_schema import Scope
    from .store import RunStore

MCP_TRANSPORTS: frozenset[str] = frozenset({"stdio", "http"})
# The policy scopes a server may bind its tools to (v41-F3): plain servers
# decide under ``mcp``; an email-bound server's tools decide under ``email``;
# a browser server (e.g. @playwright/mcp) binds ``browse`` (v71-F2).
MCP_SERVER_SCOPES: frozenset[str] = frozenset({"mcp", "email", "browse"})

# The MCP JSON-RPC method a runner receives, and returns a decoded result dict.
MCPRunner = Callable[[str, Mapping[str, object]], Mapping[str, object]]


class MCPError(Exception):
    """An MCP transport or protocol error."""


@dataclass(frozen=True)
class MCPServerConfig:
    server_id: str
    transport: str
    command: tuple[str, ...] = ()  # stdio: argv
    url: str | None = None  # http/sse: endpoint
    scope: str = "mcp"  # v41-F3: the policy scope this server's tools decide under

    def validate(self) -> None:
        if self.transport not in MCP_TRANSPORTS:
            raise MCPError(
                f"transport must be one of {sorted(MCP_TRANSPORTS)!r}, got {self.transport!r}"
            )
        if self.transport == "stdio" and not self.command:
            raise MCPError("stdio MCP server requires a command")
        if self.transport == "http" and not self.url:
            raise MCPError("http MCP server requires a url")
        if self.scope not in MCP_SERVER_SCOPES:
            raise MCPError(
                f"scope must be one of {sorted(MCP_SERVER_SCOPES)!r}, got {self.scope!r}"
            )


@dataclass(frozen=True)
class MCPTool:
    server_id: str
    name: str
    description: str
    input_schema: dict[str, object] = field(default_factory=dict)

    @property
    def capability_id(self) -> str:
        return f"mcp.{self.server_id}.{self.name}"


@dataclass(frozen=True)
class MCPCallResult:
    ok: bool
    content: object = None
    error: str | None = None


# Keyword heuristics for classifying an MCP tool's risk (highest wins). A tool
# the heuristic can't place is "unknown" — which FAILS CLOSED (v39-F1): it
# needs an approval like any risky class, because auto-allowing a name we
# could not even classify is how a live MCP server would slip a side effect
# past the gate. Only read-shaped names keep the v17 auto-allow ergonomic.
_MCP_EXTERNAL_KEYWORDS = ("send", "post", "email", "notify", "publish", "deploy", "sms", "exec")
_MCP_NETWORK_KEYWORDS = ("http", "url", "fetch", "download", "browse", "web", "request")
_MCP_WRITE_KEYWORDS = (
    "write", "create", "update", "delete", "remove", "edit", "modify", "set", "put",
)
_MCP_READ_KEYWORDS = ("read", "list", "get", "search", "query", "describe", "status", "show")

# v71-F2: a browse-bound server's page-STATE reads. Deliberately tight — a
# name matching none of these is an act (fail closed): navigation is an
# outbound fetch (read_url parity: every fetch is one operator decision),
# clicks/typing/evaluate change the page, and browser_tabs mutates tabs.
_BROWSE_READ_KEYWORDS = ("snapshot", "screenshot", "console", "network_request", "wait_for")


def classify_browse_action(tool_name: str) -> str:
    """``read`` for page-state reads, ``act`` for everything else."""
    name = tool_name.lower()
    return "read" if any(keyword in name for keyword in _BROWSE_READ_KEYWORDS) else "act"


def classify_mcp_risk(tool: MCPTool) -> str:
    """Classify an MCP tool into a capability risk class from its name.

    Priority external_side_effect > network > write > read > unknown. An
    unknown tool requires approval (fail closed); a project may grant the
    ``unknown`` risk explicitly — a visible policy act, never a default.
    """
    name = tool.name.lower()
    if any(keyword in name for keyword in _MCP_EXTERNAL_KEYWORDS):
        return "external_side_effect"
    if any(keyword in name for keyword in _MCP_NETWORK_KEYWORDS):
        return "network"
    if any(keyword in name for keyword in _MCP_WRITE_KEYWORDS):
        return "write"
    if any(keyword in name for keyword in _MCP_READ_KEYWORDS):
        return "read"
    return "unknown"


# -- v17 Step 3: MCP tools as policy-gated capabilities ----------------------


@dataclass(frozen=True)
class MCPGrants:
    allowed_servers: tuple[str, ...] = ()
    denied_servers: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()  # capability ids explicitly allowed
    allowed_risks: tuple[str, ...] = ()  # risk classes the project grants


@dataclass(frozen=True)
class MCPCapabilityDecision:
    verdict: str  # allow | allow_with_constraints | require_approval | deny
    reason: str
    capability_id: str
    risk: str


def mcp_capability_decision(tool: MCPTool, grants: MCPGrants) -> MCPCapabilityDecision:
    """Decide one MCP tool call the same way plugin tools are gated: a project can
    deny/allow servers and tools, a read tool is auto-allowed, riskier classes
    need a grant, and an external side effect always needs approval."""
    risk = classify_mcp_risk(tool)
    cap = tool.capability_id

    def decide(verdict: str, reason: str) -> MCPCapabilityDecision:
        return MCPCapabilityDecision(verdict=verdict, reason=reason, capability_id=cap, risk=risk)

    if tool.server_id in grants.denied_servers:
        return decide("deny", "mcp.deny.server_denied")
    if grants.allowed_servers and tool.server_id not in grants.allowed_servers:
        return decide("deny", "mcp.deny.server_not_allowed")
    if cap in grants.allowed_tools:
        return decide("allow_with_constraints", "mcp.allow.tool_allowlisted")
    if risk == "read":
        return decide("allow", "mcp.allow.read_risk")
    if risk in grants.allowed_risks:
        return decide("allow_with_constraints", "mcp.allow.risk_task_permission")
    if risk == "external_side_effect":
        return decide("require_approval", "mcp.require_approval.external_side_effect")
    return decide("require_approval", "mcp.require_approval.risk_not_allowed")


@dataclass(frozen=True)
class MCPPlanGate:
    decisions: tuple[MCPCapabilityDecision, ...]
    denied: tuple[str, ...]
    required_approvals: tuple[str, ...]

    @property
    def needs_gate(self) -> bool:
        return bool(self.required_approvals)


def mcp_plan_preflight(tools: list[MCPTool], grants: MCPGrants) -> MCPPlanGate:
    """v19-F1 batch: pre-flight ALL the MCP steps in a plan and gate ONCE with the
    full tool list — never one gate per call. A denied tool blocks the plan."""
    decisions = tuple(mcp_capability_decision(tool, grants) for tool in tools)
    denied = tuple(d.capability_id for d in decisions if d.verdict == "deny")
    required = tuple(d.capability_id for d in decisions if d.verdict == "require_approval")
    return MCPPlanGate(decisions=decisions, denied=denied, required_approvals=required)


class MCPClient:
    def __init__(self, config: MCPServerConfig, *, runner: MCPRunner) -> None:
        config.validate()
        self._config = config
        self._runner = runner

    @property
    def server_id(self) -> str:
        return self._config.server_id

    def list_tools(self) -> list[MCPTool]:
        """Discover the server's tools and capture their input schemas."""
        try:
            response = self._runner("tools/list", {})
        except Exception as exc:  # a transport failure is not a crash of the caller
            raise MCPError(str(exc) or exc.__class__.__name__) from exc
        raw_tools = response.get("tools")
        if not isinstance(raw_tools, list):
            raise MCPError("tools/list response missing 'tools' array")
        tools: list[MCPTool] = []
        for entry in raw_tools:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "")
            if not name:
                continue
            schema = entry.get("inputSchema")
            tools.append(
                MCPTool(
                    server_id=self._config.server_id,
                    name=name,
                    description=str(entry.get("description") or ""),
                    input_schema=dict(schema) if isinstance(schema, dict) else {},
                )
            )
        return tools

    def call_tool(self, name: str, arguments: Mapping[str, object]) -> MCPCallResult:
        try:
            response = self._runner(
                "tools/call", {"name": name, "arguments": dict(arguments)}
            )
        except Exception as exc:
            return MCPCallResult(False, error=str(exc) or exc.__class__.__name__)
        if response.get("isError"):
            return MCPCallResult(False, error=str(response.get("content") or "tool error"))
        return MCPCallResult(True, content=response.get("content"))
# -- v40-F10 (v36-F6): the engine goes live ---------------------------------

# Registered servers persist as a settings key (JSON-in-settings, the house
# storage pattern — no new table).
MCP_SERVERS_SETTINGS_KEY = "mcp_servers"


def load_mcp_servers(store: RunStore) -> dict[str, MCPServerConfig]:
    import json

    raw = store.get_setting(MCP_SERVERS_SETTINGS_KEY)
    if not raw:
        return {}
    entries = json.loads(raw) if isinstance(raw, str) else raw
    servers: dict[str, MCPServerConfig] = {}
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            config = MCPServerConfig(
                server_id=str(entry.get("server_id") or ""),
                transport=str(entry.get("transport") or "stdio"),
                command=tuple(str(part) for part in entry.get("command") or ()),
                url=None if entry.get("url") is None else str(entry.get("url")),
                scope=str(entry.get("scope") or "mcp"),
            )
            if config.server_id:
                servers[config.server_id] = config
    return servers


def _store_servers(store: RunStore, servers: dict[str, MCPServerConfig]) -> None:
    import json

    store.set_setting(
        MCP_SERVERS_SETTINGS_KEY,
        json.dumps(
            [
                {
                    "server_id": entry.server_id,
                    "transport": entry.transport,
                    "command": list(entry.command),
                    "url": entry.url,
                    "scope": entry.scope,
                }
                for entry in servers.values()
            ],
            ensure_ascii=True,
        ),
    )


def save_mcp_server(store: RunStore, config: MCPServerConfig) -> None:
    config.validate()
    servers = load_mcp_servers(store)
    servers[config.server_id] = config
    _store_servers(store, servers)


def remove_mcp_server(store: RunStore, server_id: str) -> None:
    """v47-F2: the unregister half of save_mcp_server. Learned allow rules for
    the server's scope are left in place (re-registering the id reuses them)."""
    servers = load_mcp_servers(store)
    if server_id not in servers:
        raise MCPError(f"no MCP server registered as {server_id!r}")
    del servers[server_id]
    _store_servers(store, servers)


def _jsonrpc_result(message: Mapping[str, object]) -> Mapping[str, object]:
    if "error" in message:
        raise MCPError(str(message["error"]))
    result = message.get("result")
    return result if isinstance(result, Mapping) else {}


def runner_for_config(config: MCPServerConfig) -> MCPRunner:
    """A one-shot JSON-RPC runner per transport (tests monkeypatch this seam).

    ponytail: one-shot per call (initialize -> request -> done) — a persistent
    session pool is the upgrade path if call volume matters.
    """
    import json
    import subprocess

    if config.transport == "http":
        # MCP Streamable HTTP (2025-03-26): JSON-RPC POSTs to one endpoint;
        # the response body is JSON or an SSE stream. The legacy 2024-11-05
        # HTTP+SSE flow (GET /sse + endpoint event) is not supported — its
        # 202-empty POST responses surface below as an honest MCPError.
        import httpx

        url = config.url or ""

        def run_http(method: str, params: Mapping[str, object]) -> Mapping[str, object]:
            # The spec requires both accept types; official SDK servers 406 without.
            headers = {"Accept": "application/json, text/event-stream"}
            try:
                with httpx.Client(timeout=30) as client:
                    with client.stream(
                        "POST",
                        url,
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {
                                "protocolVersion": "2025-03-26",
                                "capabilities": {},
                                "clientInfo": {"name": "skep", "version": "1"},
                            },
                        },
                        headers=headers,
                    ) as init:
                        if init.status_code >= 300:
                            body = init.read().decode("utf-8", "replace")
                            raise MCPError(
                                f"http MCP initialize failed: {init.status_code} {body[:200]}"
                            )
                        session_id = init.headers.get("mcp-session-id")
                    if session_id:
                        # Stateful servers reject session-less requests.
                        headers["Mcp-Session-Id"] = session_id
                    # Fire-and-forget: strict servers want it before tools/*;
                    # one that rejects it still answers (or honestly fails)
                    # the method POST below.
                    client.post(
                        url,
                        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                        headers=headers,
                    )
                    with client.stream(
                        "POST",
                        url,
                        json={
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": method,
                            "params": dict(params),
                        },
                        headers=headers,
                    ) as response:
                        if response.status_code >= 300:
                            body = response.read().decode("utf-8", "replace")
                            raise MCPError(
                                f"http MCP call failed: {response.status_code} {body[:200]}"
                            )
                        if "text/event-stream" in response.headers.get("content-type", ""):
                            # Parse incrementally and stop at our response — a
                            # keep-alive stream must not run out the clock.
                            for line in response.iter_lines():
                                if not line.startswith("data:"):
                                    continue
                                try:
                                    message = json.loads(line[5:].strip())
                                except json.JSONDecodeError:
                                    continue
                                if isinstance(message, dict) and message.get("id") == 2:
                                    return _jsonrpc_result(message)
                        else:
                            message = json.loads(response.read() or b"null")
                            if isinstance(message, dict) and message.get("id") == 2:
                                return _jsonrpc_result(message)
                        raise MCPError("no JSON-RPC response for the tool request")
            except MCPError:
                raise
            except json.JSONDecodeError as exc:
                raise MCPError(f"http MCP response was not JSON-RPC: {exc}") from exc
            except httpx.HTTPError as exc:
                raise MCPError(
                    f"http MCP transport error: {exc.__class__.__name__}: {exc}"
                ) from exc

        return run_http

    if config.transport != "stdio":
        raise MCPError(f"unsupported MCP transport {config.transport!r}")

    def run(method: str, params: Mapping[str, object]) -> Mapping[str, object]:
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
            },
            {"jsonrpc": "2.0", "id": 2, "method": method, "params": dict(params)},
        ]
        payload = "".join(json.dumps(r) + "\n" for r in requests)
        proc = subprocess.run(
            list(config.command),
            input=payload,
            capture_output=True,
            text=True,
            timeout=30,
        )
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == 2:
                return _jsonrpc_result(message)
        raise MCPError("no JSON-RPC response for the tool request")

    return run


def mcp_tool_scope_action(store: RunStore, tool: MCPTool) -> tuple[Scope, str]:
    """The (scope, action) one MCP tool call decides under (v41-F3).

    A plain server's tools decide as ``("mcp", "call")``. An email-bound
    server's tools decide under ``email``: read-shaped names are a ``read``,
    everything else — including ``unknown`` — is a ``send`` (the higher verb;
    fail closed for names we cannot even classify).
    """
    server = load_mcp_servers(store).get(tool.server_id)
    scope = server.scope if server is not None else "mcp"
    if scope == "email":
        return "email", ("read" if classify_mcp_risk(tool) == "read" else "send")
    if scope == "browse":
        return "browse", classify_browse_action(tool.name)
    return "mcp", "call"


def mcp_scope_decision(store: RunStore, tool: MCPTool) -> AutonomyDecision:
    """Decide one MCP tool call against its resolved scope (``mcp``, or
    ``email`` for an email-bound server — v41-F3).

    Explicit scope rules win (a deny is a hard deny — no card); an unmatched
    tool falls back to the risk ladder: read auto-allows (the v17 ergonomic),
    everything else — including ``unknown`` (v39-F1 fail-closed) — cards.
    """
    from .autonomy import AutonomyDecision
    from .policy_schema import (
        DEFAULT_DENY_RULE_ID,
        POLICY_DOCUMENT_SETTINGS_KEY,
        PolicyDocument,
        decide,
        document_from_settings,
        resolve,
    )

    raw = store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY)
    document = document_from_settings(raw) or PolicyDocument()
    resolved = resolve(document)
    scope, action = mcp_tool_scope_action(store, tool)
    value = f"{tool.server_id}:{tool.name}"
    decision = decide(resolved, scope, action, value, template=document.template)
    if decision.rule_id != DEFAULT_DENY_RULE_ID:
        verdict = decision.verdict
        return AutonomyDecision(
            verdict="allow" if verdict == "allow" else verdict,
            reason=f"{scope}.{verdict}.scope_rule",
            detail=value,
            decided_by=decision.decided_by,
        )
    if scope == "browse":
        # v71-F2: the generic keyword ladder would misread browser_* names
        # (every one contains "browse" → network risk). Page-state reads flow;
        # every page ACT — navigation included (read_url parity: each fetch is
        # one operator decision) — cards until an allow rule is learned.
        if action == "read":
            return AutonomyDecision(
                verdict="allow",
                reason="browse.allow.read_risk",
                detail=value,
                decided_by=f"{document.template or 'policy'}/browse:read",
            )
        return AutonomyDecision(
            verdict="require_approval",
            reason="browse.require_approval.page_action",
            detail=value,
            decided_by=f"{document.template or 'policy'}/browse:act",
        )
    risk = classify_mcp_risk(tool)
    if risk == "read":
        return AutonomyDecision(
            verdict="allow",
            reason=f"{scope}.allow.read_risk",
            detail=value,
            decided_by=f"{document.template or 'policy'}/risk:read",
        )
    return AutonomyDecision(
        verdict="require_approval",
        reason=(
            f"{scope}.require_approval.external_side_effect"
            if risk == "external_side_effect"
            else f"{scope}.require_approval.risk_not_allowed"
        ),
        detail=value,
        decided_by=f"{document.template or 'policy'}/risk:{risk}",
    )
