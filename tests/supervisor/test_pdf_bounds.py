"""Review R1 (K2, K11): the stdlib PDF reader under hostile input.

read_url parses PDFs inside the serve process, so a tiny hostile file must
not cost gigabytes or minutes. One budget per document bounds total decoded
bytes (stacked Flate included), parsed objects (an object stream's /N is
bounded by its body), parser work, Form XObject calls (depth, cycles and a
total cap) and the extracted text. Past a cap the reader refuses with a
ValueError that says so — or, for output, stops and says the text goes on.
Every file here is built in code (``pdf_builder``); each case asserts its
wall clock."""

from __future__ import annotations

import time
import zlib
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from skep.seeds.tools import doc_extract
from skep.seeds.tools.doc_extract import pdf_to_text

from .pdf_builder import HELVETICA, build, simple_pdf, stream

SECONDS = 5.0  # generous for a loaded CI box; unbounded parsing takes minutes


@contextmanager
def _quick() -> Iterator[None]:
    start = time.monotonic()
    yield
    assert time.monotonic() - start < SECONDS


def _page_with(content_ref: str, extra_objects: list[bytes], resources: str = "") -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /Contents {content_ref} "
            f"/Resources << /Font << /F1 4 0 R >> {resources} >> >>"
        ).encode(),
        HELVETICA,
        *extra_objects,
    ]
    return build(objects)


def _flate_of_zeros(megabytes: int) -> bytes:
    packer = zlib.compressobj(9)
    chunk = b" " * (1 << 20)
    return b"".join(packer.compress(chunk) for _ in range(megabytes)) + packer.flush()


def test_a_stacked_flate_bomb_is_refused_past_the_decoded_budget() -> None:
    inner = _flate_of_zeros(doc_extract.MAX_DECODED_BYTES // (1 << 20) + 8)
    outer = zlib.compress(inner, 9)
    bomb = (
        f"<< /Length {len(outer)} /Filter [/FlateDecode /FlateDecode] >>\nstream\n".encode()
        + outer
        + b"\nendstream"
    )
    with _quick(), pytest.raises(ValueError, match=r"too complex.*decoded"):
        pdf_to_text(_page_with("5 0 R", [bomb]))


def test_a_filter_chain_past_its_cap_is_refused() -> None:
    body = b"BT /F1 12 Tf (x) Tj ET"
    for _ in range(doc_extract.MAX_FILTERS + 1):
        body = zlib.compress(body)
    chain = " ".join(["/FlateDecode"] * (doc_extract.MAX_FILTERS + 1))
    many = f"<< /Length {len(body)} /Filter [{chain}] >>\nstream\n".encode() + body + b"\nendstream"
    with _quick(), pytest.raises(ValueError, match=r"too complex.*filters"):
        pdf_to_text(_page_with("5 0 R", [many]))


def test_an_object_stream_claiming_a_billion_objects_costs_nothing() -> None:
    header = b"6 0 -5 0 7 999999999 "
    body = header + b"<< /Type /Font >>"
    objstm = stream(body, f"/Type /ObjStm /N 1000000000 /First {len(header)} ")
    content = stream(b"BT /F1 12 Tf 72 720 Td (still here) Tj ET")
    with _quick():
        assert pdf_to_text(_page_with("6 0 R", [objstm, content])) == "still here"


def test_a_self_referencing_form_is_drawn_once() -> None:
    form = stream(b"BT /F1 12 Tf (loop) Tj ET /X0 Do", "/Type /XObject /Subtype /Form ")
    content = stream(b"/X0 Do")
    pdf = _page_with("6 0 R", [form, content], "/XObject << /X0 5 0 R >>")
    with _quick():
        assert pdf_to_text(pdf) == "loop"


def test_exponential_form_fan_out_is_refused_past_the_form_cap() -> None:
    # Form i draws form i+1 forty times: 40**8 calls unbounded.
    depth = 8
    forms = []
    for level in range(depth):
        draws = b" ".join([b"/N Do"] * 40)
        nxt = 5 + level + 1
        resources = f"/Resources << /XObject << /N {nxt} 0 R >> >> " if level < depth - 1 else ""
        forms.append(stream(draws, f"/Type /XObject /Subtype /Form {resources}"))
    content = stream(b"/N Do")
    pdf = _page_with(f"{5 + depth} 0 R", [*forms, content], "/XObject << /N 5 0 R >>")
    with _quick(), pytest.raises(ValueError, match=r"too complex.*form"):
        pdf_to_text(pdf)


def test_a_cmap_of_huge_ranges_is_bounded_by_the_work_budget() -> None:
    ranges = b"\n".join(b"<0000> <FFFF> <0041>" for _ in range(2000))
    cmap = stream(
        b"begincodespacerange <0000> <FFFF> endcodespacerange\n"
        b"2000 beginbfrange\n" + ranges + b"\nendbfrange"
    )
    font = b"<< /Type /Font /Subtype /Type0 /BaseFont /X /ToUnicode 6 0 R >>"
    content = stream(b"BT /F1 12 Tf <0041> Tj ET")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Contents 5 0 R /Resources << /Font << /F1 4 0 R >> >> >>",
        font,
        content,
        cmap,
    ]
    with _quick(), pytest.raises(ValueError, match="too complex"):
        pdf_to_text(build(objects))


def test_many_streams_without_endstream_scan_in_linear_time() -> None:
    junk = b"".join(b"%d 0 obj << /Length 999999 >> stream\nxx\n" % n for n in range(9, 60000))
    pdf = simple_pdf(b"BT /F1 12 Tf 72 720 Td (body) Tj ET") + junk
    with _quick():
        assert "body" in pdf_to_text(pdf)


def test_nested_object_headers_cost_bounded_work() -> None:
    nested = b"1 0 obj [" + b"1 0 obj " * 400_000 + b"]"
    with _quick(), pytest.raises(ValueError, match="too complex"):
        pdf_to_text(b"%PDF-1.7\n" + nested)


def test_the_extracted_text_stops_at_its_cap_and_says_so() -> None:
    line = b"(" + b"word " * 200 + b") Tj 0 -14 Td "
    content = b"BT /F1 12 Tf 72 720 Td " + line * 200 + b"ET"
    with _quick():
        text = pdf_to_text(simple_pdf(content), max_chars=5000)
    assert text.startswith("word word")
    assert len(text) < 6000 and text.endswith("[the PDF's text continues past 5000 characters]")


def test_an_indirect_contents_array_reads_every_stream_in_order() -> None:
    """K11: /Contents 5 0 R where object 5 is an ARRAY of streams was read as
    one stream — an empty page, mislabelled "no text layer"."""
    first = stream(b"BT /F1 12 Tf 72 720 Td (first part) Tj ET")
    second = stream(b"BT /F1 12 Tf 72 700 Td (second part) Tj ET")
    pdf = _page_with("5 0 R", [b"[6 0 R 7 0 R]", first, second])
    assert pdf_to_text(pdf) == "first part\nsecond part"
