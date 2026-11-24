"""Tiny in-test PDF writer: real PDF structure (objects, streams, xref,
trailer), built from source so no binary fixture ships in the repo."""

from __future__ import annotations

import zlib


def stream(content: bytes, extra: str = "", *, flate: bool = True) -> bytes:
    body = zlib.compress(content) if flate else content
    filters = "/Filter /FlateDecode " if flate else ""
    head = f"<< {filters}/Length {len(body)} {extra}>>\nstream\n".encode()
    return head + body + b"\nendstream"


def build(objects: list[bytes], *, root: int = 1, trailer_extra: str = "") -> bytes:
    """``objects[i]`` becomes object ``i + 1``."""
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root {root} 0 R {trailer_extra}>>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


HELVETICA = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"


def simple_pdf(*pages: bytes, font: bytes = HELVETICA) -> bytes:
    """One page per content stream, all sharing font /F1 (object 3)."""
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(pages)))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode(),
        font,
    ]
    for i, content in enumerate(pages):
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>".encode()
        )
        objects.append(stream(content))
    return build(objects)
