"""Phase 07 / 7.3: read_url's char cap carries the page body, not its menus.

The fixtures under tests/fixtures/html are small hand-written pages shaped
like a docs site, a news article and a GitHub issue page."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from skep.workers.html_text import html_to_markdown, html_to_text

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "html"

# (fixture, body text that must survive, boilerplate that must go)
CASES = [
    (
        "docs_page.html",
        [
            "# Installing widgetkit",
            "pip install widgetkit",
            "## Configuration file",
            "Note: the config file is safe to commit.",  # an aside INSIDE the article stays
        ],
        [
            "Search docs",
            "Getting started",
            "Configuration reference",
            "Edit this page on GitHub",
            "Copyright 2026",
            "We use cookies",
            "On this page",  # in-article nav is still a nav
        ],
    ),
    (
        "news_article.html",
        [
            "# Town opens new library after a decade of planning",  # article header stays
            "By A. Reporter",
            "40,000 books",
            "This is a place for everyone",
            "Correction: an earlier version",  # article footer stays
        ],
        ["Politics", "Subscribe", "Most read", "Celebrity", "Advertise", "Accept Cookies"],
    ),
    (
        "github_issue.html",
        [
            "# read_url drops the issue body #123",
            "octo-dev commented",
            "Steps to reproduce: call read_url on any issue URL.",
            "Confirmed. The fix should strip the global navigation.",
        ],
        ["Pricing", "Sign up", "Pull requests 7", "Insights", "githubstatus", "optional cookies"],
    ),
]


@pytest.mark.parametrize(("fixture", "keep", "drop"), CASES)
def test_markdown_keeps_the_body_and_drops_the_chrome(
    fixture: str, keep: list[str], drop: list[str]
) -> None:
    text = html_to_markdown((FIXTURES / fixture).read_text(encoding="utf-8"))
    for wanted in keep:
        assert wanted in text, (wanted, text)
    for unwanted in drop:
        assert unwanted not in text, (unwanted, text)


@pytest.mark.parametrize(("fixture", "keep", "drop"), CASES)
def test_flat_text_drops_the_same_chrome(fixture: str, keep: list[str], drop: list[str]) -> None:
    text = html_to_text((FIXTURES / fixture).read_text(encoding="utf-8"))
    for wanted in keep:
        assert wanted.lstrip("# ") in text, (wanted, text)
    for unwanted in drop:
        assert unwanted not in text, (unwanted, text)


def test_the_body_now_fits_the_card_lane_cap() -> None:
    """The point of the change: the first chars of the excerpt are content."""
    text = html_to_markdown((FIXTURES / "github_issue.html").read_text(encoding="utf-8"))
    assert text.index("read_url drops the issue body") < 80


def test_without_a_main_landmark_only_nav_footer_and_cookie_banners_go() -> None:
    """No <main>/<article>: a <header> or <aside> may be the content, so only
    the unambiguous chrome is dropped."""
    html = (
        "<header><h1>Post title</h1></header>"
        "<nav><a href='/'>Home</a></nav>"
        "<aside><p>Key point of the post</p></aside>"
        "<div class='content'><p>Body text</p></div>"
        "<div class='gdpr-notice'>Consent please</div>"
        "<footer>Site footer</footer>"
    )
    text = html_to_markdown(html)
    assert "# Post title" in text and "Key point of the post" in text and "Body text" in text
    assert "Home" not in text and "Consent please" not in text and "Site footer" not in text


def test_a_section_about_cookies_is_content_not_a_banner() -> None:
    html = (
        "<div id='cookies'><h2>Cookies</h2><p>Set-Cookie takes attributes.</p></div>"
        "<section class='cookie-attributes'><p>SameSite limits sends.</p></section>"
    )
    text = html_to_markdown(html)
    assert "Set-Cookie takes attributes." in text and "SameSite limits sends." in text


def test_an_unclosed_boilerplate_region_is_not_proof_and_is_kept() -> None:
    """A region that never closes is malformed markup, not proven chrome —
    dropping it would drop everything after it."""
    html = "<body><footer><p>Body that a broken page left inside an open footer"
    assert "Body that a broken page" in html_to_markdown(html)
    assert "Body that a broken page" in html_to_text(html)


def test_role_landmarks_count_like_their_tags() -> None:
    html = (
        "<div role='banner'>Brand bar</div>"
        "<div role='navigation'><a href='/a'>Menu item</a></div>"
        "<div role='main'><p>Main body</p><div role='complementary'>Inline note</div></div>"
        "<div role='complementary'>Side rail</div>"
        "<div role='contentinfo'>Legal footer</div>"
    )
    text = html_to_markdown(html)
    assert "Main body" in text and "Inline note" in text
    for gone in ("Brand bar", "Menu item", "Side rail", "Legal footer"):
        assert gone not in text


def test_nested_same_tag_regions_close_at_the_right_end() -> None:
    html = (
        "<main><p>Lead</p></main>"
        "<aside><aside>inner</aside>outer tail</aside>"
        "<p>After the aside</p>"
    )
    text = html_to_markdown(html)
    assert "Lead" in text and "After the aside" in text
    assert "inner" not in text and "outer tail" not in text


# --- review R1 (K10): chrome is a region, never the page itself ------------


@pytest.mark.parametrize("convert", [html_to_text, html_to_markdown])
def test_a_cookie_class_on_the_page_wrapper_never_drops_the_page(convert: Any) -> None:
    for wrapper in (
        '<html class="cookie-consent-banner-shown"><body><p>Body text</p></body></html>',
        '<body class="cookie-notice-hidden"><p>Body text</p></body>',
        '<main class="cookie-banner-offset"><p>Body text</p></main>',
        '<article id="gdpr-notice-wrapper"><p>Body text</p></article>',
        '<div id="cookie-consent-wrapper"><main><p>Body text</p></main></div>',
    ):
        assert "Body text" in convert(wrapper), wrapper


@pytest.mark.parametrize("convert", [html_to_text, html_to_markdown])
def test_a_void_element_never_opens_a_region(convert: Any) -> None:
    page = (
        '<head><link id="cookie-notice-css" rel="stylesheet"></head>'
        '<body><img class="cookie-banner-icon" src="x.png">'
        "<nav>Site menu</nav><main><p>Body text</p></main><footer>Legal</footer></body>"
    )
    text = convert(page)
    assert "Body text" in text
    assert "Site menu" not in text and "Legal" not in text  # chrome after it still strips


@pytest.mark.parametrize("convert", [html_to_text, html_to_markdown])
def test_role_main_is_case_insensitive(convert: Any) -> None:
    page = '<header>Site banner</header><div role="MAIN"><header>Post title</header>Body</div>'
    text = convert(page)
    assert "Post title" in text and "Body" in text and "Site banner" not in text


@pytest.mark.parametrize("convert", [html_to_text, html_to_markdown])
def test_a_landmark_inside_a_comment_or_script_is_not_a_landmark(convert: Any) -> None:
    """With no real landmark, a header or aside is not proven chrome."""
    for hidden in ("<!-- <main> -->", "<script>let t = '<main>'</script>",
                   "<template><main>t</main></template>"):  # fmt: skip
        page = f"{hidden}<header>Real heading</header><aside>Real note</aside><p>Body</p>"
        text = convert(page)
        assert "Real heading" in text and "Real note" in text, hidden


@pytest.mark.parametrize("convert", [html_to_text, html_to_markdown])
def test_a_landmark_in_a_hidden_region_never_keeps_the_chrome_around_it(convert: Any) -> None:
    """L9: a <main> inside a <template> (or noscript) in the nav kept the nav."""
    for hidden in (
        "<template><main>t</main></template>",
        "<noscript><article>n</article></noscript>",
    ):
        page = f"<nav>Site menu {hidden}</nav><main><p>Body text</p></main>"
        text = convert(page)
        assert "Body text" in text and "Site menu" not in text, hidden
