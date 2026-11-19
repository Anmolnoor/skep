"""docs-site: facts the site prints are checked against the code, not typed twice.

The CLI page names commands; this test holds each one against the argparse
surface. The changelog is generated from the release sections of
``docs/version-history.md``; this test holds those headings against the
release record (what was tagged, released and uploaded, and when).
"""

from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path

from skep.cli import _parser

ROOT = Path(__file__).resolve().parents[2]
CLI_PAGE = ROOT / "site" / "src" / "pages" / "cli.astro"
HISTORY = ROOT / "docs" / "version-history.md"

# A command is named in code: after a "$ " prompt, a backtick, <code>, or at the
# start of a string. Prose such as "skep runs the verify command" is not a command.
NAMED_COMMAND_RE = re.compile(
    r"(?:\$ |`|<code>|[\"'(])skep ([a-z][a-z0-9-]*)(?: +([a-z][a-z0-9-]*))?"
)


def _surface() -> dict[str, set[str]]:
    """``{command: {subcommand, ...}}`` from the real parser."""
    surface: dict[str, set[str]] = {}
    for action in _parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                surface[name] = {
                    child
                    for nested in sub._actions
                    if isinstance(nested, argparse._SubParsersAction)
                    for child in nested.choices
                }
    return surface


def _unknown_commands(text: str, surface: dict[str, set[str]]) -> list[str]:
    unknown = []
    for command, subcommand in NAMED_COMMAND_RE.findall(text):
        if command not in surface:
            unknown.append(f"skep {command}")
        elif surface[command] and subcommand and subcommand not in surface[command]:
            unknown.append(f"skep {command} {subcommand}")
    return unknown


def test_command_scan_catches_a_made_up_command() -> None:
    surface = _surface()
    assert {"run", "review", "policy", "onboard", "doctor"} <= set(surface)
    sample = (
        "`skep policy why` and <code>skep onboard</code>\n"
        "$ skep run ~/app --execution-mode sandbox\n"
        "prose: skep invents nothing\n"
        "`skep teleport` and `skep policy vanish`\n"
    )
    assert _unknown_commands(sample, surface) == ["skep teleport", "skep policy vanish"]


def test_cli_page_names_only_real_commands() -> None:
    unknown = _unknown_commands(CLI_PAGE.read_text(encoding="utf-8"), _surface())
    assert not unknown, f"site/src/pages/cli.astro names commands skep does not have: {unknown}"


# Outside the CLI page a backtick or a quote is usually prose ("skep runs the
# command"), so only a shell prompt or a <code> element counts as naming one.
STRICT_COMMAND_RE = re.compile(
    r"(?:\$ |<code>)skep (?!is\b)([a-z][a-z0-9-]*)(?: +([a-z][a-z0-9-]*))?"
)


def test_no_site_source_names_a_made_up_command() -> None:
    surface = _surface()
    unknown = []
    for path in sorted((ROOT / "site" / "src").rglob("*")):
        if path.suffix not in {".astro", ".tsx", ".jsx", ".ts", ".mjs"} or ".test." in path.name:
            continue
        for command, subcommand in STRICT_COMMAND_RE.findall(path.read_text(encoding="utf-8")):
            if command not in surface:
                unknown.append(f"{path.relative_to(ROOT)}: skep {command}")
            elif surface[command] and subcommand and subcommand not in surface[command]:
                unknown.append(f"{path.relative_to(ROOT)}: skep {command} {subcommand}")
    assert not unknown, "site sources name commands skep does not have:\n" + "\n".join(unknown)


def test_cli_page_covers_every_top_level_command() -> None:
    """The other direction: a command added to skep must reach the page. The
    page was written at 1.4.2 and main gained `memory add` the same day."""
    page = CLI_PAGE.read_text(encoding="utf-8")
    missing = sorted(name for name in _surface() if f"skep {name}" not in page)
    assert not missing, f"site/src/pages/cli.astro does not mention: {missing}"


def test_current_version_has_a_release_section() -> None:
    """The changelog is generated: a release with no section is a release the
    site does not show."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = pyproject["project"]["version"]
    history = HISTORY.read_text(encoding="utf-8")
    assert re.search(
        rf"^## v{re.escape(version)} — .+ \(\d{{4}}-\d{{2}}-\d{{2}}\)$", history, re.M
    ), f"docs/version-history.md has no `## v{version} — title (date)` section"


def test_release_headings_match_the_release_record() -> None:
    """1.2.1, 1.3.0 and 1.4.0 were never published; their work shipped in
    1.4.1. 1.2.0 was published 2026-11-09 and 1.4.1 on 2026-11-16 (the dates
    of the tag, the GitHub release and the PyPI upload, not of the commit)."""
    headings = [
        line for line in HISTORY.read_text(encoding="utf-8").splitlines() if line.startswith("## v")
    ]

    def heading(version: str) -> str:
        matches = [line for line in headings if line.startswith(f"## v{version} — ")]
        assert len(matches) == 1, f"expected one release section for v{version}: {matches}"
        return matches[0]

    for unpublished in ("1.2.1", "1.3.0", "1.4.0"):
        assert heading(unpublished).endswith(", never published; shipped in v1.4.1)")
    assert heading("1.2.0").endswith("(2026-11-09)")
    assert heading("1.4.1").endswith("(2026-11-16)")
    assert heading("1.1.0").endswith("(2026-10-22)")
    assert heading("1.4.2").endswith("(2026-11-18)")
