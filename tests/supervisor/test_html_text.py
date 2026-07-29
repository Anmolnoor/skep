"""v29-F1: the HTML→readable-text helper (stdlib parser, no dependency)."""

from __future__ import annotations

from skep.workers.html_text import html_to_text


def test_script_and_style_content_is_dropped() -> None:
    html = """
    <html><head><style>.x{color:red}</style></head>
    <body><script>alert('x')</script><p>Hello world</p></body></html>
    """
    text = html_to_text(html)
    assert "Hello world" in text
    assert "alert" not in text
    assert "color:red" not in text


def test_block_tags_become_line_breaks() -> None:
    html = "<p>First</p><p>Second</p><ul><li>a</li><li>b</li></ul>"
    lines = [line for line in html_to_text(html).splitlines() if line]
    assert lines == ["First", "Second", "a", "b"]


def test_entities_are_decoded() -> None:
    assert html_to_text("<p>Tom &amp; Jerry &lt;3</p>") == "Tom & Jerry <3"


def test_plain_text_passes_through() -> None:
    assert html_to_text("just some plain text") == "just some plain text"


def test_whitespace_is_collapsed_and_blank_lines_bounded() -> None:
    html = "<p>a    b\t\tc</p>\n\n\n\n<p>d</p>"
    text = html_to_text(html)
    assert "a b c" in text
    assert "\n\n\n" not in text  # never more than one blank line


def test_malformed_html_does_not_raise() -> None:
    # Unclosed tags, stray brackets — the lenient parser must not blow up.
    assert "keep" in html_to_text("<div><p>keep <b>this <  unclosed")


def test_research_template_and_docs_describe_governed_reading() -> None:
    """v29-F3: the research run reads via the capability; docs say it's gated."""
    from pathlib import Path

    from skep.supervisor.templates import deep_research_template

    template = deep_research_template(["example.com"])
    # The instructions describe what actually happens: allow-listed hosts are
    # the sources; anything else is refused (the runnable researcher worker
    # gates worker-side, then the sandbox gates again).
    assert "allow-listed hosts" in template.instructions
    assert "refused" in template.instructions
    assert template.network == ("example.com",)  # the browse grant IS the allowlist

    how = (Path(__file__).resolve().parents[2] / "docs" / "how-it-works.md").read_text()
    assert "## Web Reading" in how
    assert "network.read" in how
    assert "network allowlist" in how  # web access == the run's allowlist, gated


def test_html_to_markdown_keeps_structure() -> None:
    """v83-F1: web_extract parity — structure survives as markdown."""
    from skep.workers.html_text import html_to_markdown

    html = (
        "<html><head><script>nope()</script></head><body>"
        "<h2>Section</h2><p>Intro text</p>"
        "<ul><li>one</li><li>two</li></ul>"
        '<a href="https://example.com/doc">the doc</a>'
        "<pre>  code   spacing</pre>"
        "<blockquote>quoted</blockquote>"
        "<table><tr><td>a</td><td>b</td></tr></table>"
        "</body></html>"
    )
    md = html_to_markdown(html)
    assert "## Section" in md
    assert "- one" in md and "- two" in md
    assert "[the doc](https://example.com/doc)" in md
    assert "```" in md and "  code   spacing" in md  # fence keeps whitespace
    assert "> quoted" in md
    assert "a | b" in md
    assert "nope()" not in md


def test_html_to_markdown_never_hands_back_an_open_fence() -> None:
    from skep.workers.html_text import html_to_markdown

    md = html_to_markdown("<pre>left open")
    assert md.count("```") == 2


def test_html_to_markdown_relative_links_stay_plain_text() -> None:
    from skep.workers.html_text import html_to_markdown

    assert html_to_markdown('<a href="/rel">rel</a>') == "rel"
