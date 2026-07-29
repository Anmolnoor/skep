"""A scripted stand-in for the Anthropic Messages API (v72-F1).

The LLM client talks to this over localhost, so model listing, the
x-api-key/anthropic-version headers, SSE framing, and streamed tool_use
input deltas are exercised without a live provider.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeAnthropic:
    def __init__(self, *, api_key: str | None = None, models: list[str] | None = None) -> None:
        self.api_key = api_key
        self.models = models if models is not None else ["claude-sonnet-5", "claude-haiku-4-5"]
        self.chat_scripts: list[list[dict[str, Any]]] = []
        self.requests: list[dict[str, Any]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> FakeAnthropic:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def script_reply(self, text: str, *, thinking: str | None = None) -> None:
        events: list[dict[str, Any]] = [{"type": "message_start", "message": {}}]
        if thinking:
            events.append(
                {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}}
            )
            events.append(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": thinking},
                }
            )
            events.append({"type": "content_block_stop", "index": 0})
        index = 1 if thinking else 0
        events.append(
            {"type": "content_block_start", "index": index, "content_block": {"type": "text"}}
        )
        for i, word in enumerate(text.split(" ")):
            events.append(
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "text_delta", "text": word if i == 0 else f" {word}"},
                }
            )
        events.append({"type": "content_block_stop", "index": index})
        events.append({"type": "message_stop"})
        self.chat_scripts.append(events)

    def script_tool_call(self, name: str, arguments: dict[str, Any]) -> None:
        encoded = json.dumps(arguments, separators=(",", ":"))
        split = max(1, len(encoded) // 2)
        self.chat_scripts.append(
            [
                {"type": "message_start", "message": {}},
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "tool_use", "id": "toolu_1", "name": name},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": encoded[:split]},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": encoded[split:]},
                },
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
                {"type": "message_stop"},
            ]
        )

    def chat_bodies(self) -> list[dict[str, Any]]:
        return [r["body"] for r in self.requests if r["path"] == "/v1/messages"]

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def _authorized(self) -> bool:
                if fake.api_key is None:
                    return True
                return self.headers.get("x-api-key") == fake.api_key

            def _send(
                self, status: int, body: bytes, content_type: str = "application/json"
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                fake.requests.append(
                    {"path": self.path, "headers": dict(self.headers), "body": None}
                )
                if not self._authorized():
                    self._send(401, b'{"error": "unauthorized"}')
                elif self.path == "/v1/models":
                    models = [{"id": model} for model in fake.models]
                    self._send(200, json.dumps({"data": models}).encode())
                else:
                    self._send(404, b'{"error": "not found"}')

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                body = json.loads(raw) if raw else None
                fake.requests.append(
                    {"path": self.path, "headers": dict(self.headers), "body": body}
                )
                if not self._authorized():
                    self._send(401, b'{"error": "unauthorized"}')
                elif self.path == "/v1/messages":
                    if not fake.chat_scripts:
                        self._send(500, b'{"error": "no scripted reply left"}')
                        return
                    script = fake.chat_scripts.pop(0)
                    payload = "".join(
                        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                        for event in script
                    )
                    self._send(200, payload.encode(), content_type="text/event-stream")
                else:
                    self._send(404, b'{"error": "not found"}')

        return Handler
