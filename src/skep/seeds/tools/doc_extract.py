"""doc_extract — read a PDF or HTML document from a URL as plain text.

Phase 07 / 7.3: a shipped seed tool in the forge's single-file stdio MCP
format (stdlib only, JSON-RPC over stdio, self_test offline). Read-only;
it reads public addresses only and its sandbox decides which hosts it may
reach. The PDF reader here is also the one skep's read_url and researcher
use (``pdf_to_text``), so there is one PDF path, not three.

The PDF reader handles what text PDFs are made of: objects found by
scanning (no xref needed, so a damaged table still reads), Flate/ASCIIHex/
ASCII85 streams, PDF 1.5 object streams, simple fonts (WinAnsi/MacRoman,
/Differences) and composite fonts through their ToUnicode CMap, Form
XObjects, and word/line breaks placed by where each string lands. It does
NOT decrypt (an encrypted PDF needs a cipher stdlib lacks), OCR a scan, or
map CJK fonts that ship no ToUnicode — each of those says so.
"""

import base64
import ipaddress
import json
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
import zlib
from html.parser import HTMLParser
from typing import Any, NamedTuple

MAX_BYTES = 8 * 1024 * 1024
TIMEOUT_SECONDS = 20

TOOLS: list[dict[str, Any]] = [
    {
        "name": "extract_document",
        "description": "Read a PDF or HTML document from a URL as plain text. "
        "Arguments: {url: string} — a public http(s) address. Example: "
        "{name: extract_document, arguments: {url: https://example.com/report.pdf}}. "
        "Read-only. Encrypted or scanned (image-only) PDFs are refused with "
        "a reason; it does not OCR.",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "self_test",
        "description": "Zero-argument offline self-check; extracts a built-in PDF "
        "and HTML fixture.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# --- fetch errors that say what to do next --------------------------------------------


def http_error_message(status: int, reason: str, retry_after: str | None = None) -> str:
    """One line naming the HTTP failure and the next step (I9)."""
    head = f"HTTP {status} {reason}".rstrip()
    wait = (retry_after or "").strip()
    hint = f"wait {wait}s" if wait.isdigit() else f"wait until {wait} (Retry-After)" if wait else ""
    if status == 429:
        return (
            f"{head} — the site is rate-limiting this reader; "
            f"{hint or 'wait a minute'} before trying again"
        )
    if status in (404, 410):
        return (
            f"{head} — nothing at this URL; check it for typos or find the page's current address"
        )
    if status in (401, 403):
        return (
            f"{head} — the site refuses this reader (login wall or bot block); use another source"
        )
    if status >= 500:
        return f"{head} — the site is failing on its side; try again later" + (
            f" ({hint})" if hint else ""
        )
    return f"{head} — the site rejected the request; check the URL or use another source"


# --- PDF -------------------------------------------------------------------------------

_WS = b" \t\r\n\f\x00"
_DELIM = b"()<>[]{}/%"
_NUM = re.compile(rb"[+-]?(?:\d+\.?\d*|\.\d+)")
_OBJ = re.compile(rb"(\d+)\s+(\d+)\s+obj\b")
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


class _Parser:
    """Tokenizer for PDF objects and content streams: composites come back
    built (dict, list), ``n g R`` as a _Ref, anything else bare as an _Op."""

    def __init__(self, data: bytes, pos: int = 0) -> None:
        self.data = data
        self.pos = pos

    def skip(self) -> None:
        data, end = self.data, len(self.data)
        while self.pos < end:
            if data[self.pos] in _WS:
                self.pos += 1
            elif data[self.pos] == 0x25:  # % comment
                newline = data.find(b"\n", self.pos)
                self.pos = end if newline < 0 else newline + 1
            else:
                return

    def _word_end(self, pos: int) -> int:
        while pos < len(self.data) and self.data[pos] not in _WS + _DELIM:
            pos += 1
        return pos

    def token(self) -> Any:
        self.skip()
        data = self.data
        if self.pos >= len(data):
            return None
        char = data[self.pos : self.pos + 1]
        if char == b"/":
            end = self._word_end(self.pos + 1)
            raw = re.sub(
                rb"#([0-9A-Fa-f]{2})",
                lambda m: bytes([int(m.group(1), 16)]),
                data[self.pos + 1 : end],
            )
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
            end = data.find(b">", self.pos)
            end = len(data) if end < 0 else end
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
        number = _NUM.match(data, self.pos)
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
        gen = re.compile(rb"\d+").match(self.data, self.pos)
        if gen:
            self.pos = gen.end()
            self.skip()
            if self.data.startswith(b"R", self.pos) and self._word_end(self.pos) == self.pos + 1:
                self.pos += 1
                return _Ref(value, int(gen.group()))
        self.pos = save
        return value

    def _literal(self) -> bytes:
        data, end = self.data, len(self.data)
        self.pos += 1
        depth, out = 1, bytearray()
        while self.pos < end:
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


def _decode(meta: Any, raw: bytes) -> bytes | None:
    """A stream's bytes through its filters; None for codecs that hold no
    text (images, LZW) or a broken stream."""
    meta = meta if isinstance(meta, dict) else {}
    filters = meta.get("Filter")
    data = raw
    for name in filters if isinstance(filters, list) else [filters] if filters else []:
        if name in ("FlateDecode", "Fl"):
            try:
                data = zlib.decompressobj().decompress(data)
            except zlib.error:
                return None
        elif name in ("ASCIIHexDecode", "AHx"):
            digits = re.sub(rb"[^0-9A-Fa-f]", b"", data.split(b">")[0])
            data = bytes.fromhex((digits + b"0" * (len(digits) % 2)).decode())
        elif name in ("ASCII85Decode", "A85"):
            body = data.strip().removeprefix(b"<~").split(b"~>")[0]
            try:
                data = base64.a85decode(re.sub(rb"\s", b"", body))
            except ValueError:
                return None
        else:
            return None
    return data


class _Pdf:
    def __init__(self, data: bytes) -> None:
        self.objects: dict[int, Any] = {}
        self.streams: dict[int, bytes] = {}
        for match in _OBJ.finditer(data):
            parser = _Parser(data, match.end())
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
                end = start + length if isinstance(length, int) else -1
                if end < 0 or not data[end : end + 12].lstrip().startswith(b"endstream"):
                    found = data.find(b"endstream", start)
                    end = len(data) if found < 0 else found
                self.streams[num] = data[start:end]
        for num, value in list(self.objects.items()):
            if isinstance(value, dict) and value.get("Type") == "ObjStm":
                self._unpack(value, num)
        self.trailers = [
            value for value in self.objects.values() if isinstance(value, dict) and "Root" in value
        ]
        for match in re.finditer(rb"trailer\s*<<", data):
            trailer = _Parser(data, match.end() - 2).token()
            if isinstance(trailer, dict):
                self.trailers.append(trailer)

    def _unpack(self, meta: dict[str, Any], num: int) -> None:
        body = _decode(meta, self.streams.get(num, b""))
        first, count = meta.get("First"), meta.get("N")
        if body is None or not isinstance(first, int) or not isinstance(count, int):
            return
        header = _Parser(body)
        pairs = [(header.token(), header.token()) for _ in range(count)]
        for packed, offset in pairs:
            if isinstance(packed, int) and isinstance(offset, int):
                self.objects.setdefault(packed, _Parser(body, first + offset).token())

    def get(self, value: Any) -> Any:
        for _ in range(32):  # a reference chain, never a cycle
            if not isinstance(value, _Ref):
                return value
            value = self.objects.get(value.num)
        return None

    def stream(self, ref: Any) -> bytes | None:
        if not isinstance(ref, _Ref) or ref.num not in self.streams:
            return None
        return _decode(self.objects.get(ref.num), self.streams[ref.num])

    def pages(self) -> list[dict[str, Any]]:
        roots = [self.get(t.get("Root")) for t in self.trailers]
        roots += [
            v for v in self.objects.values() if isinstance(v, dict) and v.get("Type") == "Catalog"
        ]
        catalog = next((r for r in reversed(roots) if isinstance(r, dict) and "Pages" in r), None)
        found: list[dict[str, Any]] = []
        seen: set[int] = set()

        def walk(ref: Any, inherited: dict[str, Any], depth: int) -> None:
            if depth > 64 or (isinstance(ref, _Ref) and ref.num in seen):
                return
            if isinstance(ref, _Ref):
                seen.add(ref.num)
            node = self.get(ref)
            if not isinstance(node, dict):
                return
            if "Resources" in node:
                inherited = {"Resources": node["Resources"]}
            kids = self.get(node.get("Kids"))
            if isinstance(kids, list):
                for kid in kids:
                    walk(kid, inherited, depth + 1)
            else:
                found.append({**inherited, **node})

        if catalog is not None:
            walk(catalog["Pages"], {}, 0)
        return found


def _cmap(data: bytes) -> tuple[dict[int, str], int]:
    """A ToUnicode CMap: code -> text, and the code width in bytes."""
    mapping: dict[int, str] = {}
    space = re.search(rb"begincodespacerange\s*<([0-9A-Fa-f]+)>", data)
    width = max(1, len(space.group(1)) // 2) if space else 1

    def utf16(hexed: bytes) -> str:
        return bytes.fromhex(hexed.decode()).decode("utf-16-be", errors="replace")

    for block in re.findall(rb"beginbfchar(.*?)endbfchar", data, re.S):
        for source, target in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", block):
            mapping[int(source, 16)] = utf16(target)
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", data, re.S):
        for low, high, target in re.findall(
            rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]*>|\[[^\]]*\])", block
        ):
            first, last = int(low, 16), int(high, 16)
            if target.startswith(b"["):
                for offset, item in enumerate(re.findall(rb"<([0-9A-Fa-f]*)>", target)):
                    mapping[first + offset] = utf16(item)
                continue
            base = bytes.fromhex(target[1:-1].decode()) or b"\0\0"
            start = int.from_bytes(base[-2:], "big")
            for offset in range(min(last - first, 65535) + 1):
                unit = ((start + offset) & 0xFFFF).to_bytes(2, "big")
                mapping[first + offset] = (base[:-2] + unit).decode("utf-16-be", errors="replace")
    return mapping, width


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
            self.map, width = _cmap(cmap)
            self.width = width if composite else 1
        encoding = pdf.get(font.get("Encoding"))
        base = encoding.get("BaseEncoding") if isinstance(encoding, dict) else encoding
        self.codec = "mac_roman" if base == "MacRomanEncoding" else "cp1252"
        if isinstance(encoding, dict):
            code = 0
            for item in pdf.get(encoding.get("Differences")) or []:
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
            spec = [pdf.get(v) for v in pdf.get(child.get("W")) or []]
            i = 0
            while i + 1 < len(spec):  # [c [w w ...]] or [c_first c_last w]
                if isinstance(spec[i], int) and isinstance(spec[i + 1], list):
                    for offset, w in enumerate(spec[i + 1]):
                        self._set(spec[i] + offset, pdf.get(w))
                    i += 2
                elif (
                    i + 2 < len(spec) and isinstance(spec[i], int) and isinstance(spec[i + 1], int)
                ):
                    for code in range(spec[i], min(spec[i + 1], spec[i] + 65535) + 1):
                        self._set(code, spec[i + 2])
                    i += 3
                else:
                    break
        else:
            first = pdf.get(font.get("FirstChar"))
            for offset, w in enumerate(pdf.get(font.get("Widths")) or []):
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

    def __init__(self) -> None:
        self.out: list[str] = []
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
        if self.last is not None:
            if abs(f - self.last[1]) > 0.5 * self.size * (abs(d) or 1.0):
                self.out.append("\n")
            elif abs(e - self.last[0]) > 0.2 * self.size * (abs(a) or 1.0):
                self.out.append(" ")
        advance = 0.0
        for item in items:
            if isinstance(item, bytes):
                for code in self.font.codes(item):
                    self.glyphs += 1
                    text = self.font.text(code)
                    self.unmapped += not text
                    self.out.append(text)
                    advance += self.font.widths.get(code, self.font.default) / 1000 * self.size
            elif (shift := _number(item)) is not None:
                if -shift / 1000 > 0.2:
                    self.out.append(" ")
                advance -= shift / 1000 * self.size
        self.tm = (a, b, c, d, e + advance * a, f + advance * b)
        self.last = (self.tm[4], self.tm[5])


