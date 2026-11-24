"""Phase 06 (6.2): cache-safe skill hints — the system prompt never moves.

The skill index in the system prompt lists every seed by name only (v126-F8:
descriptions do not fit the bound), so "merge these pdfs" left the Queen to
notice `pdf` in a comma-packed name list. Re-ranking the index per turn would
change the system prompt every turn and void the provider's prompt cache —
costing MORE tokens than it saves. Instead a deterministic keyword match puts
ONE line on the user turn that matched, stored with it, so every later turn
replays it byte-for-byte and the cached prefix never changes.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.seed_skills import load_seed_skills
from skep.supervisor.serve.chat import SKILL_HINT_MAX, skill_hint
from skep.supervisor.templates import WorkflowTemplate

from .fake_ollama import FakeOllama
from .test_serve_chat_tools import chat_client

_CLOCK = re.compile(r"Operator local time: [^\n]*store timestamps are UTC\.")


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


@pytest.fixture()
def seeded(tmp_path: Path) -> Iterator[RunStore]:
    store = RunStore(tmp_path / "supervisor.sqlite3")
    load_seed_skills(store)
    yield store
    store.close()


def test_a_message_naming_a_seed_gets_a_one_line_hint(seeded: RunStore) -> None:
    hint = skill_hint(seeded, "Can you merge these two PDFs for me?")
    assert hint.startswith("[Relevant skills: pdf — read, merge, split")
    assert hint.endswith("]") and "\n" not in hint


def test_every_word_of_a_multi_word_name_must_appear(seeded: RunStore) -> None:
    """A clear match, not a guess: 'summarize-a-youtube-video' needs
    summarize + youtube + video (the stopword 'a' aside)."""
    assert "summarize-a-youtube-video" in skill_hint(seeded, "summarize this youtube video please")
    assert "summarize-a-youtube-video" not in skill_hint(seeded, "summarize the video")


def test_no_match_means_no_hint(seeded: RunStore) -> None:
    assert skill_hint(seeded, "hi") == ""
    assert skill_hint(seeded, "") == ""


def test_operator_skills_are_never_hinted_they_are_described_already(
    seeded: RunStore,
) -> None:
    seeded.add_template(
        WorkflowTemplate(name="invoice", instructions="x", description="bill a client")
    )
    assert "invoice" not in skill_hint(seeded, "send the invoice")


def test_the_hint_is_bounded_and_most_specific_first(seeded: RunStore) -> None:
    message = (
        "add a test, plan the pdf docx xlsx powerpoint work, review a pr, "
        "git and github, summarize a youtube video"
    )
    hint = skill_hint(seeded, message)
    names = [part.split(" — ")[0] for part in hint[len("[Relevant skills: ") : -1].split("; ")]
    assert len(names) == SKILL_HINT_MAX
    assert names[0] == "summarize-a-youtube-video"  # three words matched


def _system(body: dict[str, Any]) -> str:
    return _CLOCK.sub("<clock>", body["messages"][0]["content"])


def test_the_system_prompt_is_byte_identical_and_the_hint_rides_its_turn(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """Test 6.3: system prompt (reach_block, skill index, tool specs) and every
    replayed prior turn are identical across turns — the clock line aside —
    and the hint is on the matching user turn only."""
    client, chat_id = chat_client(config, ollama)
    store = RunStore(config.db_path)
    try:
        load_seed_skills(store)
    finally:
        store.close()
    for content in ("hi", "merge these pdfs", "thanks"):
        ollama.script_reply("ok")
        client.post(f"/api/chats/{chat_id}/messages", json={"content": content})
    first, second, third = ollama.chat_bodies()
    assert _system(first) == _system(second) == _system(third)
    assert first["tools"] == second["tools"] == third["tools"]
    # Replayed turns never move: each body extends the one before it.
    for earlier, later in ((first, second), (second, third)):
        replayed = earlier["messages"][1:]
        assert later["messages"][1 : 1 + len(replayed)] == replayed
    users = [m["content"] for m in third["messages"] if m["role"] == "user"]
    assert users[0] == "hi" and users[2] == "thanks"
    assert users[1].startswith("merge these pdfs\n\n[Relevant skills: pdf — ")
    # Stored on the row, so a replay after a restart is the same bytes.
    store = RunStore(config.db_path)
    try:
        stored = [m.content for m in store.chat_messages(chat_id) if m.role == "user"]
    finally:
        store.close()
    assert stored == users


def test_the_users_bubble_shows_what_they_typed_and_the_model_replays_the_hint(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """R1 (F7): the stored row keeps the hint (replay bytes stay put), but the
    chat fetch shows the user their own words — Resend copies those, so the
    hint never stacks."""
    client, chat_id = chat_client(config, ollama)
    store = RunStore(config.db_path)
    try:
        load_seed_skills(store)
    finally:
        store.close()
    for _ in range(2):  # the second send is what Resend would post
        ollama.script_reply("ok")
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "merge these pdfs"})
    shown = [
        m["content"]
        for m in client.get(f"/api/chats/{chat_id}").json()["messages"]
        if m["role"] == "user"
    ]
    assert shown == ["merge these pdfs", "merge these pdfs"]
    replayed = [m["content"] for m in ollama.chat_bodies()[-1]["messages"] if m["role"] == "user"]
    assert replayed[0] == replayed[1]
    assert replayed[0].startswith("merge these pdfs\n\n[Relevant skills: pdf — ")
    assert replayed[0].count("[Relevant skills:") == 1


def test_an_operator_typed_hint_line_is_never_stripped(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """R2 (G6): the bubble strips only the hint the server recorded on the
    row — an operator's own "[Relevant skills: …]" line, on a message that
    got no generated hint, is what they typed."""
    client, chat_id = chat_client(config, ollama)
    typed = "note for later\n\n[Relevant skills: my own list]"
    ollama.script_reply("ok")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": typed})
    shown = [
        m["content"]
        for m in client.get(f"/api/chats/{chat_id}").json()["messages"]
        if m["role"] == "user"
    ]
    assert shown == [typed]
    replayed = [m["content"] for m in ollama.chat_bodies()[-1]["messages"] if m["role"] == "user"]
    assert replayed == [typed]
