"""v29-F1: turn a fetched HTML page into readable plain text (stdlib only).

The governed ``network.read`` capability fetches a page through the same
allowlist gate as ``network.fetch`` and hands the body here. Deliberately
minimal — strip tags, drop script/style, keep block structure as line breaks,
decode entities. A non-HTML body passes through essentially unchanged.

Phase 07 / 7.3: page chrome (navigation, site header/footer, side rails,
cookie banners) is dropped so a capped excerpt carries the body. Landmarks
only, judged by tag/role — deterministic, no scoring. A region that never
closes is malformed markup, not proven chrome, and is kept.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

# Tags whose content is not readable text.
_DROP_CONTENT = frozenset({"script", "style", "template", "noscript", "head"})
# Tags that end a line of readable text.
_BLOCK = frozenset(
    {
        "p",
        "div",
        "br",
        "li",
        "tr",
        "section",
        "article",
        "header",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "ul",
        "ol",
        "table",
        "blockquote",
        "pre",
        "hr",
        "figure",
        "nav",
        "aside",
    }
)


# The body lives in these; chrome tags inside them (an article's own header,
# footer, or aside) are content.
_CONTENT = frozenset({"main", "article"})
# Review R1 (K10): never a chrome region — the page itself, its landmarks,
# and void elements (no end tag: a region they opened would never close).
_NEVER_CHROME = frozenset({"html", "body", "main", "article"})
_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
     "source", "track", "wbr"}
)  # fmt: skip
_COOKIE = re.compile(r"cookie|gdpr|onetrust", re.I)
_BANNER = re.compile(r"banner|bar|notice|popup|modal|dialog|overlay|wall|prompt|consent|sdk", re.I)


def _is_landmark(tag: str, attrs: list[tuple[str, str | None]]) -> bool:
    return tag in _CONTENT or (dict(attrs).get("role") or "").strip().lower() == "main"


class _LandmarkScan(HTMLParser):
    """K10: does the page mark its body? Judged from parsed start tags outside
    ignored regions — a ``<main>`` in a comment, script or template is text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found = False
        self._ignored = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_CONTENT:
            self._ignored += 1
        elif not self._ignored and _is_landmark(tag, attrs):
            self.found = True

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_CONTENT and self._ignored:
            self._ignored -= 1


def _has_landmark(html: str) -> bool:
    scan = _LandmarkScan()
    try:
        scan.feed(html)
        scan.close()
    except Exception:  # a lenient parser rarely raises; judge what it read
        pass
    return scan.found


