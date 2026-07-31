from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_landing_demo_shows_remembered_approval_flow() -> None:
    """The landing must demonstrate the real approval UX: gate, remember,
    resume, template — not just claim it."""
    page = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")

    assert "approval needed: shell.run" in page
    assert "approve + remember" in page
    assert "saved template:" in page
    assert "passed [confirmed]" in page


def test_landing_page_names_shipped_adapters_truthfully() -> None:
    """v33 shipped Codex/Aider; the site must not call them planned."""
    index = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    agents = (ROOT / "docs" / "agents.html").read_text(encoding="utf-8")

    for name in ("Claude Code", "Codex", "Aider"):
        assert name in index
        assert name in agents
    assert "planned adapters" not in index
    assert "planned adapters" not in agents


def test_site_wires_live_github_stats() -> None:
    """LAUNCH-2: analytics numbers come live from the GitHub API, never
    hardcoded — the shields.io badge was replaced by data-stat wiring."""
    site_js = (ROOT / "docs" / "site.js").read_text(encoding="utf-8")
    open_source = (ROOT / "docs" / "open-source.html").read_text(encoding="utf-8")

    assert "api.github.com/repos/" in site_js
    assert "stargazers_count" in site_js
    assert "pypi.org/pypi/skep/json" in site_js
    assert 'data-stat="stars"' in open_source
    assert 'data-stat="contributors"' in open_source


def test_site_mobile_layout_contains_scrollable_code_blocks() -> None:
    css = (ROOT / "docs" / "site.css").read_text(encoding="utf-8")

    assert re.search(r"\.codebox pre\s*\{[^}]*overflow-x:\s*auto", css, re.DOTALL)
    assert re.search(r"\.term pre\s*\{[^}]*overflow-x:\s*auto", css, re.DOTALL)


def test_every_site_page_loads_shared_components() -> None:
    """Each page must pull the shared header/footer (site.js) and tokens
    (site.css) — the design system lives in exactly one place."""
    for page in sorted((ROOT / "docs").glob("*.html")):
        text = page.read_text(encoding="utf-8")
        assert 'href="./site.css"' in text, f"{page.name} misses site.css"
        assert 'src="./site.js"' in text, f"{page.name} misses site.js"
