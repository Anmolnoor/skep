"""v17 Step 2: the MCP client skeleton (injected runner, hermetic)."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

import httpx
import pytest

from skep.supervisor.mcp_client import (
    MCPClient,
    MCPError,
    MCPServerConfig,
    MCPTool,
    runner_for_config,
)

# Captured before any monkeypatching so stacked patches never wrap each other.
_REAL_CLIENT = httpx.Client


def test_config_validates_transport_and_required_fields() -> None:
    MCPServerConfig("s", "stdio", command=("mcp-server",)).validate()
    MCPServerConfig("s", "http", url="https://mcp.example").validate()
    with pytest.raises(MCPError):
        MCPServerConfig("s", "carrier-pigeon").validate()
    with pytest.raises(MCPError):
        MCPServerConfig("s", "stdio").validate()  # no command
    with pytest.raises(MCPError):
        MCPServerConfig("s", "http").validate()  # no url


def _runner(responses: dict[str, Mapping[str, object]]):  # type: ignore[no-untyped-def]
    def run(method: str, params: Mapping[str, object]) -> Mapping[str, object]:
        return responses[method]

    return run


def test_list_tools_captures_names_and_schemas() -> None:
    client = MCPClient(
        MCPServerConfig("files", "stdio", command=("srv",)),
        runner=_runner(
            {
                "tools/list": {
                    "tools": [
                        {
                            "name": "read_file",
                            "description": "Read a file",
                            "inputSchema": {"type": "object", "properties": {"path": {}}},
                        },
                        {"name": "", "description": "nameless -> dropped"},
                    ]
                }
            }
        ),
    )
    tools = client.list_tools()
    assert [t.name for t in tools] == ["read_file"]
    assert tools[0].capability_id == "mcp.files.read_file"
    assert tools[0].input_schema["type"] == "object"


def test_call_tool_success_and_error() -> None:
    ok_client = MCPClient(
        MCPServerConfig("files", "stdio", command=("srv",)),
        runner=_runner({"tools/call": {"content": "hello"}}),
    )
    result = ok_client.call_tool("read_file", {"path": "x"})
    assert result.ok is True and result.content == "hello"

    err_client = MCPClient(
        MCPServerConfig("files", "stdio", command=("srv",)),
        runner=_runner({"tools/call": {"isError": True, "content": "no such file"}}),
    )
    err = err_client.call_tool("read_file", {"path": "x"})
    assert err.ok is False and "no such file" in (err.error or "")


def test_transport_failure_becomes_mcp_error_not_a_crash() -> None:
    def boom(_method: str, _params: Mapping[str, object]) -> Mapping[str, object]:
        raise ConnectionError("server down")

    client = MCPClient(MCPServerConfig("files", "stdio", command=("srv",)), runner=boom)
    with pytest.raises(MCPError):
        client.list_tools()
    # A failed call is a failure result, not a raised exception.
    assert client.call_tool("read_file", {}).ok is False


def test_capability_id_shape() -> None:
    tool = MCPTool(server_id="github", name="create_issue", description="")
    assert tool.capability_id == "mcp.github.create_issue"


# -- v80-F1: the Streamable HTTP runner --------------------------------------


def _http_config() -> MCPServerConfig:
    return MCPServerConfig("remote", "http", url="https://mcp.example/mcp")


def _patch_http(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    """Route the runner's httpx.Client through a MockTransport — no live HTTP."""

    def client(**kwargs: Any) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_CLIENT(**kwargs)

    monkeypatch.setattr(httpx, "Client", client)


def test_http_runner_json_response_with_session_and_accept_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "result": {}},
                headers={"Mcp-Session-Id": "abc123"},
            )
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        assert body == {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "result": {"tools": []}})

    _patch_http(monkeypatch, handler)
    assert runner_for_config(_http_config())("tools/list", {}) == {"tools": []}
    # initialize -> initialized -> method; the server's session id echoes back.
    assert [r.headers.get("mcp-session-id") for r in seen] == [None, "abc123", "abc123"]
    assert all("text/event-stream" in r.headers["accept"] for r in seen)


def test_http_runner_sse_response_skips_interleaved_notifications(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sse = (
        b'data: {"jsonrpc":"2.0","method":"notifications/message","params":{}}\n\n'
        b'data: {"jsonrpc":"2.0","id":2,"result":{"content":"hi"}}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
        if "id" not in body:
            return httpx.Response(202)
        return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})

    _patch_http(monkeypatch, handler)
    result = runner_for_config(_http_config())("tools/call", {"name": "t"})
    assert result == {"content": "hi"}


def test_http_runner_jsonrpc_error_raises_mcp_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
        if "id" not in body:
            return httpx.Response(202)
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 2, "error": {"message": "no such tool"}}
        )

    _patch_http(monkeypatch, handler)
    with pytest.raises(MCPError, match="no such tool"):
        runner_for_config(_http_config())("tools/call", {"name": "ghost"})


def test_http_runner_transport_errors_are_honest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A 500 names the status and body.
    _patch_http(monkeypatch, lambda request: httpx.Response(500, text="boom"))
    with pytest.raises(MCPError, match="500"):
        runner_for_config(_http_config())("tools/list", {})

    # A dead URL names the connection failure, not a bare class name.
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _patch_http(monkeypatch, unreachable)
    with pytest.raises(MCPError, match="connection refused"):
        runner_for_config(_http_config())("tools/list", {})

    # A legacy HTTP+SSE server (202-empty POST responses) fails honestly.
    def legacy(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
        return httpx.Response(202)

    _patch_http(monkeypatch, legacy)
    with pytest.raises(MCPError, match="no JSON-RPC response"):
        runner_for_config(_http_config())("tools/call", {"name": "t"})
