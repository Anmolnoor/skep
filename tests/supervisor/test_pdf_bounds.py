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
def _quick(seconds: float = SECONDS) -> Iterator[None]:
    start = time.monotonic()
    yield
    assert time.monotonic() - start < seconds


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
    # 3.2 MB walked linearly takes ~2.5 s on a slow box (more under a loaded
    # parallel suite); the quadratic scan this pins took hours — 30 s still
    # tells them apart without flaking on CI
    with _quick(30.0), pytest.raises(ValueError, match="too complex"):
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


# --- review R2 (L1, L2): every use is budgeted, no scan is quadratic ----------


@contextmanager
def _small() -> Iterator[None]:
    """L1: what a hostile file may allocate stays far under its old gigabytes."""
    import tracemalloc

    tracemalloc.start()
    try:
        yield
        assert tracemalloc.get_traced_memory()[1] < 160 << 20
    finally:
        tracemalloc.stop()


def test_a_contents_array_repeating_one_stream_is_charged_per_use() -> None:
    """L1: /Contents [6 0 R 6 0 R ...] decoded the stream once and joined it
    N times uncharged — a 9 KB file cost 1.6 GB. Every use is charged."""
    content = stream(b" " * (4 << 20))
    refs = " ".join(["6 0 R"] * 1000)
    pdf = _page_with("5 0 R", [f"[{refs}]".encode(), content])
    with _small(), _quick(), pytest.raises(ValueError, match="too complex"):
        pdf_to_text(pdf)


def test_a_form_drawn_many_times_is_charged_per_draw() -> None:
    """L1: each draw of a cached form parses its bytes again — charged."""
    form = stream(b" " * (2 << 20) + b"BT /F1 12 Tf (f) Tj ET", "/Type /XObject /Subtype /Form ")
    content = stream(b"/X0 Do " * 1500)
    pdf = _page_with("6 0 R", [form, content], "/XObject << /X0 5 0 R >>")
    with _small(), _quick(), pytest.raises(ValueError, match="too complex"):
        pdf_to_text(pdf)


def test_a_long_digit_run_is_scanned_in_linear_time() -> None:
    """L2: the object-header regex backtracked over every digit run — 40 KB
    of '0' took 7.7 s."""
    with _quick(), pytest.raises(ValueError, match="found no pages"):
        pdf_to_text(b"%PDF-1.7\n" + b"0" * 400_000)


@pytest.mark.parametrize(
    "token",
    [
        b"(" + b"A" * 300_000 + b")",
        b"(" + b"\\(" * 150_000 + b")",
        b"<" + b"41" * 150_000,
        b"/" + b"#41" * 100_000,
        b"/" + b"A" * 300_000,
        b"0" * 300_000,
        b" " * 300_000 + b"1",
    ],
    ids=["literal", "escapes", "hex", "name-escapes", "name", "digits", "whitespace"],
)
def test_object_stream_offsets_naming_one_big_token_cost_bounded_work(token: bytes) -> None:
    """L2: an object stream may point every one of its objects at the same
    offset — each parse re-read one huge token for a single tick."""
    count = 20_000
    header = b" ".join(b"%d 0" % (100 + i) for i in range(count)) + b" "
    objstm = stream(header + token, f"/Type /ObjStm /N {count} /First {len(header)} ")
    pdf = _page_with("6 0 R", [objstm, stream(b"BT /F1 12 Tf (still here) Tj ET")])
    with _quick():
        try:
            assert pdf_to_text(pdf) == "still here"
        except ValueError as exc:
            assert "too complex" in str(exc)


def test_nested_literal_headers_cost_bounded_work() -> None:
    """L2: every header inside an unclosed literal re-read it to the end."""
    with _quick(), pytest.raises(ValueError, match="too complex"):
        pdf_to_text(b"%PDF-1.7\n" + b"1 0 obj (" * 100_000)


