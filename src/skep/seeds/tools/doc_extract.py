"""doc_extract — the plain text of a PDF or HTML document you already hold.

Phase 07 / 7.3: a shipped seed tool in the forge's single-file stdio MCP
format (stdlib only, JSON-RPC over stdio, self_test offline). Review R1
(K4): it has NO network — a forged tool's wall is deny-all unless its card
approves hosts, and this one approves none — so it extracts from content it
is given (base64 bytes or text); reading a URL is read_url's job, which
reads PDFs with this same reader (``pdf_to_text``): one PDF path, not
three. A seed runs from its own bytes inside the wall (it cannot import
skep), so the reader lives here and read_url and the researcher import it.

The PDF reader handles what text PDFs are made of: objects found by
scanning (no xref needed, so a damaged table still reads), Flate/ASCIIHex/
ASCII85 streams, PDF 1.5 object streams, simple fonts (WinAnsi/MacRoman,
/Differences) and composite fonts through their ToUnicode CMap, Form
XObjects, and word/line breaks placed by where each string lands. It does
NOT decrypt (an encrypted PDF needs a cipher stdlib lacks), OCR a scan, or
map CJK fonts that ship no ToUnicode — each of those says so. One budget
per document (K2) bounds what a hostile file may cost.
"""

import base64
import binascii
import bisect
import json
import re
import sys
import zlib
from html.parser import HTMLParser
from typing import Any, NamedTuple

MAX_BYTES = 8 * 1024 * 1024
# Review R1 (K2): one budget per document — read_url parses inside the serve
# process, so a tiny hostile file must cost neither gigabytes nor minutes.
MAX_DECODED_BYTES = 32 * 1024 * 1024  # every stream's filter output, summed
MAX_READ_BYTES = 4 * MAX_BYTES  # raw stream bytes handed to a decoder, summed
# Review R2 (L1, L2): bytes the tokenizer walks, summed over EVERY use — each
# object parse, each use of a content stream, each draw of a form, each CMap.
MAX_PARSED_BYTES = 2 * MAX_DECODED_BYTES
MAX_FILTERS = 4  # filters on one stream
MAX_OBJECTS = 200_000  # objects found by scanning plus unpacked from object streams
MAX_WORK = 2_000_000  # parser tokens plus font/CMap map entries
MAX_FORM_DEPTH = 8
MAX_FORM_CALLS = 2_000  # Form XObjects drawn, in total
MAX_TEXT_CHARS = 1_000_000
REPLY_CHARS = 100_000  # what one extract_document reply carries

