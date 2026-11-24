"""Phase 07 / 7.3: the researcher reads PDFs and says what to do on failure.

The real ``_urllib_fetch`` runs against a loopback HTTP server — nothing
leaves the machine."""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.supervisor.contracts_io import mint_task, write_task_file
from skep.worker_contract import Permissions
from skep.workers import researcher
from skep.workers.researcher import run_research, run_researcher_task

from .pdf_builder import build, simple_pdf

_PDF = simple_pdf(b"BT /F1 12 Tf 72 720 Td (Attention is all you need) Tj ET")
_ENCRYPTED = build(
    [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [] /Count 0 >>",
        b"<< /Filter /Standard /V 2 /R 3 /O <00> /U <00> /P -4 >>",
    ],
    trailer_extra="/Encrypt 3 0 R ",
)
_PAGES: dict[str, tuple[int, dict[str, str], bytes]] = {
    "/paper.pdf": (200, {"Content-Type": "application/pdf"}, _PDF),
    "/untyped": (200, {}, _PDF),
    "/download": (200, {"Content-Type": "application/octet-stream"}, _PDF),
    "/locked.pdf": (200, {"Content-Type": "application/pdf"}, _ENCRYPTED),
    "/missing": (404, {}, b"no"),
    "/busy": (429, {"Retry-After": "30"}, b""),
    "/down": (503, {}, b""),
}


@pytest.fixture
def site() -> Iterator[str]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            status, headers, body = _PAGES[self.path]
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()


def _research(site: str, *paths: str) -> str:
    result = run_research(
        "q",
        sources=[site + path for path in paths],
        allowlist=["127.0.0.1"],
        fetch=researcher._urllib_fetch,
    )
    return result.report_md


def test_a_pdf_source_is_read_and_quoted(site: str) -> None:
    """The v122-F7 dead end (a PDF listed as binary, never read) is gone."""
    report = _research(site, "/paper.pdf", "/untyped")
    assert f"- [fetched] {site}/paper.pdf: Attention is all you need" in report
    assert f"### {site}/paper.pdf\n\nAttention is all you need" in report
    assert f"### {site}/untyped\n\nAttention is all you need" in report  # decided by the bytes
    assert "binary" not in report


def test_a_pdf_served_as_octet_stream_is_read(site: str) -> None:
    """Review R1 (K13, N S12): a download served as application/octet-stream
    is sniffed for %PDF- like an untyped one, not listed as binary."""
    report = _research(site, "/download")
    assert f"### {site}/download\n\nAttention is all you need" in report


def test_an_unreadable_pdf_is_listed_with_the_reason(site: str) -> None:
    report = _research(site, "/locked.pdf")
    assert f"{site}/locked.pdf: (binary: application/pdf, not quoted: this PDF is encrypted" in (
        report
    )


def test_http_failures_name_the_next_step(site: str) -> None:
    report = _research(site, "/missing", "/busy", "/down")
    assert f"[unreachable] {site}/missing: HTTP 404 Not Found — nothing at this URL" in report
    assert "HTTP 429 Too Many Requests — the site is rate-limiting" in report
    assert "wait 30s before trying again" in report
    assert "HTTP 503 Service Unavailable — the site is failing on its side" in report


def test_an_unreachable_host_and_a_timeout_say_what_to_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import urllib.error
    import urllib.request

    def refused(request: urllib.request.Request, timeout: float) -> None:
        raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))

    monkeypatch.setattr(urllib.request, "urlopen", refused)
    with pytest.raises(researcher.FetchError, match="network allowlist") as caught:
        researcher._urllib_fetch("https://docs.example/a")
    assert "could not reach docs.example" in str(caught.value)

    def slow(request: urllib.request.Request, timeout: float) -> None:
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", slow)
    with pytest.raises(researcher.FetchError, match="timed out after 30s"):
        researcher._urllib_fetch("https://docs.example/a")

    def connect_timeout(request: urllib.request.Request, timeout: float) -> None:
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(urllib.request, "urlopen", connect_timeout)
    with pytest.raises(researcher.FetchError, match="timed out after 30s"):
        researcher._urllib_fetch("https://docs.example/a")


def test_a_task_with_no_sources_says_what_to_pass(tmp_path: Path) -> None:
    """A missing parameter: no Sources line and nothing allow-listed."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    task = mint_task(
        workspace=workspace,
        instructions="Question: what changed",
        worker_kind="researcher",
        permissions=Permissions(
            read=["workspace"], write=["workspace"], network=[], env_allowlist=[]
        ),
    )
    out = tmp_path / "result.json"
    assert run_researcher_task(write_task_file(task, tmp_path / "task.json"), out) == 3
    details = json.loads(out.read_text())["verification"]["details"]
    assert details.startswith("no sources to read")
    assert "Sources:" in details and "allow-list" in details


def test_a_pdf_beyond_the_size_cap_is_listed_not_read(
    site: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("skep.seeds.tools.doc_extract.MAX_BYTES", 16)
    report = _research(site, "/paper.pdf")
    assert "(binary: application/pdf, not quoted: larger than 8 MiB" in report
