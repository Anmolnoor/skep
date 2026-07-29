from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

from .dashboard import render_dashboard
from .status import build_status, status_json


def make_server(home: Path, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            status = build_status(home)
            if self.path in {"/", "/dashboard"}:
                self._send("text/html; charset=utf-8", render_dashboard(status))
                return
            if self.path == "/api/status":
                self._send("application/json", status_json(status))
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, _format: str, *args: Any) -> None:
            return

        def _send(self, content_type: str, body: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return ThreadingHTTPServer((host, port), Handler)


def serve(home: Path, host: str, port: int) -> tuple[str, ThreadingHTTPServer]:
    server = make_server(home, host, port)
    actual_host, actual_port = cast(tuple[str, int], server.server_address)
    url = f"http://{actual_host}:{actual_port}/dashboard"
    return url, server
