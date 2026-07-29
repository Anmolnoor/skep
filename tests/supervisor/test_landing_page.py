from __future__ import annotations

import html
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _landing_demo_transcript() -> str:
    page = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    match = re.search(
        r'<ol class="terminal-lines" id="terminal-lines">(.*?)</ol>',
        page,
        re.DOTALL,
    )
    assert match is not None
    return html.unescape(re.sub(r"<[^>]+>", " ", match.group(1)))


def test_landing_demo_transcript_shows_remembered_approval_flow() -> None:
    transcript = " ".join(_landing_demo_transcript().split())

    assert "approve + remember" in transcript
    assert "saved template:" in transcript
    assert "matched template: health-endpoint (shell: python -m pytest)" in transcript
    assert "without new approvals" in transcript
    assert transcript.count("skep run") >= 2


def test_landing_page_names_shipped_adapters_truthfully() -> None:
    """v33 shipped Codex/Aider; the landing page must not call them planned."""
    page = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")

    assert "Codex and Aider" in page
    assert "planned adapters" not in page
    assert "skep.workers.codex" in page


def test_landing_page_hero_includes_github_stars_badge() -> None:
    page = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")

    assert "GitHub stars" in page
    assert "img.shields.io/github/stars/Anmolnoor/skep" in page


def test_landing_page_mobile_layout_contains_scrollable_code_blocks() -> None:
    css = (ROOT / "docs" / "landing.css").read_text(encoding="utf-8")

    assert re.search(r"\.command-block\s*\{[^}]*min-width:\s*0;", css, re.DOTALL)
    assert "pre {" in css
    assert "overflow-x: auto;" in css
