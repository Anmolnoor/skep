"""A scripted stand-in for the Ollama HTTP API (v6).

Real localhost HTTP, like the fake worker is a real subprocess: the serve
daemon's LLM client talks to this over the wire, so auth headers, status
codes, and NDJSON streaming are exercised for real. ``/api/tags`` serves a
fixed model list; ``/api/chat`` pops the next scripted response (a list of
stream chunks) per call and records every request body for assertions.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeOllama:
    def __init__(self, *, api_key: str | None = None, models: list[str] | None = None) -> None:
        self.api_key = api_key
        self.models = models if models is not None else ["llama3.2", "qwen3:8b"]
        # Each entry scripts one POST /api/chat: the chunks of its reply stream.
        self.chat_scripts: list[list[dict[str, Any]]] = []
        # v48-F1: statuses to answer /api/chat with BEFORE serving scripts —
        # scripts the transient flakes ollama.com shows in the field.
        self.fail_statuses: list[int] = []
        # v73-F1: the provider's own request wall — /api/chat bodies larger
        # than this many bytes draw a 400, like ollama.com in the field.
        self.reject_over_bytes: int | None = None
        # v74-F2: what POST /api/show reports per model (the real daemon keys
        # context length under the model's ARCHITECTURE prefix). Models not
        # listed here draw a 404, like an older daemon or unknown model.
        self.show_context_lengths: dict[str, int] = {}
        self.requests: list[dict[str, Any]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> FakeOllama:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def script_reply(self, text: str, *, model: str = "fake") -> None:
        """Queue a plain streamed text reply, split into word chunks."""
        words = text.split(" ")
        chunks: list[dict[str, Any]] = [
            {"model": model, "message": {"role": "assistant", "content": w if i == 0 else f" {w}"}}
            for i, w in enumerate(words)
        ]
        chunks.append(
            {"model": model, "message": {"role": "assistant", "content": ""}, "done": True}
        )
        self.chat_scripts.append(chunks)

    def script_tool_call(
        self, name: str, arguments: dict[str, Any], *, model: str = "fake"
    ) -> None:
        """Queue a reply that calls one tool and ends the turn."""
        self.chat_scripts.append(
            [
                {
                    "model": model,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"function": {"name": name, "arguments": arguments}}],
                    },
                },
                {"model": model, "message": {"role": "assistant", "content": ""}, "done": True},
            ]
        )

    def chat_bodies(self) -> list[dict[str, Any]]:
        """The parsed bodies of every POST /api/chat received, in order."""
        return [r["body"] for r in self.requests if r["path"] == "/api/chat"]

    def chat_raw_sizes(self) -> list[int]:
        """v73-F1: the wire byte size of every POST /api/chat, in order."""
        return [r["raw_len"] for r in self.requests if r["path"] == "/api/chat"]

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # silence per-request stderr
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
                elif self.path == "/api/tags":
                    # Alternate "name"/"model" keys — both shapes occur in the wild.
                    listed = [
                        {"name": m} if i % 2 == 0 else {"model": m}
                        for i, m in enumerate(fake.models)
                    ]
                    self._send(200, json.dumps({"models": listed}).encode())
                else:
                    self._send(404, b'{"error": "not found"}')

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                body = json.loads(raw) if raw else None
                fake.requests.append(
                    {
                        "path": self.path,
                        "headers": dict(self.headers),
                        "body": body,
                        # v73-F1: the wire size — what reject_over_bytes compares.
                        "raw_len": len(raw),
                    }
                )
                if not self._authorized():
                    self._send(401, b'{"error": "unauthorized"}')
                elif self.path == "/api/show":
                    model = str((body or {}).get("model") or "")
                    length = fake.show_context_lengths.get(model)
                    if length is None:
                        self._send(404, b'{"error": "model not found"}')
                        return
                    shown = {
                        "model_info": {
                            "general.architecture": "llama",
                            "llama.context_length": length,
                        }
                    }
                    self._send(200, json.dumps(shown).encode())
                elif self.path == "/api/chat":
                    if fake.fail_statuses:
                        self._send(fake.fail_statuses.pop(0), b'{"error": "transient"}')
                        return
                    if fake.reject_over_bytes is not None and len(raw) > fake.reject_over_bytes:
                        self._send(400, b'{"error": "request too large"}')
                        return
                    if not fake.chat_scripts:
                        self._send(500, b'{"error": "no scripted reply left"}')
                        return
                    chunks = fake.chat_scripts.pop(0)
                    payload = "".join(json.dumps(c) + "\n" for c in chunks).encode()
                    self._send(200, payload, content_type="application/x-ndjson")
                else:
                    self._send(404, b'{"error": "not found"}')

        return Handler
