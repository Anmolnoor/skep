"""v53-F7 (ADR 0027): the approved-skill index rides the chat prompt.

Names + one line each; the full recipe loads on demand via view_skill.
Drafts never appear: candidates live in their own table and reach the
registry only through the human approve gate (ADR 0016).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.serve.chat import skill_index_block
from skep.supervisor.skills import SkillCandidate, candidate_signature
from skep.supervisor.templates import WorkflowTemplate

from .fake_ollama import FakeOllama
from .test_serve_chat_tools import chat_client


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


def test_turn_prompt_carries_the_skill_index(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    store = RunStore(config.db_path)
    try:
        store.add_template(
            WorkflowTemplate(
                name="release-checklist",
                instructions="Cut a release: bump, tag, changelog.",
                description="Steps for cutting a skep release",
            )
        )
    finally:
        store.close()

    ollama.script_reply("ok")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"})

    content = ollama.chat_bodies()[-1]["messages"][0]["content"]
    # v99-F2: names, not descriptions — coverage is what the index buys, and
    # view_skill is one call away for the recipe.
    assert "release-checklist" in content
    assert "view_skill" in content
    # Index only — neither the description nor the instructions ride the prompt.
    assert "Steps for cutting a skep release" not in content
    assert "bump, tag, changelog" not in content


def test_no_templates_means_no_index(config: SupervisorConfig, ollama: FakeOllama) -> None:
    client, chat_id = chat_client(config, ollama)
    ollama.script_reply("ok")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"})
    content = ollama.chat_bodies()[-1]["messages"][0]["content"]
    assert "Approved skills and templates" not in content


def test_every_skill_is_listed_and_drafts_are_invisible(tmp_path: Path) -> None:
    """v99-F2: no cap, no overflow line. A name behind '… and N more' is a
    name the model can never call view_skill on."""
    store = RunStore(tmp_path / "supervisor.sqlite3")
    try:
        for index in range(200):
            store.add_template(
                WorkflowTemplate(name=f"skill-{index:03}", instructions=f"do thing {index}")
            )
        draft_template = WorkflowTemplate(name="draft-only", instructions="not yet approved")
        store.add_candidate(
            SkillCandidate(
                name="draft-only",
                signature=candidate_signature(draft_template),
                status="draft",
                template=draft_template,
                source_task_ids=("t-1",),
                occurrences=1,
            )
        )
        block = skill_index_block(store)
    finally:
        store.close()
    listed = {name.strip() for name in block.splitlines()[-1].split(",")}
    assert len(listed) == 200
    assert all(f"skill-{index:03}" in listed for index in range(200))
    assert "more —" not in block and "…" not in block  # nothing hidden (I8)
    assert "draft-only" not in block  # a candidate never self-promotes
