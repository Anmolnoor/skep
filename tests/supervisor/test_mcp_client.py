"""v17 Step 2: the MCP client skeleton (injected runner, hermetic)."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
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


def test_b112_keep_alive_stream_hits_a_total_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """ES (B112, lane F3 f3-33): the 30 s httpx timeout is per read, so an SSE
    stream of keep-alives held a Queen turn > 10 min and blocked shutdown.
    The whole exchange now has one total deadline."""
    import time

    def forever() -> Iterator[bytes]:
        while True:
            time.sleep(0.02)
            yield b": keep-alive\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
        if "id" not in body:
            return httpx.Response(202)
        return httpx.Response(200, content=forever(), headers={"content-type": "text/event-stream"})

    _patch_http(monkeypatch, handler)
    monkeypatch.setattr("skep.supervisor.mcp_client.MCP_CALL_DEADLINE_SECONDS", 0.3)
    started = time.monotonic()
    with pytest.raises(MCPError, match="deadline"):
        runner_for_config(_http_config())("tools/call", {"name": "get_report"})
    assert time.monotonic() - started < 5


# R6: a stdio server shaped like Playwright's — it rejects an initialize with
# no clientInfo, answers a call only after work that takes time, and stops
# the moment its stdin closes.
_SLOW_STDIO_SERVER = r"""
import json, os, sys, threading, time
greeted = False
def answer(msg_id):
    time.sleep(0.5)
    reply = {"content": [{"type": "text", "text": "page"}]} if greeted else None
    out = {"jsonrpc": "2.0", "id": msg_id}
    out.update({"result": reply} if reply else {"error": {"message": "no clientInfo"}})
    print(json.dumps(out), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    if request.get("method") == "initialize":
        greeted = isinstance(request["params"].get("clientInfo"), dict)
    elif request.get("method") == "tools/call":
        threading.Thread(target=answer, args=(request["id"],)).start()
os._exit(0)
"""


def test_r6_stdio_runner_waits_for_a_slow_reply_before_closing_stdin() -> None:
    """R6 (D25): the real Playwright server got stdin's EOF with the request
    and exited (~0.4 s) before its browser answered — every call came back
    "no JSON-RPC response". The runner introduces itself (clientInfo, which
    the spec requires) and holds stdin open until request 2 is answered."""
    import sys

    config = MCPServerConfig(
        server_id="slow", transport="stdio", command=(sys.executable, "-c", _SLOW_STDIO_SERVER)
    )
    call = MCPClient(config, runner=runner_for_config(config)).call_tool("browser_snapshot", {})
    assert call.ok, call.error
    assert call.content == [{"type": "text", "text": "page"}]


def test_r6_stdio_runner_gives_up_at_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setattr("skep.supervisor.mcp_client.MCP_CALL_DEADLINE_SECONDS", 0.2)
    config = MCPServerConfig(
        server_id="mute",
        transport="stdio",
        command=(sys.executable, "-c", "import sys, time\nfor _ in sys.stdin: time.sleep(5)"),
    )
    call = MCPClient(config, runner=runner_for_config(config)).call_tool("anything", {})
    assert not call.ok
    assert "deadline" in str(call.error)


def test_r6_stdio_runner_reports_a_server_that_exits_without_answering() -> None:
    import sys

    config = MCPServerConfig(
        server_id="gone",
        transport="stdio",
        command=(sys.executable, "-c", "print('starting up'); print('{\"id\": 1}')"),
    )
    call = MCPClient(config, runner=runner_for_config(config)).call_tool("anything", {})
    assert not call.ok
    assert call.error == "no JSON-RPC response for the tool request"


# A browse server shaped like Playwright's file output: a relative `filename`
# argument is written under the server's own cwd.
_FILE_WRITING_SERVER = r"""
import json, os, sys
for line in sys.stdin:
    request = json.loads(line)
    if request.get("method") == "tools/call":
        name = request["params"]["arguments"]["filename"]
        open(name, "w").write("console log")
        where = os.path.realpath(name)
        reply = {"jsonrpc": "2.0", "id": request["id"], "result": {"content": where}}
        print(json.dumps(reply), flush=True)
"""


def test_r6_a_browse_server_writes_files_only_in_its_folder_under_the_skep_home(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R6 review: Playwright's page-state reads (browser_snapshot,
    browser_console_messages, …) take an optional `filename` and write it
    anywhere under the server's cwd — which was serve's own folder (the repo
    or the operator's home). A read runs in the turn with no card, so a read
    could overwrite a file there. The browse server now runs in a folder of
    its own under the skep home."""
    import sys

    serve_cwd = tmp_path / "serve-cwd"
    serve_cwd.mkdir()
    (serve_cwd / "notes.txt").write_text("mine")
    monkeypatch.chdir(serve_cwd)
    home = tmp_path / "home"
    config = MCPServerConfig(
        server_id="browser",
        transport="stdio",
        command=(sys.executable, "-c", _FILE_WRITING_SERVER),
        scope="browse",
    )
    call = MCPClient(config, runner=runner_for_config(config, skep_home=home)).call_tool(
        "browser_console_messages", {"filename": "notes.txt"}
    )
    assert call.ok, call.error
    assert (serve_cwd / "notes.txt").read_text() == "mine"
    assert call.content == str((home / "browser" / "notes.txt").resolve())
    # No home to put the folder in: the browse server does not start at all.
    refused = MCPClient(config, runner=runner_for_config(config)).call_tool("x", {"filename": "y"})
    assert not refused.ok
    assert "skep home" in str(refused.error)
