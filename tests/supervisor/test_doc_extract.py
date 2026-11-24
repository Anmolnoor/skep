"""Phase 07 / 7.3: the stdlib PDF text extractor and the doc_extract seed tool.

Every PDF here is built from source by ``pdf_builder`` (real objects,
streams, xref) so no binary fixture ships."""

from __future__ import annotations

import contextlib
import http.server
import importlib.util
import json
import subprocess
import sys
import threading
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from skep.seeds.tools.doc_extract import http_error_message, pdf_to_text
from skep.supervisor.forge import seed_tool_source, seed_tools_root, trial_script, trial_verdict

from .pdf_builder import HELVETICA, build, simple_pdf, stream

# --- the extractor ------------------------------------------------------------------------


def test_lines_and_pages_in_page_tree_order() -> None:
    first = b"BT /F1 12 Tf 72 720 Td (Hello, world!) Tj 0 -14 Td (Second line) Tj ET"
    second = b"BT /F1 12 Tf 72 720 Td (Page two) Tj ET"
    assert pdf_to_text(simple_pdf(first, second)) == "Hello, world!\nSecond line\n\nPage two"


def test_page_order_follows_kids_not_object_numbers() -> None:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        # Kids name object 6 first; inherited Resources come from the Pages node
        b"<< /Type /Pages /Kids [6 0 R 4 0 R] /Count 2 /Resources << /Font << /F1 3 0 R >> >> >>",
        HELVETICA,
        b"<< /Type /Page /Parent 2 0 R /Contents 5 0 R >>",
        stream(b"BT /F1 12 Tf 1 0 0 1 72 700 Tm (later) Tj ET"),
        b"<< /Type /Page /Parent 2 0 R /Contents [7 0 R 8 0 R] >>",
        stream(b"BT /F1 12 Tf 72 700 Td (earlier) Tj ET"),
        stream(b"BT /F1 12 Tf 72 680 Td (same page) Tj ET", flate=False),
    ]
    assert pdf_to_text(build(objects)) == "earlier\nsame page\n\nlater"


def test_spaces_come_from_where_text_lands() -> None:
    """A word placed past the last one's end is a new word; glyphs placed
    edge to edge (Chrome prints each glyph with its own Td) are one word.
    Helvetica here has no /Widths, so a glyph is 500/1000 em: 'Hello' at
    12pt advances 30."""
    gap = b"BT /F1 12 Tf 72 720 Td (Hello) Tj 40 0 Td (world) Tj ET"
    flush = b"BT /F1 12 Tf 72 720 Td (Hello) Tj 30 0 Td (world) Tj ET"
    kerned = b"BT /F1 12 Tf 72 720 Td [(Hel) 20 (lo) -300 (world)] TJ ET"
    assert pdf_to_text(simple_pdf(gap)) == "Hello world"
    assert pdf_to_text(simple_pdf(flush)) == "Helloworld"
    assert pdf_to_text(simple_pdf(kerned)) == "Hello world"


def test_text_blocks_on_one_baseline_join_and_quote_operators_break_lines() -> None:
    content = (
        b"BT /F1 10 Tf 1 0 0 1 72 700 Tm (left) Tj ET "
        b"BT /F1 10 Tf 1 0 0 1 200 700 Tm (right) Tj ET "
        b"BT /F1 10 Tf 12 TL 72 600 Td (one) Tj (two) ' 0 0 (three) \" ET"
    )
    assert pdf_to_text(simple_pdf(content)) == "left right\none\ntwo\nthree"


def test_literal_and_hex_string_escapes() -> None:
    content = rb"BT /F1 12 Tf 72 720 Td (a\(b\) \\ \101\102C) Tj 0 -20 Td <48693> Tj ET"
    assert pdf_to_text(simple_pdf(content)) == "a(b) \\ ABC\nHi0"


