"""v29-F1: turn a fetched HTML page into readable plain text (stdlib only).

The governed ``network.read`` capability fetches a page through the same
allowlist gate as ``network.fetch`` and hands the body here. Deliberately
minimal — strip tags, drop script/style, keep block structure as line breaks,
decode entities. A non-HTML body passes through essentially unchanged.
"""

from __future__ import annotations

from html.parser import HTMLParser

# Tags whose content is not readable text.
_DROP_CONTENT = frozenset({"script", "style", "template", "noscript", "head"})
# Tags that end a line of readable text.
_BLOCK = frozenset(
    {
        "p", "div", "br", "li", "tr", "section", "article", "header", "footer",
        "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table", "blockquote",
        "pre", "hr", "figure", "nav", "aside",
    }
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._drop_depth = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in _DROP_CONTENT:
            self._drop_depth += 1
        elif tag in _BLOCK:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
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


class _MarkdownExtractor(HTMLParser):
    """v83-F1: like _TextExtractor but keeps document structure — headings,
    links, list items, code fences, blockquotes, table cells — as markdown."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._drop_depth = 0
        self._links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
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

    def handle_endtag(self, tag: str) -> None:
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
    parser = _MarkdownExtractor()
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
    parser = _TextExtractor()
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
