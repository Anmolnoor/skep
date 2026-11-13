"""W38 (found live): a read card for a symlink named the link and hid where
the read really went. The card now shows both; a confirm refuses when the
link moved since the card was shown; search_files lists a symlink with its
target instead of skipping it silently (it never follows one)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from skep.supervisor.serve.channels import card_notes

from .test_taint_v3 import Live, _action, live  # noqa: F401 (the fixture)


@pytest.fixture()
def env(live: Callable[[str], Live]) -> Live:  # noqa: F811
    return live("builder")


def _link(env: Live) -> tuple[Path, Path]:
    target = env.paths["outside"] / "notes.txt"
    link = env.paths["repo"] / "docs" / "roadmap.md"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)
    return link, target.resolve()


def test_a_read_card_for_a_symlink_names_where_it_resolves(env: Live) -> None:
    link, target = _link(env)
    chat = env.chat()
    env.ollama.script_tool_call("read_file", {"path": str(link)})
    card = _action(env.turn(chat, "read the roadmap"))["card"]
    assert card["headline"] == f"read_file — {link} → resolves to {target}"
    assert card["resolves_to"] == str(target)
    assert any(f"resolves to {target}" in note for note in card_notes(card))
    (fetched,) = env.store.chat_actions(chat)
    assert fetched.card and fetched.card["resolves_to"] == str(target)


def test_a_plain_path_card_says_nothing_extra(env: Live) -> None:
    chat = env.chat()
    plain = env.paths["outside"].resolve() / "notes.txt"
    env.ollama.script_tool_call("read_file", {"path": str(plain)})
    card = _action(env.turn(chat, "read it"))["card"]
    assert "resolves to" not in card["headline"] and "resolves_to" not in card


def test_a_confirm_refuses_when_the_link_moved_since_the_card(env: Live) -> None:
    link, _target = _link(env)
    chat = env.chat()
    env.ollama.script_tool_call("read_file", {"path": str(link)})
    card = _action(env.turn(chat, "read the roadmap"))
    other = env.paths["outside"] / "other.txt"
    other.write_text("SECOND TARGET\n")
    link.unlink()
    link.symlink_to(other)
    env.ollama.script_reply("ok")
    env.confirm(chat, card["action_id"])
    rows = [json.loads(m.content) for m in env.store.chat_messages(chat) if m.role == "tool"]
    assert not rows[-1]["ok"] and "now resolves to" in rows[-1]["error"]
    assert "SECOND TARGET" not in json.dumps(rows)


def test_search_files_lists_a_symlink_with_its_target_and_never_follows_it(env: Live) -> None:
    from skep.supervisor.serve.fileio import search_files_result

    link, target = _link(env)
    (env.paths["outside"] / "notes.txt").write_text("needle\n")
    names = search_files_result("roadmap", path=str(env.paths["repo"]), target="files")
    assert names["symlinks"] == [{"path": str(link), "target": str(target)}]
    assert not any("roadmap" in hit for hit in names["matches"])
    content = search_files_result("needle", path=str(env.paths["repo"]))
    assert content["matches"] == []  # not followed
    assert {"path": str(link), "target": str(target)} in content["symlinks"]
    assert "not followed" in content["note"]