def test_composite_font_maps_through_tounicode_and_widths() -> None:
    """Type0 / Identity-H: 2-byte codes, text only via ToUnicode (bfchar,
    bfrange with a base and with an array), advances from the W array."""
    cmap = (
        b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
        b"1 begincodespacerange <0000> <FFFF> endcodespacerange\n"
        b"2 beginbfchar <0003> <0020> <0010> <FB01> endbfchar\n"
        b"2 beginbfrange <0024> <003D> <0041> <0044> <0045> [<0068> <0069>] endbfrange\n"
        b"endcmap CMapName currentdict /CMap defineresource pop end end"
    )
    # 0x2B -> 'H', 0x44 -> 'h', 0x45 -> 'i', 0x10 -> 'fi' ligature, 0x03 -> ' '
    content = (
        b"BT /F1 20 Tf 1 0 0 -1 50 100 Tm <002B> Tj 12 0 Td <0045> Tj "
        b"4 0 Td <0003> Tj 6 0 Td <0010> Tj 10 0 Td <0044> Tj ET"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        b"<< /Type /Font /Subtype /Type0 /BaseFont /X /Encoding /Identity-H "
        b"/DescendantFonts [6 0 R] /ToUnicode 7 0 R >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>",
        stream(content),
        b"<< /Type /Font /Subtype /CIDFontType2 /DW 1000 "
        b"/W [3 [300] 16 [500] 43 45 600 68 69 200] >>",
        stream(cmap),
    ]
    assert pdf_to_text(build(objects)) == "Hi fih"


def test_objects_inside_an_object_stream_are_found() -> None:
    """PDF 1.5 packs dictionaries (here the page and the font) into a
    compressed /ObjStm — common in every modern producer."""
    packed = [
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 6 0 R >> >> /Contents 3 0 R >>",
        HELVETICA,
    ]
    header = b""
    body = b""
    for number, item in zip((5, 6), packed, strict=True):
        header += f"{number} {len(body)} ".encode()
        body += item + b" "
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [5 0 R] /Count 1 >>",
        stream(b"BT /F1 12 Tf 72 720 Td (packed) Tj ET"),
        stream(header + body, f"/Type /ObjStm /N 2 /First {len(header)} "),
    ]
    assert pdf_to_text(build(objects)) == "packed"


def test_differences_encoding_and_form_xobjects() -> None:
    font = (
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Times-Roman "
        b"/Encoding << /Type /Encoding /Differences [1 /quoteright /fi /uni00E9] >> >>"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        font,
        b"<< /Type /Page /Parent 2 0 R /Contents 5 0 R "
        b"/Resources << /Font << /F1 3 0 R >> /XObject << /Fm0 6 0 R >> >> >>",
        stream(b"BT /F1 12 Tf 72 720 Td (it\001s) Tj ET q /Fm0 Do Q"),
        stream(
            b"BT /F1 12 Tf 72 600 Td (\002nal caf\003) Tj ET",
            "/Type /XObject /Subtype /Form /BBox [0 0 612 792] "
            "/Resources << /Font << /F1 3 0 R >> >> ",
        ),
    ]
    assert pdf_to_text(build(objects)) == "it\u2019s\nfinal café"


def test_an_inline_image_does_not_derail_the_content_parser() -> None:
    content = (
        b"BT /F1 12 Tf 72 720 Td (before) Tj ET "
        b"BI /W 2 /H 2 /CS /G /BPC 8 ID \x00) ( <]\xff EI "
        b"BT /F1 12 Tf 72 700 Td (after) Tj ET"
    )
    assert pdf_to_text(simple_pdf(content)) == "before\nafter"


def test_an_encrypted_pdf_says_so_and_what_to_do() -> None:
    pdf = build(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [] /Count 0 >>",
            b"<< /Filter /Standard /V 4 /R 4 /O <00> /U <00> /P -4 >>",
        ],
        trailer_extra="/Encrypt 3 0 R ",
    )
    with pytest.raises(ValueError, match="encrypted") as caught:
        pdf_to_text(pdf)
    assert "open it" in str(caught.value)


def test_a_scanned_pdf_with_no_text_layer_says_so() -> None:
    image = b"q 612 0 0 792 0 0 cm /Im0 Do Q"
    with pytest.raises(ValueError, match="no text layer"):
        pdf_to_text(simple_pdf(image))