def _numbers(operands: list[Any], count: int) -> list[float] | None:
    values = [_number(v) for v in operands[-count:]]
    floats = [v for v in values if v is not None]
    return floats if len(floats) == count else None


def _run(pdf: _Pdf, content: bytes, resources: Any, state: _Text, depth: int = 0) -> None:
    resources = pdf.get(resources)
    resources = resources if isinstance(resources, dict) else {}
    fonts = pdf.get(resources.get("Font"))
    fonts = fonts if isinstance(fonts, dict) else {}
    xobjects = pdf.get(resources.get("XObject"))
    xobjects = xobjects if isinstance(xobjects, dict) else {}
    cache: dict[str, _Font] = {}
    parser = _Parser(content)
    operands: list[Any] = []
    while (token := parser.token()) is not None:
        if not isinstance(token, _Op):
            operands.append(token)
            continue
        last = operands[-1] if operands else None
        if token == "BT":
            state.tm = state.tlm = _IDENTITY
        elif token == "Tf" and len(operands) >= 2:
            key = str(operands[-2])
            if key not in cache:
                cache[key] = _Font(pdf, fonts.get(key))
            state.font = cache[key]
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
            state.show([last])
        elif token == "TJ" and isinstance(last, list):
            state.show(last)
        elif token == "ID":  # inline image: skip its raw bytes to EI
            end = re.compile(rb"\sEI(?=\s|$)").search(parser.data, parser.pos)
            parser.pos = len(parser.data) if end is None else end.end()
        elif token == "Do" and depth < 8:
            ref = xobjects.get(str(last))
            form = pdf.get(ref)
            body = pdf.stream(ref)
            if isinstance(form, dict) and form.get("Subtype") == "Form" and body is not None:
                saved = state.tm, state.tlm
                _run(pdf, body, form.get("Resources") or resources, state, depth + 1)
                state.tm, state.tlm = saved
        operands = []


