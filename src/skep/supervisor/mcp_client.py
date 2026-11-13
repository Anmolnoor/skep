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
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import subprocess
    from pathlib import Path

    from .autonomy import AutonomyDecision
    from .policy_schema import Scope
    from .roles import Role
    from .serve.settings import ConfigHolder
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
    # ES (B09): the server's ``annotations.readOnlyHint`` — True only when the
    # server says so; None = not looked up yet (a bare name from a call).
    read_only: bool | None = None

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
# needs an approval like any risky class.
#
# ES (B09, P0): keywords match whole name TOKENS, never substrings — "get"
# inside tar*get*/bud*get*/for*get* once made `target_account_purge` a read
# that ran on the host with no card. And "read" is affirmative: the server
# must mark the tool ``readOnlyHint`` AND its LEADING verb must be a read
# verb. `query`/`fetch` are deliberately not read verbs (`run_query`, a fetch
# egresses); anything else asks until the operator runs allow_mcp_tool.
_MCP_EXTERNAL_KEYWORDS = ("send", "post", "email", "notify", "publish", "deploy", "sms", "exec")
_MCP_NETWORK_KEYWORDS = ("http", "url", "fetch", "download", "browse", "web", "request")
_MCP_WRITE_KEYWORDS = (
    "write",
    "create",
    "update",
    "delete",
    "remove",
    "edit",
    "modify",
    "set",
    "put",
)
_MCP_READ_VERBS = ("get", "list", "read", "search", "describe", "show", "find")
# W10: a verb that changes or sends something. Anywhere in a name it makes the
# tool not-read whatever the server's hint says (`get_and_transfer`). Nouns
# that double as verbs (`run`, `order`, `email`) are left out: `get_run` reads.
_MCP_MUTATION_VERBS = frozenset(
    (*_MCP_WRITE_KEYWORDS, "send", "post", "notify", "publish", "deploy", "exec", "execute",
     "transfer", "pay", "purge", "submit", "insert", "upload", "move", "rename", "drop",
     "revoke", "grant", "reset", "kill", "cancel", "approve", "archive", "destroy", "wipe",
     "merge", "push", "invite", "share", "reply", "forward", "refund", "withdraw", "click",
     "fill", "press", "navigate", "eval", "evaluate")
)  # fmt: skip


def _name_tokens(name: str) -> list[str]:
    """``getUser`` / ``get_user`` / ``get-user`` → ``["get", "user"]``."""
    import re

    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return [token for token in re.split(r"[^a-z0-9]+", spaced.lower()) if token]


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
    """Classify an MCP tool into a capability risk class.

    ``read`` only for a server-marked read-only tool whose leading verb is a
    read verb (B09) and whose name has no mutation verb anywhere (W10).
    Otherwise external_side_effect > network > write > unknown by whole name
    token. Every non-read class requires approval
    (fail closed); a project may grant a risk explicitly — a visible act.
    """
    tokens = _name_tokens(tool.name)
    if (
        tool.read_only is True
        and tokens
        and tokens[0] in _MCP_READ_VERBS
        and _MCP_MUTATION_VERBS.isdisjoint(tokens)
    ):
        return "read"
    for risk, keywords in (
        ("external_side_effect", _MCP_EXTERNAL_KEYWORDS),
        ("network", _MCP_NETWORK_KEYWORDS),
        ("write", _MCP_WRITE_KEYWORDS),
    ):
        if any(token in keywords for token in tokens):
            return risk
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
            annotations = entry.get("annotations")
            tools.append(
                MCPTool(
                    server_id=self._config.server_id,
                    name=name,
                    description=str(entry.get("description") or ""),
                    input_schema=dict(schema) if isinstance(schema, dict) else {},
                    read_only=isinstance(annotations, dict)
                    and annotations.get("readOnlyHint") is True,
                )
            )
        return tools

    def call_tool(self, name: str, arguments: Mapping[str, object]) -> MCPCallResult:
        try:
            response = self._runner("tools/call", {"name": name, "arguments": dict(arguments)})
        except Exception as exc:
            return MCPCallResult(False, error=str(exc) or exc.__class__.__name__)
        if response.get("isError"):
            return MCPCallResult(False, error=str(response.get("content") or "tool error"))
        return MCPCallResult(True, content=response.get("content"))