def test_composite_glyphs_with_no_unicode_map_are_named_not_returned_as_scraps() -> None:
    """A CJK PDF whose Type0 font ships no ToUnicode: the few mapped glyphs
    (a page number in Helvetica) must not pass as the document's text."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        b"<< /Type /Font /Subtype /Type0 /Encoding /Identity-H /DescendantFonts [6 0 R] >>",
        b"<< /Type /Page /Parent 2 0 R /Contents 5 0 R "
        b"/Resources << /Font << /K1 3 0 R /F1 7 0 R >> >> >>",
        stream(b"BT /K1 12 Tf 72 720 Td <0F2A0F2B0F2C0F2D> Tj ET BT /F1 9 Tf 300 40 Td (1) Tj ET"),
        b"<< /Type /Font /Subtype /CIDFontType0 /DW 1000 >>",
        HELVETICA,
    ]
    with pytest.raises(ValueError, match="no Unicode map"):
        pdf_to_text(build(objects))


def test_not_a_pdf_is_refused() -> None:
    with pytest.raises(ValueError, match="not a PDF"):
        pdf_to_text(b"<html>nope</html>")


def test_a_cut_or_corrupt_pdf_only_ever_raises_valueerror() -> None:
    """Truncated downloads and junk must not crash a read: every cut point
    of a valid file yields text or the actionable ValueError."""
    whole = simple_pdf(b"BT /F1 12 Tf 72 720 Td (Hello) Tj ET")
    for cut in range(0, len(whole), 7):
        with contextlib.suppress(ValueError):
            pdf_to_text(whole[:cut])
    junk = b"%PDF-1.4\n1 0 obj << /Length 999999 /Filter /FlateDecode >> stream\nxx"
    with pytest.raises(ValueError):
        pdf_to_text(junk + zlib.compress(b"partial")[:5])


def test_http_error_messages_name_the_next_step() -> None:
    assert "nothing at this URL" in http_error_message(404, "Not Found")
    busy = http_error_message(429, "Too Many Requests", "30")
    assert "wait 30s" in busy and "rate-limiting" in busy
    assert "Retry-After" in http_error_message(429, "", "Wed, 21 Oct 2026 07:28:00 GMT")
    assert "wait a minute" in http_error_message(429, "")
    assert "login wall or bot block" in http_error_message(403, "Forbidden")
    assert "try again later" in http_error_message(503, "Service Unavailable", "120")
    assert "wait 120s" in http_error_message(503, "Service Unavailable", "120")
    assert http_error_message(418, "I'm a teapot").startswith("HTTP 418 I'm a teapot")


# --- the seed tool ------------------------------------------------------------------------


def _seed_module() -> Any:
    path = seed_tools_root() / "doc_extract.py"
    spec = importlib.util.spec_from_file_location("doc_extract_seed", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_doc_extract_passes_the_real_trial_harness(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-c", trial_script(seed_tool_source("doc_extract.py"))],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=120,
    )
    ok, reason, evidence = trial_verdict(
        {"state": "completed", "output": proc.stdout, "stderr": proc.stderr}
    )
    assert ok, reason
    assert evidence["tools"] == ["extract_document", "self_test"]
    assert "offline" in str(evidence["self_test"])


def test_doc_extract_is_stdlib_only() -> None:
    """A seed runs as one copied file: it may import nothing from skep."""
    source = seed_tool_source("doc_extract.py")
    assert "import skep" not in source and "from skep" not in source


_PDF = simple_pdf(b"BT /F1 12 Tf 72 720 Td (Quarterly report) Tj ET")
_PAGES: dict[str, tuple[int, dict[str, str], bytes]] = {
    "/report.pdf": (200, {"Content-Type": "application/pdf"}, _PDF),
    "/mislabelled": (200, {"Content-Type": "application/octet-stream"}, _PDF),
    "/page": (
        200,
        {"Content-Type": "text/html; charset=utf-8"},
        b"<nav>Menu</nav><main><h1>Title</h1><p>Body &amp; more</p></main>",
    ),
    "/missing": (404, {}, b"gone"),
    "/busy": (429, {"Retry-After": "30"}, b""),
    "/logo.png": (200, {"Content-Type": "image/png"}, b"\x89PNG\r\n"),
}


@pytest.fixture
def local_site() -> Iterator[str]:
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
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()


def test_doc_extract_reads_pdfs_and_html_and_errors_say_what_next(
    local_site: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _seed_module()
    monkeypatch.setattr(module, "_refuse_non_public", lambda _host: None)  # the fake is loopback

    def call(url: str) -> dict[str, Any]:
        result: dict[str, Any] = module.call_tool("extract_document", {"url": url})
        return result

    assert call(f"{local_site}/report.pdf") == {"content": "Quarterly report"}
    assert call(f"{local_site}/mislabelled") == {"content": "Quarterly report"}
    assert call(f"{local_site}/page") == {"content": "Title\nBody & more"}
    missing = call(f"{local_site}/missing")
    assert missing["isError"] and "HTTP 404" in missing["content"]
    busy = call(f"{local_site}/busy")
    assert busy["isError"] and "wait 30s" in busy["content"]
    image = call(f"{local_site}/logo.png")
    assert image["isError"] and "image/png is not a document" in image["content"]
    empty = module.call_tool("extract_document", {})
    assert empty["isError"] and "needs a url" in empty["content"]
    ftp = module.call_tool("extract_document", {"url": "ftp://example.com/x.pdf"})
    assert ftp["isError"] and "http(s)" in ftp["content"]


def test_doc_extract_refuses_non_public_addresses_like_read_url() -> None:
    module = _seed_module()
    for url in ("http://127.0.0.1:8765/api", "http://localhost/", "http://169.254.169.254/"):
        result = module.call_tool("extract_document", {"url": url})
        assert result["isError"] and "public" in result["content"], url


def test_doc_extract_answers_jsonrpc_over_stdio() -> None:
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "nope"}},
    ]
    proc = subprocess.run(
        [sys.executable, str(seed_tools_root() / "doc_extract.py")],
        input="\n".join(json.dumps(r) for r in requests) + "\n",
        capture_output=True,
        text=True,
        timeout=60,
    )
    replies = [json.loads(line) for line in proc.stdout.splitlines()]
    names = [tool["name"] for tool in replies[0]["result"]["tools"]]
    assert names == ["extract_document", "self_test"]
    assert replies[1]["result"]["isError"] is True


def test_stream_filters_comments_and_more_text_operators() -> None:
    """ASCIIHex / ASCII85 streams, comments, real numbers, TD / T*, octal and
    line-continuation escapes, a simple font's /Widths — and streams that
    hold no text (an image codec, broken Flate) are skipped, not fatal."""
    import base64

    words = b"BT /F1 10 Tf 72.5 700 TD (one\\\ntwo\\n) Tj T* (three) Tj ET % trailing comment\n"
    font = (
        b"<< /Type /Font /Subtype /TrueType /BaseFont /A /FirstChar 65 "
        b"/Widths [1000 1000] /Encoding /MacRomanEncoding >>"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        font,
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 3 0 R >> >> "
        b"/Contents [5 0 R 6 0 R 7 0 R 8 0 R 9 0 R] >>",
        b"<< /Filter /ASCIIHexDecode /Length 0 >>\nstream\n"
        + words.hex().encode()
        + b">\nendstream",
        b"<< /Filter /ASCII85Decode >>\nstream\n<~"
        + base64.a85encode(b"BT /F1 10 Tf 72 600 Td (AB) Tj 0 0 Td (AB) Tj ET")
        + b"~>\nendstream",
        b"<< /Filter /DCTDecode >>\nstream\n\xff\xd8 not text\nendstream",
        b"<< /Filter /FlateDecode >>\nstream\nnot zlib\nendstream",
        b"<< /Filter /ASCII85Decode >>\nstream\n<~ v~>\nendstream",
    ]
    # TD sets leading 700... so T* jumps a line; (AB)(AB) at one spot: the
    # second AB lands where the first started, far from its end -> a space
    assert pdf_to_text(build(objects)) == "onetwo\nthree\nAB AB"


def test_object_streams_and_page_trees_that_lie_are_survived() -> None:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        # the tree points back at itself and at a missing object
        b"<< /Type /Pages /Kids [2 0 R 9 0 R 4 0 R] /Count 1 >>",
        b"<< /Type /ObjStm /N 2 /First 1 /Filter /DCTDecode /Length 1 >>\nstream\nx\nendstream",
        b"<< /Type /Page /Parent 2 0 R /Contents 5 0 R "
        b"/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /Encoding "
        b"<< /Differences [65 /A /notaglyph] >> >> >> >> >>",
        stream(b"(no font yet) Tj BT /F1 12 Tf 72 720 Td (AB) Tj ET"),
    ]
    assert pdf_to_text(build(objects)) == "AB"


def _call_seed(module: Any, monkeypatch: pytest.MonkeyPatch, failure: Exception) -> str:
    def boom(*_args: object, **_kw: object) -> None:
        raise failure

    monkeypatch.setattr(module, "_refuse_non_public", lambda _host: None)
    monkeypatch.setattr(module.urllib.request.OpenerDirector, "open", boom)
    result: dict[str, Any] = module.call_tool("extract_document", {"url": "https://x.example/a"})
    assert result["isError"] is True
    return str(result["content"])


def test_doc_extract_transport_failures_say_what_next(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    module = _seed_module()
    assert "timed out after 20s" in _call_seed(module, monkeypatch, TimeoutError())
    unreachable = _call_seed(module, monkeypatch, urllib.error.URLError("refused"))
    assert "could not reach x.example" in unreachable and "sandbox" in unreachable


def test_doc_extract_size_text_and_type_branches(
    local_site: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _seed_module()
    monkeypatch.setattr(module, "_refuse_non_public", lambda _host: None)
    _PAGES["/notes.txt"] = (200, {"Content-Type": "text/plain"}, b"  plain notes \n")
    _PAGES["/untyped"] = (200, {}, b"<p>untyped page</p>")
    _PAGES["/zip"] = (200, {"Content-Type": "application/zip"}, b"PK")
    assert module.call_tool("extract_document", {"url": f"{local_site}/notes.txt"}) == {
        "content": "plain notes"
    }
    assert module.extract(f"{local_site}/untyped") == "untyped page"
    with pytest.raises(ValueError, match="application/zip is not a document"):
        module.extract(f"{local_site}/zip")
    monkeypatch.setattr(module, "MAX_BYTES", 4)
    with pytest.raises(ValueError, match="too big to read here"):
        module.extract(f"{local_site}/report.pdf")


def test_doc_extract_refuses_a_redirect_into_a_private_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import urllib.request

    module = _seed_module()
    calls: list[str] = []

    def guard(host: str) -> None:
        calls.append(host)
        if host == "127.0.0.1":
            raise ValueError("only reads public internet addresses")

    monkeypatch.setattr(module, "_refuse_non_public", guard)
    handler = module._Redirects()
    request = urllib.request.Request("https://public.example/a")
    with pytest.raises(ValueError, match="public"):
        handler.redirect_request(request, None, 302, "Found", {}, "http://127.0.0.1/admin")
    moved = handler.redirect_request(request, None, 302, "Found", {}, "https://public.example/b")
    assert moved.full_url == "https://public.example/b" and calls == ["127.0.0.1", "public.example"]
    with pytest.raises(ValueError, match="does not resolve"):
        _seed_module()._refuse_non_public("no-such-host.invalid")


def test_doc_extract_self_test_and_stdio_loop_in_process(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import io

    module = _seed_module()
    assert "self_test passed" in module.call_tool("self_test", {})["content"]
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "self_test"}},
        {"jsonrpc": "2.0", "id": 3, "method": "notifications/initialized"},
    ]
    monkeypatch.setattr(module, "call_tool", lambda *_a: 1 / 0)  # a crash is still a reply
    stdin = "\n".join(json.dumps(line) for line in lines[:1]) + "\n\nnot json\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin + "\n".join(map(json.dumps, lines[1:]))))
    module.main()
    replies = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert replies[0]["result"]["protocolVersion"] == "2024-11-05"
    assert replies[1]["result"] == {
        "content": "ZeroDivisionError: division by zero",
        "isError": True,
    }
    assert replies[2]["result"] == {}

    broken = _seed_module()
    monkeypatch.setattr(broken, "pdf_to_text", lambda _data: "wrong")
    assert broken.call_tool("self_test", {}) == {
        "content": "PDF extraction broken",
        "isError": True,
    }
    monkeypatch.setattr(broken, "pdf_to_text", lambda _data: "self test\nline two")
    monkeypatch.setattr(broken, "html_to_text", lambda _page: "wrong")
    assert "HTML extraction broken" in broken.call_tool("self_test", {})["content"]
