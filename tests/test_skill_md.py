"""L2: SKILL.md and docs/agent-guide.md name only commands that exist.

Both files are load-bearing context for agents (often small models), so a
stale command in them is a hallucination we shipped. Every `skep …` they name
must walk the real argparse tree with flags its leaf parser knows, and every
command in a fenced block — the copy-paste path, incl. the onboarding gate —
must parse outright. Walks the parser; never shells out.
"""

from __future__ import annotations

import argparse
import re
import shlex
from pathlib import Path

import pytest

from skep.cli import _parser
from skep.supervisor.serve.tools import TOOL_SPECS

ROOT = Path(__file__).resolve().parents[1]
DOCS = (ROOT / "SKILL.md", ROOT / "docs" / "agent-guide.md")
# The gate: an agent given only SKILL.md onboards, checks, and explains a why.
GATE_PATH = (
    "skep onboard --template builder --yes --no-chat",
    "skep onboard --check",
    "skep acp install-hint",
    'skep policy why ~/workspace shell run "git push"',
)


def _commands(text: str) -> tuple[list[str], list[str]]:
    """(fenced, inline) `skep …` command strings."""
    fenced: list[str] = []
    for block in re.findall(r"```(?:sh)?\n(.*?)```", text, flags=re.S):
        for line in block.splitlines():
            line = line.split(" #")[0].strip()
            if line.startswith("skep "):
                fenced.append(line)
    inline = [m for m in re.findall(r"`(skep [^`]+)`", text)]
    return fenced, inline


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _walk(tokens: list[str]) -> tuple[argparse.ArgumentParser, list[str]]:
    """Descend the subcommand tree; return the leaf parser and the subcommand path."""
    root = parser = _parser()
    path: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if parser is root and token == "--home":
            i += 2
            continue
        sub = _subparsers(parser)
        if token in sub:
            parser, i = sub[token], i + 1
            path.append(token)
            continue
        break
    return parser, path


def _all_commands() -> list[tuple[str, str, bool]]:
    out = []
    for doc in DOCS:
        fenced, inline = _commands(doc.read_text(encoding="utf-8"))
        out += [(doc.name, c, True) for c in fenced] + [(doc.name, c, False) for c in inline]
    return out


def test_docs_name_commands() -> None:
    for doc in DOCS:
        fenced, inline = _commands(doc.read_text(encoding="utf-8"))
        assert fenced and inline, doc


@pytest.mark.parametrize(("doc", "command", "fenced"), _all_commands())
def test_named_command_exists(doc: str, command: str, fenced: bool) -> None:
    tokens = shlex.split(command.replace("\n", " "))[1:]
    leaf, path = _walk(tokens)
    assert path, f"{doc}: `{command}` names no skep subcommand"
    root_flags = set(_parser()._option_string_actions)
    for token in tokens:
        if token.startswith("--"):
            flag = token.split("=")[0]
            assert flag in leaf._option_string_actions or flag in root_flags, (
                f"{doc}: `{command}` — {flag} is not a flag of `skep {' '.join(path)}`"
            )
    if fenced:
        _parser().parse_args(tokens)  # a copy-paste command must parse as written


def test_gate_path_is_in_skill_md() -> None:
    fenced, _ = _commands((ROOT / "SKILL.md").read_text(encoding="utf-8"))
    for command in GATE_PATH:
        assert command in fenced, command


def test_face_table_names_real_chat_tools() -> None:
    names = {t["function"]["name"] for t in TOOL_SPECS}
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    rows = [line for line in text.splitlines() if line.startswith("| ") and "`skep " in line]
    assert len(rows) >= 10
    for row in rows:
        chat = [n for n in re.findall(r"`([a-z]+(?:_[a-z]+)+)`", row)]
        assert chat and set(chat) <= names, row


def test_skill_md_frontmatter_and_readme_links() -> None:
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    head = text.split("---")[1]
    assert re.search(r"^name: skep$", head, flags=re.M)
    assert re.search(r"^description: \S", head, flags=re.M)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "SKILL.md" in readme and "docs/agent-guide.md" in readme
