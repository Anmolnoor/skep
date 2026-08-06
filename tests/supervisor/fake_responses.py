"""A scripted stand-in for the OpenAI Responses API (v108-F5).

The LLM client talks to this over localhost, so model listing, bearer auth,
SSE framing, the typed ``response.*`` event feed, split function-call argument
fragments, and the usage counts on ``response.completed`` are exercised
without a live provider.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# The counts every scripted turn reports, so a test can assert the mapping
# onto ollama's prompt_eval_count/eval_count without guessing.
USAGE_INPUT_TOKENS = 7
USAGE_OUTPUT_TOKENS = 9


def _completed() -> dict[str, Any]:
    return {
        "type": "response.completed",
        "response": {
            "usage": {
                "input_tokens": USAGE_INPUT_TOKENS,
                "output_tokens": USAGE_OUTPUT_TOKENS,
            }
        },
    }


class FakeResponses:
    def __init__(self, *, api_key: str | None = None, models: list[str] | None = None) -> None:
        self.api_key = api_key
        self.models = models if models is not None else ["gpt-5.2", "o5-mini"]
        self.chat_scripts: list[list[dict[str, Any]]] = []
        self.requests: list[dict[str, Any]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> FakeResponses:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def script_reply(self, text: str, *, thinking: str | None = None) -> None:
        events: list[dict[str, Any]] = [{"type": "response.created", "response": {}}]
        if thinking:
            events.append(
                {
                    "type": "response.reasoning_summary_text.delta",
                    "output_index": 0,
                    "delta": thinking,
                }
            )
        for i, word in enumerate(text.split(" ")):
            events.append(
                {
                    "type": "response.output_text.delta",
                    "output_index": 1 if thinking else 0,
                    "delta": word if i == 0 else f" {word}",
                }
            )
        events.append(_completed())
        self.chat_scripts.append(events)

    def script_tool_call(self, name: str, arguments: dict[str, Any]) -> None:
        encoded = json.dumps(arguments, separators=(",", ":"))
        split = max(1, len(encoded) // 2)
        self.chat_scripts.append(
            [
                {"type": "response.created", "response": {}},
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {"type": "function_call", "call_id": "call_abc", "name": name},
                },
                {
                    "type": "response.function_call_arguments.delta",
                    "output_index": 0,
                    "delta": encoded[:split],
                },
                {
                    "type": "response.function_call_arguments.delta",
                    "output_index": 0,
                    "delta": encoded[split:],
                },
                {"type": "response.output_item.done", "output_index": 0},
                _completed(),
            ]
        )

    def script_failure(self, message: str) -> None:
        self.chat_scripts.append(
            [
                {"type": "response.created", "response": {}},
                {"type": "response.failed", "response": {"error": {"message": message}}},
            ]
        )

    def chat_bodies(self) -> list[dict[str, Any]]:
        return [r["body"] for r in self.requests if r["path"] == "/v1/responses"]

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
                elif self.path == "/v1/responses":
                    if not fake.chat_scripts:
                        self._send(500, b'{"error": "no scripted reply left"}')
                        return
                    script = fake.chat_scripts.pop(0)
                    # No [DONE] sentinel: the real feed ends on response.completed
                    # and the client must not depend on one.
                    payload = "".join(
                        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in script
                    )
                    self._send(200, payload.encode(), content_type="text/event-stream")
                else:
                    self._send(404, b'{"error": "not found"}')

        return Handler