def pdf_to_text(data: bytes) -> str:
    """The text of a PDF, page by page. Raises ValueError, with what to do
    next, for anything that is not a readable text PDF."""
    if b"%PDF-" not in data[:1024]:
        raise ValueError("not a PDF — the response does not start with a PDF header")
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
        state = _Text()
        texts = []
        for page in pages:
            contents = page.get("Contents")
            refs = pdf.get(contents) if not isinstance(contents, _Ref) else [contents]
            body = b"\n".join(s for s in map(pdf.stream, refs or []) if s is not None)
            state.out, state.last = [], None
            _run(pdf, body, page.get("Resources"), state)
            lines = (" ".join(line.split()) for line in "".join(state.out).splitlines())
            texts.append("\n".join(line for line in lines if line))
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


def _refuse_non_public(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise ValueError(f"{host!r} does not resolve ({exc}) — check the address") from exc
    for info in infos:
        address = str(info[4][0])
        if not ipaddress.ip_address(address.split("%")[0]).is_global:
            raise ValueError(
                f"extract_document only reads public internet addresses: {host!r} is "
                f"{address} — open that document yourself"
            )


class _Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Any:
        _refuse_non_public(urllib.parse.urlparse(newurl).hostname or "")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch(url: str) -> tuple[str, bytes]:
    """(content type, body) of a public http(s) URL."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"extract_document reads http(s) URLs, got {url!r}")
    _refuse_non_public(parsed.hostname)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 skep-doc-extract"})
    opener = urllib.request.build_opener(_Redirects)
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read(MAX_BYTES + 1)
            kind = (
                response.headers.get_content_type() if response.headers.get("Content-Type") else ""
            )
    except urllib.error.HTTPError as exc:
        raise ValueError(
            http_error_message(exc.code, exc.reason, exc.headers.get("Retry-After"))
        ) from exc
    except TimeoutError as exc:
        raise ValueError(
            f"timed out after {TIMEOUT_SECONDS}s reading {parsed.hostname} — the site is "
            "slow or unreachable; try again later"
        ) from exc
    except urllib.error.URLError as exc:
        raise ValueError(
            f"could not reach {parsed.hostname} ({exc.reason}) — check the address; if it "
            "is right, this tool's sandbox may not allow that host"
        ) from exc
    if len(body) > MAX_BYTES:
        raise ValueError(
            f"the document is larger than {MAX_BYTES // (1024 * 1024)} MiB — "
            "too big to read here; download it yourself"
        )
    return kind, body


def extract(url: str) -> str:
    kind, body = _fetch(url)
    if kind == "application/pdf" or body.startswith(b"%PDF-"):
        return pdf_to_text(body)
    text = body.decode("utf-8", errors="replace")
    if kind in ("text/html", "application/xhtml+xml") or (
        not kind and text.lstrip().startswith("<")
    ):
        return html_to_text(text)
    if not kind or kind.startswith("text/"):
        return text.strip()
    raise ValueError(
        f"content type {kind} is not a document — extract_document reads PDF, HTML and plain text"
    )


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
        url = str(arguments.get("url") or "").strip()
        if not url:
            return _error(
                "extract_document needs a url argument — pass the http(s) address "
                "of a PDF or HTML page"
            )
        try:
            return {"content": extract(url)}
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