def test_a_cmap_whose_blocks_never_close_scans_once() -> None:
    """L2: an unclosed beginbfchar searched to the end once per opener."""
    cmap = stream(
        b"begincodespacerange <0000> <FFFF> endcodespacerange\n"
        + b"beginbfchar " * 100_000
        + b"1 beginbfrange "
        + b"<0><0>[" * 100_000
        + b" endbfrange"
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
    with _quick(), pytest.raises(ValueError):
        pdf_to_text(build(objects))


def test_one_huge_shown_string_costs_bounded_work() -> None:
    """L2: the glyph loop ran over a 20 MiB string for a single token."""
    with _quick(), pytest.raises(ValueError, match="too complex"):
        pdf_to_text(simple_pdf(b"BT /F1 12 Tf (" + b"\x01" * (20 << 20) + b") Tj ET"))


def test_an_ascii85_zero_run_is_refused_before_it_is_decoded() -> None:
    """L2: each 'z' decodes to four bytes — the room is checked first."""
    body = b"z" * (12 << 20) + b"~>"
    a85 = f"<< /Length {len(body)} /Filter /ASCII85Decode >>\nstream\n".encode() + body
    with _small(), _quick(), pytest.raises(ValueError, match=r"too complex.*decoded"):
        pdf_to_text(_page_with("5 0 R", [a85 + b"\nendstream"]))


def test_a_page_tree_of_inline_kids_is_walked_in_bounded_work() -> None:
    """L2 (found with R2): inline-dict kids naming one shared array were
    walked exponentially — no reference, so no seen-set."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids 3 0 R >>",
        b"[<< /Kids 3 0 R >> << /Kids 3 0 R >> << /Kids 3 0 R >>]",
    ]
    with _quick(), pytest.raises(ValueError, match="found no pages"):
        pdf_to_text(build(objects))


def test_an_inline_font_is_built_once() -> None:
    """L2: an inline font dict re-parsed its ToUnicode CMap on every Tf."""
    entries = b"\n".join(b"<%04X> <0041>" % i for i in range(20_000))
    cmap = stream(
        b"begincodespacerange <0000> <FFFF> endcodespacerange\n"
        b"20000 beginbfchar\n" + entries + b"\nendbfchar"
    )
    content = stream(b"BT /F1 12 Tf <0041> Tj" + b" /F1 12 Tf" * 20_000 + b" ET")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R /Resources << /Font << /F1 "
        b"<< /Type /Font /Subtype /Type0 /ToUnicode 5 0 R >> >> >> >>",
        content,
        cmap,
    ]
    with _quick():
        assert pdf_to_text(build(objects)) == "A"


@pytest.mark.parametrize("key", ["W", "Widths", "Differences"])
def test_fonts_sharing_one_big_array_cost_bounded_work(key: str) -> None:
    """L2: Differences, W and Widths loops were free — 2,000 fonts sharing one
    50,000-entry array cost 100M steps for no tick."""
    fonts = 2000
    items = " ".join(["1"] * 50_000) if key != "Differences" else "/a " * 50_000
    shared = f"[{items}]".encode()
    first = 5 + fonts  # the shared array's object number
    if key == "W":
        font = (
            f"<< /Type /Font /Subtype /Type0 /DescendantFonts [<< /W [0 {first} 0 R] >>] >>"
        ).encode()
    elif key == "Widths":
        font = f"<< /Type /Font /FirstChar 0 /Widths {first} 0 R >>".encode()
    else:
        font = f"<< /Type /Font /Encoding << /Differences {first} 0 R >> >>".encode()
    names = " ".join(f"/F{i} {5 + i} 0 R" for i in range(fonts))
    page = f"<< /Type /Page /Parent 2 0 R /Contents 4 0 R /Resources << /Font << {names} >> >> >>"
    content = stream(b"BT " + b" ".join(b"/F%d 12 Tf (x) Tj" % i for i in range(fonts)) + b" ET")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        page.encode(),
        content,
        *([font] * fonts),
        shared,
    ]
    with _quick():
        try:
            pdf_to_text(build(objects))
        except ValueError as exc:
            assert "too complex" in str(exc)


# --- review R3: CMap range expansion, the text cap per glyph, page refs ------------


def test_a_cmap_range_is_charged_for_what_it_expands_to() -> None:
    """R3 (Sol/Terra/Neon): one bfrange entry multiplied a long destination
    65,536 times while only its input bytes were charged (776 B → 264 MiB)."""
    budget = doc_extract._Budget()
    target = b"00" * 8192
    cmap = b"beginbfrange <0000> <FFFF> <" + target + b"> endbfrange"
    began = time.monotonic()
    with pytest.raises(ValueError):
        doc_extract._cmap(cmap, budget)
    assert time.monotonic() - began < 1.0


def test_the_text_cap_holds_inside_one_shown_string() -> None:
    """R3 (Neon R3-2): every glyph's text was buffered before the cap was
    checked — a 200K-char mapping shown 2,000 times allocated ~760 MiB at a
    10K cap. The cap holds glyph by glyph."""

    class _Big:
        default = 500.0

        def __init__(self) -> None:
            self.widths: dict[int, float] = {}

        def codes(self, data: bytes) -> list[int]:
            return list(data)

        def text(self, _code: int) -> str:
            return "x" * 200_000

    state = doc_extract._Text(max_chars=10_000)
    state.font = _Big()  # type: ignore[assignment]
    with pytest.raises(doc_extract._Full):
        state.show([b"\x01" * 2000])
    assert sum(map(len, state.out)) <= 10_001


def test_every_page_ref_ticks_the_work_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """R3 (Neon R3-3): a /Contents array of non-stream refs ran uncharged
    stream lookups per page."""
    ticks: list[int] = []
    real = doc_extract._Budget.tick

    def spy(self: doc_extract._Budget, n: int = 1) -> None:
        ticks.append(n)
        real(self, n)

    monkeypatch.setattr(doc_extract._Budget, "tick", spy)
    refs = " ".join(["99 0 R"] * 37)  # 37 refs to nothing: lookups, no streams
    pdf = build([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /Contents [{refs}] >>".encode(),
    ])  # fmt: skip
    with pytest.raises(ValueError):  # no text layer — but every ref was counted
        pdf_to_text(pdf)
    assert 37 in ticks