# -- v40-F10 (v36-F6): the engine goes live ---------------------------------

# ES (B112): ONE total deadline per MCP exchange. httpx's timeout is per
# read, so a server streaming SSE keep-alives never trips it — a Queen turn
# hung > 10 min and held serve's shutdown (lane F3 f3-33).
MCP_CALL_DEADLINE_SECONDS: float = 30

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


def runner_for_config(config: MCPServerConfig, *, skep_home: Path | None = None) -> MCPRunner:
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
            import time

            # The spec requires both accept types; official SDK servers 406 without.
            headers = {"Accept": "application/json, text/event-stream"}
            budget = MCP_CALL_DEADLINE_SECONDS
            deadline = time.monotonic() + budget
            try:
                # The per-read timeout bounds silence; the deadline bounds a trickle.
                with httpx.Client(timeout=budget) as client:
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
                                if time.monotonic() > deadline:
                                    raise MCPError(f"http MCP call passed its {budget:g}s deadline")
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
        if not config.server_id.startswith(FORGED_SERVER_PREFIX):
            proc = subprocess.run(
                list(config.command),
                input=payload,
                capture_output=True,
                text=True,
                timeout=30,
            )
        else:
            proc = _run_forged_sandboxed(config, payload, skep_home)
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


# ES (B115): promote_tool registers every activated forged/seed tool as
# ``forge-<plugin_id>``; those servers never run on the bare host.
FORGED_SERVER_PREFIX = "forge-"


def _run_forged_sandboxed(
    config: MCPServerConfig, payload: str, skep_home: Path | None
) -> subprocess.CompletedProcess[str]:
    """Run one call of an activated forged tool inside the sandbox its trial
    used: writes confined to a throwaway workspace, a scrubbed env (serve.env
    keys never reach it), no backend → refuse (fail closed, I12). Network
    stays open — an ACTIVE plugin may use it (plugin_can_run); the trial's
    no-network shape is the gate before activation, not after.

    ponytail: allow-all egress — a per-tool domain allowlist through the
    FilteringProxy is the upgrade path once tools declare their hosts.
    """
    import os
    import subprocess
    import tempfile
    from pathlib import Path

    from . import sandbox

    probe = sandbox.availability()
    if not probe.usable:
        raise MCPError(
            f"forged tool {config.server_id!r} runs only inside the sandbox, and no "
            f"sandbox backend is usable here ({probe.reason})"
        )
    if skep_home is None:
        # The profile hides THIS home's secrets; guessing it is how #58's fast
        # lane leaked (Mac finding A). No home, no run (fail closed, I12).
        raise MCPError(f"forged tool {config.server_id!r} needs its skep home to run sandboxed")
    with tempfile.TemporaryDirectory(prefix="skep-forged-") as tmp:
        workspace = Path(tmp)
        profile = sandbox.write_profile(
            workspace / "sandbox.profile",
            workspace=workspace,
            network=sandbox.ALLOW_ALL_NETWORK,
            skep_home=skep_home,
        )
        env = {name: os.environ[name] for name in ("PATH", "LANG") if name in os.environ}
        env.update(HOME=str(workspace), TMPDIR=str(workspace))
        return subprocess.run(
            sandbox.wrap_command(list(config.command), profile),
            input=payload,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=workspace,
            env=env,
        )


def with_server_hints(store: RunStore, tool: MCPTool) -> MCPTool:
    """B09: a bare tool name from a call carries no ``readOnlyHint``; ask the
    server's own tools/list. Only a read-verb name can become a read, so only
    those pay the lookup; any failure leaves the tool not-read-only (asks).

    ponytail: one live tools/list per read-verb decision — cache per server
    if MCP call volume makes the extra round trip matter.
    """
    if tool.read_only is not None:
        return tool
    tokens = _name_tokens(tool.name)
    read_only = False
    config = load_mcp_servers(store).get(tool.server_id)
    # A browse server classifies by its own page-state list (v71-F2).
    if config is not None and config.scope != "browse" and tokens and tokens[0] in _MCP_READ_VERBS:
        try:
            listed = MCPClient(
                config, runner=runner_for_config(config, skep_home=store.db_path.parent.parent)
            ).list_tools()
        except Exception:  # an unreachable server cannot vouch for a read
            listed = []
        read_only = any(entry.name == tool.name and entry.read_only for entry in listed)
    return replace(tool, read_only=read_only)


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
        tool = with_server_hints(store, tool)
        return "email", ("read" if classify_mcp_risk(tool) == "read" else "send")
    if scope == "browse":
        return "browse", classify_browse_action(tool.name)
    return "mcp", "call"


