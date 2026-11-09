"""B1-1 (ADR 0054 D1/D6, A9): the ACP proxy core is byte-transparent by default,
answers claimed requests itself, and reports forwarded responses and cancels."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor.acp.proxy import Direction, Forward, Handler, Proxy, Reply

from ..fixtures.acp import ACP_DIR, ALL_FIXTURES, envelope_frames

ASK = ACP_DIR / "claude-agent-acp" / "live-v1-ask.ndjson"
PERMISSION: tuple[Direction, str] = ("a2c", "session/request_permission")


def _run(
    frames: list[tuple[Direction, bytes]], handlers: dict[tuple[Direction, str], Handler]
) -> tuple[bytes, bytes]:
    to_client, to_agent = io.BytesIO(), io.BytesIO()
    proxy = Proxy(to_client.write, to_agent.write, handlers)
    for direction, raw in frames:
        proxy.feed(direction, raw)
    return to_client.getvalue(), to_agent.getvalue()


def _line(direction: Direction, msg: dict[str, Any]) -> tuple[Direction, bytes]:
    return direction, json.dumps(msg).encode() + b"\n"


def _permission_index(frames: list[tuple[Direction, bytes]]) -> int:
    return next(i for i, (_, raw) in enumerate(frames) if b"session/request_permission" in raw)


@pytest.mark.parametrize("path", ALL_FIXTURES, ids=lambda p: str(p.relative_to(ACP_DIR)))
def test_no_handlers_is_byte_transparent(path: Path) -> None:
    frames = envelope_frames(path)
    to_client, to_agent = _run(frames, {})
    assert to_client == b"".join(raw for d, raw in frames if d == "a2c")
    assert to_agent == b"".join(raw for d, raw in frames if d == "c2a")


def test_reply_answers_the_agent_and_withholds_the_request() -> None:
    frames = envelope_frames(ASK)
    ask = _permission_index(frames)
    allow = {"outcome": {"outcome": "selected", "optionId": "allow-once"}}
    to_client, to_agent = _run(frames, {PERMISSION: lambda msg: Reply(allow)})
    assert to_client == b"".join(
        raw for i, (d, raw) in enumerate(frames) if d == "a2c" and i != ask
    )
    reply = b'{"jsonrpc":"2.0","id":0,"result":' + json.dumps(allow, separators=(",", ":")).encode()
    assert reply + b"}\n" in to_agent
    # the fixture's own recorded answer is just an unclaimed response: it passes through
    recorded = frames[ask + 1]
    assert recorded[0] == "c2a" and recorded[1] in to_agent
    assert to_agent == b"".join(
        raw for d, raw in frames[: ask + 1] if d == "c2a"
    ) + reply + b"}\n" + b"".join(raw for d, raw in frames[ask + 1 :] if d == "c2a")


def test_reply_error_is_sent_to_the_sender() -> None:
    req = _line("c2a", {"jsonrpc": "2.0", "id": "x", "method": "fs/nope"})
    to_client, to_agent = _run(
        [req], {("c2a", "fs/nope"): lambda m: Reply(error={"code": -1, "message": "no"})}
    )
    assert to_agent == b""
    assert json.loads(to_client) == {
        "jsonrpc": "2.0",
        "id": "x",
        "error": {"code": -1, "message": "no"},
    }


def test_forward_reports_the_response_with_an_int_id() -> None:
    frames = envelope_frames(ASK)
    ask = _permission_index(frames)
    pair = frames[ask : ask + 2]
    seen: list[dict[str, Any] | None] = []
    to_client, to_agent = _run(pair, {PERMISSION: lambda m: Forward(seen.append)})
    assert seen == [json.loads(pair[1][1])]
    assert (to_client, to_agent) == (pair[0][1], pair[1][1])


def test_forward_reports_the_response_with_a_string_id() -> None:
    req = _line("a2c", {"jsonrpc": "2.0", "id": "p-1", "method": "terminal/create", "params": {}})
    resp = _line("c2a", {"jsonrpc": "2.0", "id": "p-1", "result": {"terminalId": "t"}})
    seen: list[dict[str, Any] | None] = []
    to_client, to_agent = _run(
        [req, resp], {("a2c", "terminal/create"): lambda m: Forward(seen.append)}
    )
    assert seen == [json.loads(resp[1])]
    assert (to_client, to_agent) == (req[1], resp[1])


CANCEL_REQUEST = {"jsonrpc": "2.0", "method": "$/cancel_request", "params": {"requestId": 0}}
SESSION_CANCEL = {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": "s1"}}


@pytest.mark.parametrize(
    ("side", "cancel", "cancels"),
    [
        ("a2c", CANCEL_REQUEST, True),  # the requester cancels its own request
        ("c2a", SESSION_CANCEL, True),  # the client ends the agent's asks for its session
        ("c2a", CANCEL_REQUEST, False),  # the client cannot cancel the agent's id 0
        ("a2c", SESSION_CANCEL, False),  # session/cancel is client-only
    ],
)
def test_cancel_is_direction_bound(side: Direction, cancel: dict[str, Any], cancels: bool) -> None:
    req = _line(
        "a2c", {"jsonrpc": "2.0", "id": 0, "method": PERMISSION[1], "params": {"sessionId": "s1"}}
    )
    stop = _line(side, cancel)
    late = _line(
        "c2a", {"jsonrpc": "2.0", "id": 0, "result": {"outcome": {"outcome": "cancelled"}}}
    )
    seen: list[dict[str, Any] | None] = []
    to_client, to_agent = _run([req, stop, late], {PERMISSION: lambda m: Forward(seen.append)})
    assert seen == ([None] if cancels else [json.loads(late[1])])
    frames = [req, stop, late]
    assert to_client == b"".join(raw for d, raw in frames if d == "a2c")
    assert to_agent == b"".join(raw for d, raw in frames if d == "c2a")


def test_session_cancel_leaves_the_clients_own_requests_pending() -> None:
    prompt = _line(
        "c2a",
        {"jsonrpc": "2.0", "id": 3, "method": "session/prompt", "params": {"sessionId": "s1"}},
    )
    done = _line("a2c", {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "cancelled"}})
    seen: list[dict[str, Any] | None] = []
    _run(
        [prompt, _line("c2a", SESSION_CANCEL), done],
        {("c2a", "session/prompt"): lambda m: Forward(seen.append)},
    )
    assert seen == [json.loads(done[1])]


def test_raising_handler_passes_the_frame_through_and_reports() -> None:
    errors: list[tuple[str, BaseException]] = []
    boom = RuntimeError("boom")

    def handler(msg: dict[str, Any]) -> Reply:
        raise boom

    req = _line("a2c", {"jsonrpc": "2.0", "id": 0, "method": PERMISSION[1]})
    to_client, to_agent = io.BytesIO(), io.BytesIO()
    proxy = Proxy(
        to_client.write,
        to_agent.write,
        {PERMISSION: handler},
        on_error=lambda label, exc: errors.append((label, exc)),
    )
    proxy.feed(*req)
    assert to_client.getvalue() == req[1] and to_agent.getvalue() == b""
    assert errors == [(PERMISSION[1], boom)]


def test_raising_callback_still_lets_the_response_through() -> None:
    def callback(resp: dict[str, Any] | None) -> None:
        raise ValueError("bad")

    req = _line("a2c", {"jsonrpc": "2.0", "id": 1, "method": PERMISSION[1]})
    resp = _line("c2a", {"jsonrpc": "2.0", "id": 1, "result": {}})
    to_client, to_agent = _run([req, resp], {PERMISSION: lambda m: Forward(callback)})
    assert (to_client, to_agent) == (req[1], resp[1])


def test_duplicate_pending_id_reports_none_to_the_first() -> None:
    first: list[dict[str, Any] | None] = []
    second: list[dict[str, Any] | None] = []
    callbacks = iter([first.append, second.append])
    req = _line("a2c", {"jsonrpc": "2.0", "id": 5, "method": PERMISSION[1]})
    resp = _line("c2a", {"jsonrpc": "2.0", "id": 5, "result": {}})
    _run([req, req, resp], {PERMISSION: lambda m: Forward(next(callbacks))})
    assert first == [None]
    assert second == [json.loads(resp[1])]


def test_null_id_is_neither_answered_nor_correlated() -> None:
    seen: list[dict[str, Any] | None] = []
    req = _line("a2c", {"jsonrpc": "2.0", "id": None, "method": PERMISSION[1]})
    resp = _line("c2a", {"jsonrpc": "2.0", "id": None, "result": {}})
    to_client, to_agent = _run([req, resp], {PERMISSION: lambda m: Forward(seen.append)})
    assert (to_client, to_agent, seen) == (req[1], resp[1], [])
    to_client, to_agent = _run([req], {PERMISSION: lambda m: Reply({})})
    assert (to_client, to_agent) == (req[1], b"")


def test_cancel_method_drops_a_pending_forward() -> None:
    seen: list[dict[str, Any] | None] = []
    proxy = Proxy(
        io.BytesIO().write, io.BytesIO().write, {PERMISSION: lambda m: Forward(seen.append)}
    )
    proxy.feed(*_line("a2c", {"jsonrpc": "2.0", "id": 7, "method": PERMISSION[1]}))
    proxy.cancel("a2c", 7)
    proxy.cancel("a2c", 7)
    assert seen == [None]


def test_bad_lines_and_unanswerable_replies_pass_through() -> None:
    lines: list[tuple[Direction, bytes]] = [
        ("a2c", b"{not json\n"),
        ("a2c", b"[1, 2]\n"),
        ("a2c", b'{"jsonrpc": "2.0"}\n'),
        ("a2c", '{ "jsonrpc":"2.0" ,  "method" : "x/y", "params":{"t":"café"}}\r\n'.encode()),
        _line("a2c", {"jsonrpc": "2.0", "method": "session/update", "params": {}}),
    ]
    to_client, to_agent = _run(lines, {("a2c", "session/update"): lambda m: Reply("ignored")})
    assert to_client == b"".join(raw for _, raw in lines)
    assert to_agent == b""


def test_ids_are_never_coerced() -> None:
    seen: dict[str, list[dict[str, Any] | None]] = {"int": [], "str": [], "zero": []}
    keys = {1: "int", "1": "str", 0: "zero"}
    frames = [
        _line(
            "a2c",
            {"jsonrpc": "2.0", "id": i, "method": "fs/write_text_file", "params": {"k": keys[i]}},
        )
        for i in keys
    ]
    frames += [_line("c2a", {"jsonrpc": "2.0", "id": i, "result": keys[i]}) for i in ("1", 0, 1)]
    _run(frames, {("a2c", "fs/write_text_file"): lambda m: Forward(seen[m["params"]["k"]].append)})
    assert seen == {
        "int": [{"jsonrpc": "2.0", "id": 1, "result": "int"}],
        "str": [{"jsonrpc": "2.0", "id": "1", "result": "str"}],
        "zero": [{"jsonrpc": "2.0", "id": 0, "result": "zero"}],
    }


def test_on_frame_sees_every_object_frame_before_dispatch() -> None:
    frames = envelope_frames(ASK)
    seen: list[tuple[Direction, Any]] = []
    errors: list[str] = []

    def handler(msg: dict[str, Any]) -> None:
        assert seen[-1] == ("a2c", msg.get("id"))  # on_frame ran first

    def on_frame(direction: Direction, msg: dict[str, Any]) -> None:
        seen.append((direction, msg.get("id")))
        if msg.get("id") == 2:
            raise RuntimeError("frame hook broke")

    lines = [*frames, ("a2c", b"{not json\n")]
    to_client, to_agent = io.BytesIO(), io.BytesIO()
    proxy = Proxy(
        to_client.write,
        to_agent.write,
        {PERMISSION: handler},
        on_error=lambda label, exc: errors.append(label),
        on_frame=on_frame,
    )
    for direction, raw in lines:
        proxy.feed(direction, raw)
    assert seen == [(d, json.loads(raw).get("id")) for d, raw in frames]
    assert errors == ["frame", "frame"]  # the c2a request and the a2c response with id 2
    assert to_client.getvalue() == b"".join(raw for d, raw in lines if d == "a2c")
    assert to_agent.getvalue() == b"".join(raw for d, raw in lines if d == "c2a")
