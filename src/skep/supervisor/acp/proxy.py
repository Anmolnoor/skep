"""B1-1 (ADR 0054 D1/D6, A9): the I/O-free core of the ACP proxy.

Transparency contract: every line no handler claims is written out as its
original bytes, in arrival order, with its id untouched.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

Direction = Literal["c2a", "a2c"]


@dataclass(frozen=True)
class Reply:
    """An answer sent back to the sender in place of forwarding the request."""

    result: Any = None
    error: dict[str, Any] | None = None


@dataclass(frozen=True)
class Forward:
    """A request forwarded as-is whose response (``None`` if cancelled) is reported back."""

    on_response: Callable[[dict[str, Any] | None], None]


Handler = Callable[[dict[str, Any]], Reply | Forward | None]
_Callback = Callable[[dict[str, Any] | None], None]


def _other(direction: Direction) -> Direction:
    return "a2c" if direction == "c2a" else "c2a"


class Proxy:
    """A JSON-RPC line relay that lets handlers claim chosen methods per direction."""

    def __init__(
        self,
        to_client: Callable[[bytes], object],
        to_agent: Callable[[bytes], object],
        handlers: Mapping[tuple[Direction, str], Handler],
        on_error: Callable[[str, BaseException], None] | None = None,
        on_frame: Callable[[Direction, dict[str, Any]], None] | None = None,
    ) -> None:
        self._out: dict[Direction, Callable[[bytes], object]] = {
            "c2a": to_agent,
            "a2c": to_client,
        }
        self._handlers = handlers
        self._on_error = on_error
        self._on_frame = on_frame
        # (request direction, json.dumps(id)) -> (callback, params.sessionId)
        self._pending: dict[tuple[Direction, str], tuple[_Callback, Any]] = {}
        self._lock = threading.Lock()

    def feed(self, direction: Direction, raw: bytes) -> None:
        """Process one line (trailing newline included) read from ``direction``'s pipe."""
        try:
            msg = json.loads(raw)
        except ValueError:
            msg = None
        if not isinstance(msg, dict):
            self._out[direction](raw)
            return
        if self._on_frame is not None:
            try:
                self._on_frame(direction, msg)
            except Exception as exc:
                if self._on_error is not None:
                    self._on_error("frame", exc)
        method = msg.get("method")
        answerable = msg.get("id") is not None
        if not isinstance(method, str):
            self._out[direction](raw)
            if answerable:
                with self._lock:
                    entry = self._pending.pop((_other(direction), json.dumps(msg["id"])), None)
                if entry is not None:
                    self._call("response", entry[0], msg)
            return
        params = msg.get("params")
        params = params if isinstance(params, dict) else {}
        if method == "$/cancel_request" and "requestId" in params:
            self.cancel(direction, params["requestId"])
        elif method == "session/cancel" and direction == "c2a" and "sessionId" in params:
            self._cancel_session(params["sessionId"])
        handler = self._handlers.get((direction, method))
        action = None
        if handler is not None:
            try:
                action = handler(msg)
            except Exception as exc:
                if self._on_error is not None:
                    self._on_error(method, exc)
        if not answerable:
            action = None
        if isinstance(action, Reply):
            body: dict[str, Any] = {"jsonrpc": "2.0", "id": msg["id"]}
            if action.error is not None:
                body["error"] = action.error
            else:
                body["result"] = action.result
            line = json.dumps(body, separators=(",", ":")).encode("utf-8") + b"\n"
            self._out[_other(direction)](line)
            return
        if isinstance(action, Forward):
            key = (direction, json.dumps(msg["id"]))
            with self._lock:
                old = self._pending.pop(key, None)
                self._pending[key] = (action.on_response, params.get("sessionId"))
            if old is not None:
                self._call(method, old[0], None)
        self._out[direction](raw)

    def cancel(self, direction: Direction, request_id: Any) -> None:
        """Drop the pending forward of a request sent from ``direction``, reporting ``None``."""
        with self._lock:
            entry = self._pending.pop((direction, json.dumps(request_id)), None)
        if entry is not None:
            self._call("cancel", entry[0], None)

    def _cancel_session(self, session_id: Any) -> None:
        # The client's session/cancel ends only the agent's open asks for that
        # session; the client's own forwarded requests still get real responses.
        with self._lock:
            keys = [k for k, e in self._pending.items() if k[0] == "a2c" and e[1] == session_id]
            dropped = [self._pending.pop(k) for k in keys]
        for callback, _ in dropped:
            self._call("session/cancel", callback, None)

    def _call(self, label: str, callback: _Callback, value: dict[str, Any] | None) -> None:
        try:
            callback(value)
        except Exception as exc:
            if self._on_error is not None:
                self._on_error(label, exc)
