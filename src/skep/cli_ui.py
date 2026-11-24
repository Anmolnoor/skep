"""Phase 10 10.2: the terminal's presentation layer (``rich``, TTY only).

Callers branch on ``rich_enabled()`` first and keep their plain output as the
other arm, byte for byte: a pipe, a file, CI, ``NO_COLOR`` and every
``--json``/``--md`` flag get no ANSI and no layout change, so scripts and the
output-pinning tests read what they always read. ``rich`` is imported inside
the renderers only — building the parser (``skep --help``) never loads it.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, TextIO

if TYPE_CHECKING:
    from rich.console import Console
    from rich.text import Text


def rich_enabled(stream: TextIO | None = None) -> bool:
    """True only on a terminal with NO_COLOR unset."""
    stream = sys.stdout if stream is None else stream
    return stream.isatty() and not os.environ.get("NO_COLOR")


def console() -> Console:
    from rich.console import Console

    return Console(highlight=False)


# ALLOW / ASK / DENY / TAMPER, plus the review card's G10 words.
_BADGES = {
    "allow": ("ALLOW", "bold green"),
    "require_approval": ("ASK", "bold yellow"),
    "deny": ("DENY", "bold red"),
    "tampered": ("TAMPER", "bold white on red"),
}


def badge(verdict: str) -> Text:
    from rich.text import Text

    label, style = _BADGES.get(verdict, (verdict.upper(), "bold"))
    return Text(label, style=style)


def print_table(columns: Sequence[str], rows: Sequence[Sequence[Any]], title: str = "") -> None:
    from rich.table import Table

    table = Table(*columns, title=title or None, header_style="bold", expand=False)
    for row in rows:
        table.add_row(*row)
    console().print(table)


def print_panel(text: str, *, title: str, style: str) -> None:
    """A bordered card: the doctor report, a review card. Never truncated (I8):
    rich wraps an over-wide line."""
    from rich.panel import Panel
    from rich.text import Text

    console().print(Panel(Text(text), title=title, border_style=style, title_align="left"))


def print_diff(patch: str) -> None:
    from rich.syntax import Syntax

    console().print(Syntax(patch, "diff", theme="ansi_dark", word_wrap=True))


def review_ansi(head: str, patch: str, tail: str) -> str:
    """``skep review <id>`` on a terminal: the evidence, the patch lit as a
    diff, then the next step — rendered to ANSI text for the pager."""
    from rich.syntax import Syntax
    from rich.text import Text

    con = console()
    with con.capture() as captured:
        con.print(Text(head))
        con.print(Syntax(patch, "diff", theme="ansi_dark", word_wrap=True))
        con.print(Text(tail))
    return captured.get()


def page_ansi(text: str) -> None:
    """Page ANSI text. less draws colour only with -R: pydoc passes it from
    3.13 on; 3.12 inherits LESS, so -R is added for this one call."""
    import pydoc

    before = os.environ.get("LESS")
    os.environ["LESS"] = f"{before} -R" if before else "-R"
    try:
        pydoc.pager(text)
    finally:
        if before is None:
            os.environ.pop("LESS", None)
        else:
            os.environ["LESS"] = before


def print_report(text: str) -> None:
    """The forensics text with its section heads and alarms lit."""
    from rich.text import Text

    styled = Text(text)
    styled.highlight_regex(r"(?m)^[A-Z][A-Z0-9 &/-]+$", "bold cyan")
    styled.highlight_regex(r"ALARM.*|confirmed=False|NOT CONFIRMED.*", "bold red")
    console().print(styled)


def print_decisions(view: dict[str, Any]) -> None:
    """``skep policy decisions`` on a terminal: newest first, badged."""
    rows = [
        (
            str(row.get("at", "-")),
            badge(str(row.get("source") or row.get("verdict") or "-")),
            f"{row.get('scope', '-')}.{row.get('action', '-')}",
            str(row.get("value", "")),
            str(row.get("actor", "-")),
            str(row.get("task_id") or "-"),
        )
        for row in view["rows"]
    ]
    print_table(("at", "verdict", "what", "value", "actor", "task"), rows)
    counts = ", ".join(f"{key} {n}" for key, n in view["summary"].items() if n)
    console().print(f"total {view['total']}" + (f": {counts}" if counts else ""))