def _read_only(tool: MCPTool, scope: str, action: str) -> bool:
    return action == "read" if scope in ("email", "browse") else classify_mcp_risk(tool) == "read"


def mcp_call_is_read(store: RunStore, server_id: str, name: str) -> bool:
    """P4-2 (ADR 0059 D6): is this call a read, by the same classifier
    ``mcp_scope_decision`` hands ``decide()``? A non-read call on a server
    with no trust flag (none exists yet) is a HIGH card."""
    tool = with_server_hints(store, MCPTool(server_id=server_id, name=name, description=""))
    scope, action = mcp_tool_scope_action(store, tool)
    return _read_only(tool, scope, action)


def mcp_scope_decision(
    store: RunStore,
    holder: ConfigHolder,
    tool: MCPTool,
    *,
    role: Role | None = None,
    chat_id: str | None = None,
) -> AutonomyDecision:
    """One MCP tool call, decided by ``policy_v3.decide()`` with the chat's
    role (ADR 0059 P3-3) under the scope its server binds: ``mcp.call``,
    ``email.read|send`` (v41-F3) or ``browse.read|act`` (v71-F2). The value
    is ``server:tool`` — never the call's arguments.

    The classifiers are inputs, not a second decision: ``read_only`` is the
    server's ``readOnlyHint`` plus a leading read verb (B09 — a name alone is
    never a read; an unreachable server cannot vouch, so the tool is not one),
    an email read, or a browse page-state read. A template allow grants an
    ``mcp.call`` only for a read-only tool, and an unmatched read-only request
    answers ``default/read-only`` allow (the read default the ladder held);
    everything else unmatched asks. Baseline and role rules reach every call
    (B29); a learned ``allow_mcp_tool`` grant answers a role's catch-all
    ``mcp-gated *`` ask (decision #1, B56). allow → in-turn, deny → refused
    with no card, ask → the card naming the rule that asked. A policy that
    will not load refuses (fail closed). ``chat_id``: the chat asking — its
    taint gates the call (P4-1, D5: a non-read tool is outbound and acts)."""
    from .autonomy import AutonomyDecision
    from .policy_v3 import READ_ONLY, Request, queen_decide

    tool = with_server_hints(store, tool)
    scope, action = mcp_tool_scope_action(store, tool)
    value = f"{tool.server_id}:{tool.name}"
    read_only = _read_only(tool, scope, action)
    try:
        verdict = queen_decide(
            holder, store, Request(scope, action, value), role=role, read_only=read_only,
            chat_id=chat_id,
        )  # fmt: skip
    except Exception as exc:  # I9: a policy that will not load never calls a tool
        return AutonomyDecision(
            verdict="deny",
            reason=f"{scope}.deny.policy_unloadable",
            detail=f"the policy did not load, so {value} is not called: {exc}",
            decided_by="policy/unloadable",
        )
    if verdict.verdict == "require_approval":
        asked = "role_layer" if verdict.layer.startswith("role:") else verdict.rule_id
        reason = f"{scope}.require_approval.{asked}"
        if verdict.layer == "taint":
            return AutonomyDecision(
                verdict="require_approval", reason=f"taint.require_approval.{asked}",
                detail=verdict.note, decided_by=verdict.decided_by,
            )  # fmt: skip
    elif verdict.decided_by == f"default/{READ_ONLY}":
        reason = f"{scope}.allow.read_risk"
    else:
        reason = f"{scope}.{verdict.verdict}.scope_rule"
    return AutonomyDecision(
        verdict=verdict.verdict, reason=reason, detail=value, decided_by=verdict.decided_by
    )