class _ChromeFilter(HTMLParser):
    """Emits like any extractor, then cuts a chrome region's output when the
    region closes. ``landmark``: the page marks its body (main/article/
    role=main) — only then is a header or aside outside it known chrome."""

    def __init__(self, landmark: bool) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._landmark = landmark
        self._content: list[str | int] | None = None  # [tag, depth]
        # [tag, depth, chunk mark, keep]: keep = a content landmark opened inside
        self._region: list[str | int | bool] | None = None
        self._drop_depth = 0  # inside script/style/template/noscript/head (each extractor)

    def _is_chrome(self, tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        if tag in _NEVER_CHROME or tag in _VOID:
            return False
        found = dict(attrs)
        role = (found.get("role") or "").strip().lower()
        if tag == "nav" or role == "navigation":
            return True
        if self._content is not None:
            return False
        if tag == "footer" or role == "contentinfo":
            return True
        if self._landmark and (tag in ("header", "aside") or role in ("banner", "complementary")):
            return True
        marker = f"{found.get('id') or ''} {found.get('class') or ''}"
        return bool(_COOKIE.search(marker) and _BANNER.search(marker))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._region is not None:
            if tag == self._region[0]:
                self._region[1] = int(self._region[1]) + 1
            # K10: the body is inside — keep it all; review R2 (L9): unless it
            # sits in a hidden region (a template's <main> is not the body)
            if _is_landmark(tag, attrs) and not self._drop_depth:
                self._region[3] = True
        elif self._is_chrome(tag, attrs):
            self._region = [tag, 1, len(self._chunks), False]
        elif self._content is not None:
            if tag == self._content[0]:
                self._content[1] = int(self._content[1]) + 1
        elif _is_landmark(tag, attrs) and not self._drop_depth:
            self._content = [tag, 1]
        self._start(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        self._end(tag)
        if self._region is not None:
            if tag == self._region[0]:
                self._region[1] = int(self._region[1]) - 1
                if self._region[1] == 0:
                    if not self._region[3]:
                        del self._chunks[int(self._region[2]) :]
                    self._region = None
        elif self._content is not None and tag == self._content[0]:
            self._content[1] = int(self._content[1]) - 1
            if self._content[1] == 0:
                self._content = None

    def _start(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raise NotImplementedError  # pragma: no cover - each extractor defines it

    def _end(self, tag: str) -> None:
        raise NotImplementedError  # pragma: no cover - each extractor defines it


class _TextExtractor(_ChromeFilter):
    def __init__(self, landmark: bool) -> None:
        super().__init__(landmark)

    def _start(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_CONTENT:
            self._drop_depth += 1
        elif tag in _BLOCK:
            self._chunks.append("\n")

    def _end(self, tag: str) -> None:
        if tag in _DROP_CONTENT and self._drop_depth > 0:
            self._drop_depth -= 1
        elif tag in _BLOCK:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._drop_depth == 0:
            self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


# v83-F1: markdown heading prefixes.
_MD_HEADING = {"h1": "#", "h2": "##", "h3": "###", "h4": "####", "h5": "#####", "h6": "######"}


class _MarkdownExtractor(_ChromeFilter):
    """v83-F1: like _TextExtractor but keeps document structure — headings,
    links, list items, code fences, blockquotes, table cells — as markdown."""

    def __init__(self, landmark: bool) -> None:
        super().__init__(landmark)
        self._links: list[str] = []

    def _start(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_CONTENT:
            self._drop_depth += 1
            return
        if self._drop_depth:
            return
        if tag in _MD_HEADING:
            self._chunks.append(f"\n\n{_MD_HEADING[tag]} ")
        elif tag == "pre":
            self._chunks.append("\n```\n")
        elif tag == "li":
            self._chunks.append("\n- ")
        elif tag == "blockquote":
            self._chunks.append("\n> ")
        elif tag == "a":
            href = next((value for key, value in attrs if key == "href"), None) or ""
            if href.startswith(("http://", "https://")):
                self._links.append(href)
                self._chunks.append("[")
            else:
                self._links.append("")
        elif tag in ("td", "th"):
            self._chunks.append(" | ")
        elif tag == "br":
            self._chunks.append("\n")
        elif tag in _BLOCK:
            self._chunks.append("\n\n")

    def _end(self, tag: str) -> None:
        if tag in _DROP_CONTENT:
            if self._drop_depth:
                self._drop_depth -= 1
            return
        if self._drop_depth:
            return
        if tag in _MD_HEADING:
            self._chunks.append("\n\n")
        elif tag == "pre":
            self._chunks.append("\n```\n")
        elif tag == "a":
            href = self._links.pop() if self._links else ""
            if href:
                self._chunks.append(f"]({href})")
        elif tag in _BLOCK:
            self._chunks.append("\n\n")

    def handle_data(self, data: str) -> None:
        if not self._drop_depth:
            self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def html_to_markdown(html: str) -> str:
    """Structured markdown from an HTML (or plain) body.

    Headings, links, list items, blockquotes, and table cells survive as
    markdown; ``pre`` blocks become fences with their whitespace intact.
    Same leniency contract as :func:`html_to_text` — never raises.
    """
    parser = _MarkdownExtractor(_has_landmark(html))
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # a lenient parser rarely raises; keep what we got
        pass
    raw = parser.text()
    lines: list[str] = []
    blank_run = 0
    fenced = False
    for line in raw.splitlines():
        if line.strip() == "```":
            fenced = not fenced
            blank_run = 0
            lines.append("```")
            continue
        if fenced:
            lines.append(line.rstrip())
            continue
        collapsed = " ".join(line.split())
        if collapsed:
            blank_run = 0
            lines.append(collapsed)
        else:
            blank_run += 1
            if blank_run == 1:
                lines.append("")
    if fenced:
        lines.append("```")  # never hand back an unclosed fence
    return "\n".join(lines).strip()


def html_to_text(html: str) -> str:
    """Readable text from an HTML (or plain) body.

    Collapses runs of spaces/tabs, keeps at most one blank line between blocks,
    and never allocates beyond the input. Malformed HTML never raises — the
    stdlib parser is lenient and a parse error just yields whatever it read.
    """
    parser = _TextExtractor(_has_landmark(html))
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # a lenient parser rarely raises; keep what we got
        pass
    raw = parser.text()
    lines: list[str] = []
    blank_run = 0
    for line in raw.splitlines():
        collapsed = " ".join(line.split())
        if collapsed:
            blank_run = 0
            lines.append(collapsed)
        else:
            blank_run += 1
            if blank_run == 1:
                lines.append("")
    return "\n".join(lines).strip()
