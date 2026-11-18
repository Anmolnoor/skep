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
from skep.supervisor.serve import chat as chat_module
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


def test_turn_prompt_carries_the_skill_index(config: SupervisorConfig, ollama: FakeOllama) -> None:
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
    # v99-F2: coverage is what the index buys, and view_skill is one call
    # away for the recipe. v126-F8: an operator skill's short description
    # rides along so the model knows when to use it; the recipe never does.
    assert "- release-checklist: Steps for cutting a skep release" in content
    assert "view_skill" in content
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


# v126-F8: v99-F2 made the index names-only, so the model saw "notion" but
# not when to use it. Non-seed skills carry a short description inside a
# bound; the 85-seed shelf stays names-only (measured: it does not fit).


def test_operator_skills_carry_a_short_description_and_seeds_stay_names(tmp_path: Path) -> None:
    from skep.supervisor.seed_skills import load_seed_skills

    store = RunStore(tmp_path / "supervisor.sqlite3")
    try:
        seeds = load_seed_skills(store)["loaded"]
        store.add_template(
            WorkflowTemplate(
                name="release-checklist",
                instructions="1. tag",
                description="cut a release: changelog, tag, publish to PyPI and verify the wheel",
                provenance="external",
            )
        )
        block = skill_index_block(store)
    finally:
        store.close()
    lines = block.splitlines()
    assert lines[1] == "- release-checklist: cut a release: changelog, tag, publish to PyPI and…"
    assert lines[-1].split(", ") == seeds  # every seed, names only, none hidden
    assert len(block) <= chat_module.SKILL_INDEX_MAX_CHARS


def test_short_descriptions_cut_on_a_word() -> None:
    short = chat_module.short_description
    assert short("fits as is") == "fits as is"
    cut = short("read and update Notion pages and databases via the vendor MCP server")
    assert cut == "read and update Notion pages and databases via the…"
    assert len(cut) <= chat_module.SKILL_DESCRIPTION_CHARS + 1
    assert short("x" * 80) == "x" * 50 + "…"  # no space to cut on
    assert short("  spread\n over   lines ") == "spread over lines"


def test_the_index_stays_bounded_and_complete_with_many_operator_skills(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "supervisor.sqlite3")
    try:
        for index in range(200):
            store.add_template(
                WorkflowTemplate(
                    name=f"skill-{index:03}",
                    instructions="x",
                    description=f"does the {index} thing for the operator, every single day",
                )
            )
        block = skill_index_block(store)
    finally:
        store.close()
    assert len(block) <= chat_module.SKILL_INDEX_MAX_CHARS
    described = [line[2:].split(":")[0] for line in block.splitlines()[1:-1]]
    overflow = block.splitlines()[-1].split(", ")
    assert described and overflow
    assert sorted(described + overflow) == [f"skill-{index:03}" for index in range(200)]
