"""A scripted stand-in for the OpenAI-compatible chat/completions API (v7).

The LLM client talks to this over localhost, so model listing, auth headers, SSE
framing, and streamed tool-call deltas are exercised without a live provider.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeOpenAI:
    def __init__(self, *, api_key: str | None = None, models: list[str] | None = None) -> None:
        self.api_key = api_key
        self.models = models if models is not None else ["gpt-oss", "qwen3"]
        self.chat_scripts: list[list[dict[str, Any]]] = []
        self.requests: list[dict[str, Any]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> FakeOpenAI:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def script_reply(self, text: str) -> None:
        words = text.split(" ")
        chunks: list[dict[str, Any]] = [
            {"choices": [{"delta": {"content": w if i == 0 else f" {w}"}}]}
            for i, w in enumerate(words)
        ]
        chunks.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        self.chat_scripts.append(chunks)

    def script_drop(self) -> None:
        """v59-F4: the next chat call sends a partial body then slams the
        connection — the client sees a transport-class incomplete read."""
        self.chat_scripts.append([{"__drop__": True}])

    def script_tool_call(self, name: str, arguments: dict[str, Any]) -> None:
        encoded = json.dumps(arguments, separators=(",", ":"))
        split = max(1, len(encoded) // 2)
        self.chat_scripts.append(
            [
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": name,
                                            "arguments": encoded[:split],
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "function": {"arguments": encoded[split:]},
                                    }
                                ]
                            }
                        }
                    ]
                },
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            ]
        )

    def chat_bodies(self) -> list[dict[str, Any]]:
        return [r["body"] for r in self.requests if r["path"] == "/v1/chat/completions"]

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def _authorized(self) -> bool:
                if fake.api_key is None:
                    return True
                return self.headers.get("Authorization") == f"Bearer {fake.api_key}"

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
                elif self.path == "/v1/chat/completions":
                    if not fake.chat_scripts:
                        self._send(500, b'{"error": "no scripted reply left"}')
                        return
                    script = fake.chat_scripts.pop(0)
                    if script and script[0].get("__drop__"):
                        # Promise more bytes than we send, then RST the socket
                        # (SO_LINGER 0 — a plain close leaves the client
                        # waiting out its read timeout): the client raises on
                        # the incomplete body.
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Content-Length", "1000")
                        self.end_headers()
                        self.wfile.write(b"data: ")
                        self.wfile.flush()
                        # close() alone leaves the fd open (rfile/wfile hold
                        # io refcounts on it); shutdown sends the FIN now.
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.close_connection = True
                        return
                    payload = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in script)
                    payload += "data: [DONE]\n\n"
                    self._send(200, payload.encode(), content_type="text/event-stream")
                else:
                    self._send(404, b'{"error": "not found"}')

        return Handler
