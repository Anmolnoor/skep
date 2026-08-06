"""A scripted stand-in for the Bedrock Converse API (v108-F6).

The client talks to this over localhost, so SigV4-signed requests, the binary
``vnd.amazon.eventstream`` framing (encoded here for real, CRCs included),
streamed toolUse input fragments, and the control-plane model listing are all
exercised without an AWS account.
"""

from __future__ import annotations

import json
import re
import struct
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

_CONVERSE_PATH = re.compile(r"^/model/(?P<model>[^/]+)/converse-stream$")
_STRING_HEADER_TYPE = 7


def encode_frame(headers: dict[str, str], payload: bytes) -> bytes:
    """One eventstream message: prelude + string headers + payload + CRCs."""
    encoded_headers = b""
    for name, value in headers.items():
        raw_name = name.encode("utf-8")
        raw_value = value.encode("utf-8")
        encoded_headers += (
            bytes([len(raw_name)])
            + raw_name
            + bytes([_STRING_HEADER_TYPE])
            + struct.pack(">H", len(raw_value))
            + raw_value
        )
    total_length = 12 + len(encoded_headers) + len(payload) + 4
    prelude = struct.pack(">II", total_length, len(encoded_headers))
    prelude += struct.pack(">I", zlib.crc32(prelude))
    message = prelude + encoded_headers + payload
    return message + struct.pack(">I", zlib.crc32(message))


def encode_event(event_type: str, body: dict[str, Any]) -> bytes:
    return encode_frame(
        {
            ":event-type": event_type,
            ":message-type": "event",
            ":content-type": "application/json",
        },
        json.dumps(body).encode("utf-8"),
    )


def encode_exception(exception_type: str, message: str) -> bytes:
    return encode_frame(
        {
            ":message-type": "exception",
            ":exception-type": exception_type,
            ":content-type": "application/json",
        },
        json.dumps({"message": message}).encode("utf-8"),
    )


class FakeBedrock:
    def __init__(self, *, models: list[str] | None = None) -> None:
        self.models = models if models is not None else ["anthropic.claude-sonnet-4-v1:0"]
        self.chat_scripts: list[bytes] = []
        self.requests: list[dict[str, Any]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> FakeBedrock:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def script_reply(self, text: str, *, thinking: str | None = None) -> None:
        frames = [encode_event("messageStart", {"role": "assistant"})]
        if thinking:
            frames.append(
                encode_event(
                    "contentBlockDelta",
                    {"contentBlockIndex": 0, "delta": {"reasoningContent": {"text": thinking}}},
                )
            )
        for i, word in enumerate(text.split(" ")):
            frames.append(
                encode_event(
                    "contentBlockDelta",
                    {"contentBlockIndex": 0, "delta": {"text": word if i == 0 else f" {word}"}},
                )
            )
        frames.append(encode_event("contentBlockStop", {"contentBlockIndex": 0}))
        frames.append(encode_event("messageStop", {"stopReason": "end_turn"}))
        frames.append(encode_event("metadata", {"usage": {"inputTokens": 120, "outputTokens": 30}}))
        self.chat_scripts.append(b"".join(frames))

    def script_tool_call(self, name: str, arguments: dict[str, Any]) -> None:
        encoded = json.dumps(arguments, separators=(",", ":"))
        split = max(1, len(encoded) // 2)
        frames = [
            encode_event("messageStart", {"role": "assistant"}),
            encode_event(
                "contentBlockStart",
                {
                    "contentBlockIndex": 0,
                    "start": {"toolUse": {"toolUseId": "tooluse_1", "name": name}},
                },
            ),
            encode_event(
                "contentBlockDelta",
                {"contentBlockIndex": 0, "delta": {"toolUse": {"input": encoded[:split]}}},
            ),
            encode_event(
                "contentBlockDelta",
                {"contentBlockIndex": 0, "delta": {"toolUse": {"input": encoded[split:]}}},
            ),
            encode_event("contentBlockStop", {"contentBlockIndex": 0}),
            encode_event("messageStop", {"stopReason": "tool_use"}),
            encode_event("metadata", {"usage": {"inputTokens": 8, "outputTokens": 4}}),
        ]
        self.chat_scripts.append(b"".join(frames))

    def script_exception(self, exception_type: str, message: str) -> None:
        self.chat_scripts.append(
            encode_event("messageStart", {"role": "assistant"})
            + encode_exception(exception_type, message)
        )

    def chat_requests(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if _CONVERSE_PATH.match(r["path"])]

    def chat_bodies(self) -> list[dict[str, Any]]:
        return [r["body"] for r in self.chat_requests()]

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def _signed(self) -> bool:
                # Every Bedrock call is SigV4-signed; an unsigned one is a 403.
                return str(self.headers.get("Authorization") or "").startswith("AWS4-HMAC-SHA256")

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
                if not self._signed():
                    self._send(403, b'{"message": "unsigned request"}')
                elif self.path == "/foundation-models":
                    summaries = [{"modelId": model} for model in fake.models]
                    self._send(200, json.dumps({"modelSummaries": summaries}).encode())
                else:
                    self._send(404, b'{"message": "not found"}')

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                body = json.loads(raw) if raw else None
                fake.requests.append(
                    {"path": self.path, "headers": dict(self.headers), "body": body}
                )
                if not self._signed():
                    self._send(403, b'{"message": "unsigned request"}')
                elif _CONVERSE_PATH.match(self.path):
                    if not fake.chat_scripts:
                        self._send(500, b'{"message": "no scripted reply left"}')
                        return
                    self._send(
                        200,
                        fake.chat_scripts.pop(0),
                        content_type="application/vnd.amazon.eventstream",
                    )
                else:
                    self._send(404, b'{"message": "not found"}')

        return Handler