TOOLS: list[dict[str, Any]] = [
    {
        "name": "extract_document",
        "description": "The plain text of a document you already hold — NO network. "
        "Arguments: {content_base64: string} (a PDF or HTML file's bytes, base64) or "
        "{content: string} (HTML or plain text). Read-only. Encrypted or scanned "
        "(image-only) PDFs are refused with a reason; it does not OCR. To read a URL, "
        "use read_url (it reads PDFs too).",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {
                "content_base64": {"type": "string"},
                "content": {"type": "string"},
            },
        },
    },
    {
        "name": "self_test",
        "description": "Zero-argument offline self-check; extracts a built-in PDF "
        "and HTML fixture.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# --- PDF -------------------------------------------------------------------------------

# Review R2 (L2): every scan is linear — digit runs are bounded and an object
# header never starts inside one (it backtracked quadratically), whitespace,
# words and literal runs are matched in C, not walked byte by byte in Python.
_NUM = re.compile(rb"[+-]?(?:\d{1,32}\.?\d{0,32}|\.\d{1,32})")
_DIGITS = re.compile(rb"\d{1,10}")
_OBJ = re.compile(rb"(?<!\d)(\d{1,10})\s+(\d{1,5})\s+obj\b")
_BLANKS = re.compile(rb"(?:[ \t\r\n\f\x00]+|%[^\r\n]*)*")
_WORD = re.compile(rb"[^ \t\r\n\f\x00()<>\[\]{}/%]*")
_LITERAL_STOP = re.compile(rb"[()\\]")
_ESCAPES = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}
_LIGATURES = str.maketrans(
    {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl"}
)
_GLYPHS = {
    "space": " ",
    "quoteright": "\u2019",
    "quoteleft": "\u2018",
    "quotedblleft": "\u201c",
    "quotedblright": "\u201d",
    "endash": "\u2013",
    "emdash": "—",
    "bullet": "\u2022",
    "hyphen": "-",
    "ellipsis": "\u2026",
    "fi": "fi",
    "fl": "fl",
    "ff": "ff",
    "ffi": "ffi",
    "ffl": "ffl",
}


class _Name(str):
    """A PDF name (/Font), apart from an operator or a string."""


class _Op(str):
    """A content-stream operator or a stray delimiter."""


class _Ref(NamedTuple):
    num: int
    gen: int


class _Budget:
    """Review R1 (K2): what one document may cost. Past any cap: ValueError."""

    def __init__(self) -> None:
        self.decoded = self.read = self.objects = self.work = self.forms = self.parsed = 0

    def raw(self, size: int) -> None:
        self.read += size  # stream extents may overlap in a hostile file
        if self.read > MAX_READ_BYTES:
            raise self.refuse(f"more than {MAX_READ_BYTES >> 20} MiB of stream bytes read")

    @staticmethod
    def refuse(what: str) -> ValueError:
        return ValueError(
            f"this PDF is too complex for skep's built-in reader ({what}) — it may be "
            "hostile or simply huge; open it in a PDF viewer"
        )

    def decode(self, size: int) -> None:
        self.decoded += size
        if self.decoded > MAX_DECODED_BYTES:
            raise self.refuse(f"more than {MAX_DECODED_BYTES >> 20} MiB of decoded streams")

    def room(self) -> int:
        return MAX_DECODED_BYTES - self.decoded

    def parse(self, size: int) -> None:
        self.parsed += size
        if self.parsed > MAX_PARSED_BYTES:
            raise self.refuse(f"more than {MAX_PARSED_BYTES >> 20} MiB parsed")

    def parse_room(self) -> int:
        return MAX_PARSED_BYTES - self.parsed

    def object(self) -> None:
        self.objects += 1
        if self.objects > MAX_OBJECTS:
            raise self.refuse(f"more than {MAX_OBJECTS} objects")

    def tick(self, amount: int = 1) -> None:
        self.work += amount
        if self.work > MAX_WORK:
            raise self.refuse(f"more than {MAX_WORK} parser steps")

    def form(self) -> None:
        self.forms += 1
        if self.forms > MAX_FORM_CALLS:
            raise self.refuse(f"more than {MAX_FORM_CALLS} form drawings")


class _Full(Exception):
    """The extracted text reached its cap: stop reading, keep what we have."""


class _Parser:
    """Tokenizer for PDF objects and content streams: composites come back
    built (dict, list), ``n g R`` as a _Ref, anything else bare as an _Op."""

    def __init__(self, data: bytes, budget: _Budget, pos: int = 0, *, paid: bool = False) -> None:
        self.data = data
        self.budget = budget
        self.pos = pos
        # Review R2 (L1, L2): every byte walked is charged. ``paid``: the
        # caller charged this whole buffer for this use; else the parser
        # charges as it goes and never walks past what the budget has left.
        self.mark = len(data) if paid else pos
        self.end = len(data) if paid else min(len(data), pos + budget.parse_room() + 1)

    def _charge(self) -> None:
        if self.pos > self.mark:
            self.budget.parse(self.pos - self.mark)
            self.mark = self.pos

    def skip(self) -> None:
        if self.pos < self.end:
            self.pos = _BLANKS.match(self.data, self.pos, self.end).end()  # type: ignore[union-attr]
        self._charge()

    def _word_end(self, pos: int) -> int:
        if pos >= self.end:
            return pos
        return _WORD.match(self.data, pos, self.end).end()  # type: ignore[union-attr]

    def token(self) -> Any:
        self.budget.tick()
        value = self._token()
        self._charge()
        return value

    def _token(self) -> Any:
        self.skip()
        data = self.data
        if self.pos >= self.end:
            return None
        char = data[self.pos : self.pos + 1]
        if char == b"/":
            end = self._word_end(self.pos + 1)
            raw = data[self.pos + 1 : end]
            self.budget.tick(raw.count(b"#"))  # L2: each #xx escape is a Python step
            raw = re.sub(rb"#([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), raw)
            self.pos = end
            return _Name(raw.decode("latin-1"))
        if char == b"(":
            return self._literal()
        if data.startswith(b"<<", self.pos):
            self.pos += 2
            table: dict[str, Any] = {}
            while (key := self.token()) is not None and key != ">>":
                table[str(key)] = self.token()
            return table
        if data.startswith(b">>", self.pos):
            self.pos += 2
            return _Op(">>")
        if char == b"<":
            end = data.find(b">", self.pos, self.end)
            end = self.end if end < 0 else end
            digits = re.sub(rb"[^0-9A-Fa-f]", b"", data[self.pos + 1 : end])
            self.pos = end + 1
            return bytes.fromhex((digits + b"0" * (len(digits) % 2)).decode())
        if char == b"[":
            self.pos += 1
            items: list[Any] = []
            while (item := self.token()) is not None and item != "]":
                items.append(item)
            return items
        if char in (b"]", b"{", b"}", b")", b">"):
            self.pos += 1
            return _Op(char.decode())
        number = _NUM.match(data, self.pos, self.end)
        if number:
            self.pos = number.end()
            if b"." in number.group():
                return float(number.group())
            return self._maybe_ref(int(number.group()))
        end = max(self._word_end(self.pos), self.pos + 1)
        word = data[self.pos : end].decode("latin-1")
        self.pos = end
        return {"true": True, "false": False, "null": None}.get(word, _Op(word))

    def _maybe_ref(self, value: int) -> Any:
        save = self.pos
        self.skip()
        gen = _DIGITS.match(self.data, self.pos, self.end)
        if gen:
            self.pos = gen.end()
            self.skip()
            if (
                self.data.startswith(b"R", self.pos, self.end)
                and self._word_end(self.pos) == self.pos + 1
            ):
                self.pos += 1
                return _Ref(value, int(gen.group()))
        self.pos = save
        return value

    def _literal(self) -> bytes:
        data, end = self.data, self.end
        self.pos += 1
        depth, out = 1, bytearray()
        while self.pos < end:
            hit = _LITERAL_STOP.search(data, self.pos, end)  # L2: plain runs copied in C
            stop = end if hit is None else hit.start()
            out += data[self.pos : stop]
            self.pos = stop
            if hit is None:
                break
            self.budget.tick()  # L2: each paren or escape is a Python step
            char = data[self.pos]
            self.pos += 1
            if char == 0x5C and self.pos < end:  # backslash escape
                escaped = data[self.pos]
                self.pos += 1
                if escaped in _ESCAPES:
                    out.append(_ESCAPES[escaped])
                elif 0x30 <= escaped <= 0x37:
                    digits = bytes([escaped])
                    while len(digits) < 3 and self.pos < end and 0x30 <= data[self.pos] <= 0x37:
                        digits += data[self.pos : self.pos + 1]
                        self.pos += 1
                    out.append(int(digits, 8) & 0xFF)
                elif escaped == 0x0D:  # line continuation
                    self.pos += data.startswith(b"\n", self.pos)
                elif escaped != 0x0A:
                    out.append(escaped)
                continue
            depth += (char == 0x28) - (char == 0x29)
            if depth == 0:
                break
            out.append(char)
        return bytes(out)


def _decode(meta: Any, raw: bytes, budget: _Budget) -> bytes | None:
    """A stream's bytes through its filters; None for codecs that hold no
    text (images, LZW) or a broken stream. Every filter's output is charged
    to ``budget`` (K2): Flate never inflates past what is left of it."""
    meta = meta if isinstance(meta, dict) else {}
    filters = meta.get("Filter")
    chain = filters if isinstance(filters, list) else [filters] if filters else []
    if len(chain) > MAX_FILTERS:
        raise budget.refuse(f"a stream with more than {MAX_FILTERS} filters")
    data = raw
    for name in chain:
        if name in ("FlateDecode", "Fl"):
            room = budget.room()
            try:
                data = zlib.decompressobj().decompress(data, room + 1)
            except zlib.error:
                return None
            if len(data) > room:
                raise budget.refuse(f"more than {MAX_DECODED_BYTES >> 20} MiB of decoded streams")
        elif name in ("ASCIIHexDecode", "AHx"):
            digits = re.sub(rb"[^0-9A-Fa-f]", b"", data.partition(b">")[0])
            data = bytes.fromhex((digits + b"0" * (len(digits) % 2)).decode())
        elif name in ("ASCII85Decode", "A85"):
            body = re.sub(rb"\s", b"", data.strip().removeprefix(b"<~").partition(b"~>")[0])
            if 4 * body.count(b"z") + len(body) > budget.room():  # L2: 'z' is four bytes
                raise budget.refuse(f"more than {MAX_DECODED_BYTES >> 20} MiB of decoded streams")
            try:
                data = base64.a85decode(body)
            except ValueError:
                return None
        else:
            return None
        budget.decode(len(data))
    return data


class _Pdf:
    def __init__(self, data: bytes) -> None:
        self.budget = _Budget()
        self.objects: dict[int, Any] = {}
        self.streams: dict[int, memoryview] = {}  # extents only: nothing copied
        self._decoded: dict[int, bytes | None] = {}
        self._fonts: dict[Any, _Font] = {}
        # every endstream once, so a stream with a bad /Length finds its end
        # by bisection — never a rescan of the file per object (K2)
        ends = [m.start() for m in re.finditer(rb"endstream", data)]
        view = memoryview(data)
        for match in _OBJ.finditer(data):
            self.budget.object()
            parser = _Parser(data, self.budget, match.end())
            value = parser.token()
            num = int(match.group(1))
            self.objects[num] = value  # a later revision of an object wins
            parser.skip()
            if isinstance(value, dict) and data.startswith(b"stream", parser.pos):
                start = parser.pos + 6
                start += (
                    2 if data.startswith(b"\r\n", start) else data[start : start + 1] in b"\r\n"
                )
                length = value.get("Length")
                end = start + length if isinstance(length, int) and length >= 0 else -1
                if end < 0 or not data[end : end + 12].lstrip().startswith(b"endstream"):
                    found = bisect.bisect_left(ends, start)
                    end = ends[found] if found < len(ends) else len(data)
                self.streams[num] = view[start:end]
        for num, value in list(self.objects.items()):
            if isinstance(value, dict) and value.get("Type") == "ObjStm":
                self._unpack(value, num)
        self.trailers = [
            value for value in self.objects.values() if isinstance(value, dict) and "Root" in value
        ]
        for match in re.finditer(rb"trailer\s*<<", data):
            trailer = _Parser(data, self.budget, match.end() - 2).token()
            if isinstance(trailer, dict):
                self.trailers.append(trailer)

    def _unpack(self, meta: dict[str, Any], num: int) -> None:
        body = self._decode(num)
        first, count = meta.get("First"), meta.get("N")
        if body is None or not isinstance(first, int) or not isinstance(count, int) or first < 0:
            return
        # K2: /N is a claim; each (number, offset) pair takes at least 3 bytes
        header = _Parser(body, self.budget)
        for _ in range(min(count, len(body) // 3)):
            packed, offset = header.token(), header.token()
            if packed is None or offset is None:
                break
            if isinstance(packed, int) and isinstance(offset, int) and offset >= 0:
                self.budget.object()
                parser = _Parser(body, self.budget, first + offset)
                self.objects.setdefault(packed, parser.token())

    def _decode(self, num: int) -> bytes | None:
        if num not in self._decoded:  # each stream decodes (and is charged) once
            raw = self.streams[num]
            self.budget.raw(len(raw))
            self._decoded[num] = _decode(self.objects.get(num), bytes(raw), self.budget)
        return self._decoded[num]

    def get(self, value: Any) -> Any:
        for _ in range(32):  # a reference chain, never a cycle
            if not isinstance(value, _Ref):
                return value
            value = self.objects.get(value.num)
        return None

    def stream(self, ref: Any) -> bytes | None:
        if not isinstance(ref, _Ref) or ref.num not in self.streams:
            return None
        return self._decode(ref.num)

    def font(self, ref: Any) -> "_Font":
        # A font's maps are built once per document — an inline font dict too
        # (L2), by identity: it lives in self.objects as long as this reader.
        key = ref if isinstance(ref, _Ref) else id(ref)
        if key not in self._fonts:
            self._fonts[key] = _Font(self, ref)
        return self._fonts[key]

    def pages(self) -> list[dict[str, Any]]:
        roots = [self.get(t.get("Root")) for t in self.trailers]
        roots += [
            v for v in self.objects.values() if isinstance(v, dict) and v.get("Type") == "Catalog"
        ]
        catalog = next((r for r in reversed(roots) if isinstance(r, dict) and "Pages" in r), None)
        found: list[dict[str, Any]] = []
        seen: set[int] = set()

        def walk(ref: Any, inherited: dict[str, Any], depth: int) -> None:
            # L2: a node is walked once, by identity — an inline kid has no
            # reference to remember, and kids sharing one array were walked
            # exponentially
            node = self.get(ref)
            if depth > 64 or not isinstance(node, dict) or id(node) in seen:
                return
            seen.add(id(node))
            if "Resources" in node:
                inherited = {"Resources": node["Resources"]}
            kids = self.get(node.get("Kids"))
            if isinstance(kids, list):
                self.budget.tick(len(kids))
                for kid in kids:
                    walk(kid, inherited, depth + 1)
            else:
                found.append({**inherited, **node})

        if catalog is not None:
            walk(catalog["Pages"], {}, 0)
        return found


def _cmap(data: bytes, budget: _Budget) -> tuple[dict[int, str], int]:
    """A ToUnicode CMap: code -> text, and the code width in bytes."""
    budget.parse(len(data))  # L1: every use of a CMap is charged
    mapping: dict[int, str] = {}
    space = re.search(rb"begincodespacerange\s*<([0-9A-Fa-f]+)>", data)
    width = max(1, len(space.group(1)) // 2) if space else 1

    def utf16(hexed: bytes) -> str:
        return bytes.fromhex(hexed.decode()).decode("utf-16-be", errors="replace")

    for block in _blocks(data, b"beginbfchar", b"endbfchar"):
        for source, target in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", block):
            budget.tick()
            mapping[int(source, 16)] = utf16(target)
    for block in _blocks(data, b"beginbfrange", b"endbfrange"):
        for low, high, target in re.findall(
            rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]*>|\[[^\][]*\])", block
        ):
            first, last = int(low, 16), int(high, 16)
            budget.tick(min(max(last - first, 0), 65535) + 1)
            if target.startswith(b"["):
                items = re.findall(rb"<([0-9A-Fa-f]*)>", target)
                budget.tick(len(items))
                for offset, item in enumerate(items):
                    mapping[first + offset] = utf16(item)
                continue
            base = bytes.fromhex(target[1:-1].decode()) or b"\0\0"
            start = int.from_bytes(base[-2:], "big")
            count = min(max(last - first, 0), 65535) + 1
            budget.parse(len(base) * count)  # R3: what the range expands to, before it does
            for offset in range(count):
                unit = ((start + offset) & 0xFFFF).to_bytes(2, "big")
                mapping[first + offset] = (base[:-2] + unit).decode("utf-16-be", errors="replace")
    return mapping, width


def _blocks(data: bytes, begin: bytes, end: bytes) -> list[bytes]:
    """Each ``begin``…``end`` block in one pass: an opener with no closer
    stops the search, never a rescan to the end per opener (L2)."""
    found, pos = [], 0
    while (start := data.find(begin, pos)) >= 0:
        stop = data.find(end, start)
        if stop < 0:
            break
        found.append(data[start + len(begin) : stop])
        pos = stop + len(end)
    return found


def _glyph(name: str) -> str | None:
    if name in _GLYPHS:
        return _GLYPHS[name]
    if re.fullmatch(r"uni[0-9A-Fa-f]{4}", name):
        return chr(int(name[3:], 16))
    return name if len(name) == 1 else None


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


class _Font:
    def __init__(self, pdf: _Pdf, ref: Any) -> None:
        font = pdf.get(ref)
        font = font if isinstance(font, dict) else {}
        composite = font.get("Subtype") == "Type0"
        self.width = 2 if composite else 1
        self.map: dict[int, str] = {}
        self.widths: dict[int, float] = {}
        self.default = 500.0
        cmap = pdf.stream(font.get("ToUnicode"))
        if cmap is not None:
            self.map, width = _cmap(cmap, pdf.budget)
            self.width = width if composite else 1
        encoding = pdf.get(font.get("Encoding"))
        base = encoding.get("BaseEncoding") if isinstance(encoding, dict) else encoding
        self.codec = "mac_roman" if base == "MacRomanEncoding" else "cp1252"
        if isinstance(encoding, dict):
            code = 0
            differences = pdf.get(encoding.get("Differences")) or []
            pdf.budget.tick(len(differences))  # L2: a shared array costs every font
            for item in differences:
                if isinstance(item, int):
                    code = item
                elif isinstance(item, _Name):
                    glyph = _glyph(str(item))
                    if glyph is not None:
                        self.map.setdefault(code, glyph)
                    code += 1
        if composite:
            kids = pdf.get(font.get("DescendantFonts"))
            child = pdf.get(kids[0]) if isinstance(kids, list) and kids else None
            child = child if isinstance(child, dict) else {}
            self.default = _number(pdf.get(child.get("DW"))) or 1000.0
            widths = pdf.get(child.get("W")) or []
            pdf.budget.tick(len(widths))
            spec = [pdf.get(v) for v in widths]
            i = 0
            while i + 1 < len(spec):  # [c [w w ...]] or [c_first c_last w]
                if isinstance(spec[i], int) and isinstance(spec[i + 1], list):
                    pdf.budget.tick(len(spec[i + 1]))
                    for offset, w in enumerate(spec[i + 1]):
                        self._set(spec[i] + offset, pdf.get(w))
                    i += 2
                elif (
                    i + 2 < len(spec) and isinstance(spec[i], int) and isinstance(spec[i + 1], int)
                ):
                    pdf.budget.tick(min(max(spec[i + 1] - spec[i], 0), 65535) + 1)
                    for code in range(spec[i], min(spec[i + 1], spec[i] + 65535) + 1):
                        self._set(code, spec[i + 2])
                    i += 3
                else:
                    break
        else:
            first = pdf.get(font.get("FirstChar"))
            widths = pdf.get(font.get("Widths")) or []
            pdf.budget.tick(len(widths))
            for offset, w in enumerate(widths):
                if isinstance(first, int):
                    self._set(first + offset, pdf.get(w))

    def _set(self, code: int, width: Any) -> None:
        if (value := _number(width)) is not None:
            self.widths[code] = value

    def codes(self, raw: bytes) -> list[int]:
        if self.width == 2:
            return [int.from_bytes(raw[i : i + 2], "big") for i in range(0, len(raw) - 1, 2)]
        return list(raw)

    def text(self, code: int) -> str:
        if code in self.map:
            return self.map[code]
        return "" if self.width == 2 else bytes([code]).decode(self.codec, errors="replace")


_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


class _Text:
    """The text state: each string is placed, and where it lands (not which
    operator moved it) decides the space or line break before it."""

    def __init__(self, max_chars: int = MAX_TEXT_CHARS) -> None:
        self.out: list[str] = []
        self.chars = 0
        self.max_chars = max_chars
        self.glyphs = 0
        self.unmapped = 0  # composite-font glyphs with no Unicode mapping
        self.font: _Font | None = None
        self.size = 1.0
        self.leading = 0.0
        self.tm = self.tlm = _IDENTITY
        self.last: tuple[float, float] | None = None

    def move(self, tx: float, ty: float) -> None:
        a, b, c, d, e, f = self.tlm
        self.tm = self.tlm = (a, b, c, d, e + tx * a + ty * c, f + tx * b + ty * d)

    def show(self, items: list[Any]) -> None:
        if self.font is None:
            return
        a, b, c, d, e, f = self.tm
        pieces: list[str] = []
        if self.last is not None:
            if abs(f - self.last[1]) > 0.5 * self.size * (abs(d) or 1.0):
                pieces.append("\n")
            elif abs(e - self.last[0]) > 0.2 * self.size * (abs(a) or 1.0):
                pieces.append(" ")
        advance = 0.0
        for item in items:
            if isinstance(item, bytes):
                for code in self.font.codes(item):
                    self.glyphs += 1
                    text = self.font.text(code)
                    self.unmapped += not text
                    pieces.append(text)
                    advance += self.font.widths.get(code, self.font.default) / 1000 * self.size
                    room = self.max_chars - self.chars - sum(map(len, pieces))
                    if room < 0:  # R3: the cap holds glyph by glyph, never after
                        pieces[-1] = text[: len(text) + room + 1]
                        self.out.extend(pieces)
                        self.chars = self.max_chars + 1
                        raise _Full
            elif (shift := _number(item)) is not None:
                if -shift / 1000 > 0.2:
                    pieces.append(" ")
                advance -= shift / 1000 * self.size
        self.tm = (a, b, c, d, e + advance * a, f + advance * b)
        self.last = (self.tm[4], self.tm[5])
        self.out.extend(pieces)
        self.chars += sum(map(len, pieces))
        if self.chars > self.max_chars:  # K2: the extracted text is bounded too
            raise _Full


def _numbers(operands: list[Any], count: int) -> list[float] | None:
    values = [_number(v) for v in operands[-count:]]
    floats = [v for v in values if v is not None]
    return floats if len(floats) == count else None


def _run(
    pdf: _Pdf,
    content: bytes,
    resources: Any,
    state: _Text,
    depth: int = 0,
    drawing: frozenset[int] = frozenset(),
) -> None:
    """Run one content stream. ``drawing``: the forms on the stack, so a
    form that draws itself (directly or through others) is drawn once (K2)."""
    resources = pdf.get(resources)
    resources = resources if isinstance(resources, dict) else {}
    fonts = pdf.get(resources.get("Font"))
    fonts = fonts if isinstance(fonts, dict) else {}
    xobjects = pdf.get(resources.get("XObject"))
    xobjects = xobjects if isinstance(xobjects, dict) else {}
    parser = _Parser(content, pdf.budget, paid=True)  # L1: the caller charged this use
    operands: list[Any] = []
    while (token := parser.token()) is not None:
        if not isinstance(token, _Op):
            operands.append(token)
            continue
        last = operands[-1] if operands else None
        if token == "BT":
            state.tm = state.tlm = _IDENTITY
        elif token == "Tf" and len(operands) >= 2:
            state.font = pdf.font(fonts.get(str(operands[-2])))
            state.size = abs(_number(last) or 1.0)
        elif token == "TL" and (nums := _numbers(operands, 1)):
            state.leading = nums[0]
        elif token in ("Td", "TD") and (nums := _numbers(operands, 2)):
            if token == "TD":
                state.leading = -nums[1]
            state.move(nums[0], nums[1])
        elif token == "Tm" and (nums := _numbers(operands, 6)):
            state.tm = state.tlm = (nums[0], nums[1], nums[2], nums[3], nums[4], nums[5])
        elif token == "T*":
            state.move(0, -state.leading)
        elif token in ("Tj", "'", '"') and isinstance(last, bytes):
            if token != "Tj":
                state.move(0, -state.leading)
            pdf.budget.tick(len(last))  # L2: one step per shown byte
            state.show([last])
        elif token == "TJ" and isinstance(last, list):
            pdf.budget.tick(sum(len(item) for item in last if isinstance(item, bytes)))
            state.show(last)
        elif token == "ID":  # inline image: skip its raw bytes to EI
            end = re.compile(rb"\sEI(?=\s|$)").search(parser.data, parser.pos)
            parser.pos = len(parser.data) if end is None else end.end()
        elif token == "Do" and depth < MAX_FORM_DEPTH:
            ref = xobjects.get(str(last))
            form = pdf.get(ref)
            if (
                isinstance(ref, _Ref)
                and ref.num not in drawing
                and isinstance(form, dict)
                and form.get("Subtype") == "Form"  # an image is never decoded
            ):
                pdf.budget.form()
                body = pdf.stream(ref)
                if body is not None:
                    pdf.budget.parse(len(body))  # L1: every draw is charged
                    saved = state.tm, state.tlm
                    inner = form.get("Resources") or resources
                    _run(pdf, body, inner, state, depth + 1, drawing | {ref.num})
                    state.tm, state.tlm = saved
        operands = []


def pdf_to_text(data: bytes, *, max_chars: int = MAX_TEXT_CHARS) -> str:
    """The text of a PDF, page by page. Raises ValueError, with what to do
    next, for anything that is not a readable text PDF — or one past the
    per-document budget (K2). Text past ``max_chars`` is not read; the
    result then ends saying so."""
    if b"%PDF-" not in data[:1024]:
        raise ValueError("not a PDF — the response does not start with a PDF header")
    full = False
    try:
        pdf = _Pdf(data)
        if any("Encrypt" in trailer for trailer in pdf.trailers):
            raise ValueError(
                "this PDF is encrypted — skep's built-in reader cannot decrypt it; "
                "download it and open it in a PDF viewer"
            )
        pages = pdf.pages()
        if not pages:
            raise ValueError(
                "found no pages — the PDF is truncated or damaged; try downloading it again"
            )
        state = _Text(max_chars)
        texts = []
        for page in pages:
            contents = page.get("Contents")
            # K11: a reference may name an ARRAY of streams — read in order
            target = pdf.get(contents)
            refs = target if isinstance(target, list) else [contents]
            pdf.budget.tick(len(refs))  # R3: each ref's lookup is work, stream or not
            streams = [s for s in map(pdf.stream, refs) if s is not None]
            # L1: every use is charged, before the join allocates it — one
            # cached stream named N times costs N times
            pdf.budget.parse(sum(map(len, streams)) + len(streams))
            body = b"\n".join(streams)
            state.out, state.last = [], None
            try:
                _run(pdf, body, page.get("Resources"), state)
            except _Full:
                full = True
            lines = (" ".join(line.split()) for line in "".join(state.out).splitlines())
            texts.append("\n".join(line for line in lines if line))
            if full:
                break
    except ValueError:
        raise
    except Exception as exc:  # a malformed file is an answer, never a crash
        raise ValueError(
            f"could not parse this PDF ({type(exc).__name__}) — it is damaged or "
            "uses a structure skep's reader does not support; open it in a PDF viewer"
        ) from exc
    text = "\n\n".join(t for t in texts if t).translate(_LIGATURES)
    if state.unmapped * 2 > state.glyphs:
        raise ValueError(
            "this PDF draws text whose fonts carry no Unicode map (common in CJK "
            "PDFs) — skep's reader cannot recover the characters; open it in a PDF viewer"
        )
    if full:
        return f"{text[:max_chars]}\n\n[the PDF's text continues past {max_chars} characters]"
    if text:
        return text
    raise ValueError(
        "this PDF has no text layer (a scan or images only) — skep's reader does "
        "not OCR; open it in a PDF viewer"
    )


# --- HTML ------------------------------------------------------------------------------

_SKIP = frozenset({"script", "style", "head", "template", "noscript", "nav", "footer"})
_BREAK = frozenset({"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre"})


class _Html(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        self.skip += tag in _SKIP
        if tag in _BREAK:
            self.out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        self.skip -= tag in _SKIP and self.skip > 0
        if tag in _BREAK:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.out.append(data)


def html_to_text(page: str) -> str:
    """Plain text of a page: scripts, styles, navigation and footers dropped."""
    parser = _Html()
    parser.feed(page)
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.out).splitlines())
    return "\n".join(line for line in lines if line)


# --- the tool --------------------------------------------------------------------------


def extract(arguments: dict[str, Any]) -> str:
    """The text of the document in ``arguments`` (K4: given, never fetched)."""
    encoded, given = arguments.get("content_base64"), arguments.get("content")
    if isinstance(encoded, str) and encoded.strip():
        if len(encoded) > (MAX_BYTES // 3 + 1) * 4 + 1024:
            raise ValueError(f"the document is larger than {MAX_BYTES >> 20} MiB — too big here")
        try:
            body = base64.b64decode("".join(encoded.split()), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(
                "content_base64 is not valid base64 — pass the file's bytes base64-encoded, "
                "or pass HTML or text as content"
            ) from exc
    elif isinstance(given, str) and given.strip():
        body = given.encode("utf-8")
    else:
        raise ValueError(
            "extract_document needs content_base64 (a PDF or HTML file's bytes, base64) or "
            "content (HTML or plain text); it has no network — read a URL with read_url"
        )
    if len(body) > MAX_BYTES:
        raise ValueError(f"the document is larger than {MAX_BYTES >> 20} MiB — too big here")
    if b"%PDF-" in body[:1024]:
        return pdf_to_text(body, max_chars=REPLY_CHARS)
    page = body.decode("utf-8", errors="replace")
    text = html_to_text(page) if page.lstrip().startswith("<") else page.strip()
    if len(text) > REPLY_CHARS:
        return f"{text[:REPLY_CHARS]}\n\n[the text continues past {REPLY_CHARS} characters]"
    return text


def _fixture_pdf() -> bytes:
    content = zlib.compress(b"BT /F1 12 Tf 72 720 Td (self test) Tj 0 -14 Td (line two) Tj ET")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R /Resources << /Font << /F1 "
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> >>",
        b"<< /Length %d /Filter /FlateDecode >>\nstream\n%s\nendstream" % (len(content), content),
    ]
    numbered = (b"%d 0 obj\n%s\nendobj\n" % (n, body) for n, body in enumerate(objects, 1))
    return b"%PDF-1.4\n" + b"".join(numbered)  # no xref: objects are found by scanning


def _error(message: str) -> dict[str, Any]:
    return {"content": message, "isError": True}


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "extract_document":
        try:
            return {"content": extract(arguments)}
        except ValueError as exc:
            return _error(str(exc))
    if name == "self_test":
        if pdf_to_text(_fixture_pdf()) != "self test\nline two":
            return _error("PDF extraction broken")
        page = "<nav>menu</nav><h1>Title</h1><p>a &amp; b</p><script>x()</script>"
        if html_to_text(page) != "Title\na & b":
            return _error("HTML extraction broken: " + repr(html_to_text(page)))
        return {"content": "self_test passed: PDF + HTML extraction intact (offline)"}
    known = ", ".join(str(tool["name"]) for tool in TOOLS)
    return _error("no tool named " + repr(name) + "; tools: " + known)


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        method = request.get("method")
        result: dict[str, Any]
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = request.get("params") or {}
            try:
                result = call_tool(str(params.get("name")), params.get("arguments") or {})
            except Exception as exc:  # a tool error is a reply, never a crash
                result = {"content": type(exc).__name__ + ": " + str(exc), "isError": True}
        else:
            result = {}
        print(json.dumps({"jsonrpc": "2.0", "id": request.get("id"), "result": result}), flush=True)


if __name__ == "__main__":
    main()
