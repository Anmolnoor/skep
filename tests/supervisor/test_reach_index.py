"""v74-F5: one "You can reach:" index — tools, skills, MCP servers.

The pieces existed (F3 tool index, v53-F7 skill index, list_mcp_tools
discovery); what was missing was the one roof: a single prompt section where
every capability appears with its detail verb, so "what can you reach?" has
one answer and adding a tool, skill, or server needs zero prompt edits.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.serve.chat import reach_block
from skep.supervisor.templates import WorkflowTemplate

from .fake_ollama import FakeOllama
from .test_serve_chat import configured_client


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


def _seed_registries(store: RunStore) -> None:
    store.add_template(
        WorkflowTemplate(
            name="release-checklist",
            instructions="Cut a release: bump, tag, changelog.",
            description="Steps for cutting a skep release",
        )
    )
    store.set_setting(
        "mcp_servers",
        [
            {
                "server_id": "obsidian",
                "transport": "stdio",
                "command": ["npx", "obsidian-mcp"],
            }
        ],
    )


def test_the_prompt_carries_one_reach_roof_with_detail_verbs(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """Live registries feed the block — a new skill or server appears with
    zero prompt edits — and every section names its detail verb."""
    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    store = RunStore(config.db_path)
    try:
        _seed_registries(store)
    finally:
        store.close()

    ollama.script_reply("ok")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "what can you reach?"})
    content = ollama.chat_bodies()[-1]["messages"][0]["content"]

    assert "You can reach:" in content
    # Tools by category, detail verb: describe_tools.
    assert "[runs]" in content and "[mcp & plugins]" in content
    assert "describe_tools(names=[...])" in content
    # Skills by name, detail verb: view_skill (v99-F2: names, no descriptions).
    assert "release-checklist" in content
    assert "Steps for cutting a skep release" not in content
    assert "view_skill" in content
    # MCP servers by id only, detail verb: list_mcp_tools.
    assert "- obsidian (stdio)" in content
    assert "list_mcp_tools" in content
    assert "obsidian-mcp" not in content  # ids only — commands stay behind reads


def test_reach_block_sizes_are_measured(config: SupervisorConfig) -> None:
    """I10: measured, not adjectival. The tool index dominates (~11KB with
    80-char summaries — the plan's 4KB estimate lost to 92 real tools); the
    skills + MCP fold-in stays under 2KB at fixture scale. The binding
    acceptance is F3's: the whole fresh-chat floor <= 25KB, pinned in
    test_tool_index."""
    store = RunStore(config.db_path)
    try:
        _seed_registries(store)
        block = reach_block(store)
        assert 0 < len(block) < 9_000  # v99-F3: re-encoded index, ratcheted
        # Skills + MCP sections alone (headers + entries) — the fold-in cost.
        skills_and_mcp = block[block.index("Approved skills") :]
        assert len(skills_and_mcp) < 1_000  # v99-F2: names, not descriptions
    finally:
        store.close()


def test_full_delivery_keeps_skills_and_mcp_but_drops_the_tool_index(
    config: SupervisorConfig,
) -> None:
    from skep.supervisor.serve.llm import TOOL_DELIVERY_SETTING

    store = RunStore(config.db_path)
    try:
        _seed_registries(store)
        store.set_setting(TOOL_DELIVERY_SETTING, "full")
        block = reach_block(store)
        assert "Tool index" not in block
        assert "release-checklist" in block
        assert "- obsidian (stdio)" in block
    finally:
        store.close()


def test_empty_registries_mean_no_extra_sections(config: SupervisorConfig) -> None:
    from skep.supervisor.serve.llm import TOOL_DELIVERY_SETTING

    store = RunStore(config.db_path)
    try:
        block = reach_block(store)
        assert "Tool index" in block  # the tool index always rides indexed mode
        assert "Approved skills" not in block
        assert "Registered MCP servers (call list_mcp_tools" not in block
        # Full delivery + nothing registered → no roof at all.
        store.set_setting(TOOL_DELIVERY_SETTING, "full")
        assert reach_block(store) == ""
    finally:
        store.close()
