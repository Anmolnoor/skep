"""v51-F4: skill management from chat — view free, create/patch/delete carded.

Chat-authored skills are procedural knowledge only: provenance='chat', zero
capability grants by construction (the WorkflowTemplate defaults are empty).
They skip ADR 0016's test gate (nothing was learned) but never the human
gate — the confirmation card IS the human gate.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.serve.jobs import Dispatcher
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import (
    MUTATING_TOOL_NAMES,
    READ_TOOL_NAMES,
    execute_mutation,
    execute_read_tool,
)
from skep.supervisor.templates import WorkflowTemplate

from .fake_ollama import FakeOllama
from .test_serve_chat import sse_events
from .test_serve_chat_tools import chat_client


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


def test_tool_tiers() -> None:
    assert "view_skill" in READ_TOOL_NAMES
    for tool in ("create_skill", "patch_skill", "delete_skill"):
        assert tool in MUTATING_TOOL_NAMES


def test_view_skill_shows_the_recipe_and_grants(config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        store.add_template(
            WorkflowTemplate(
                name="release-notes",
                instructions="Summarize merged PRs since the last tag.",
                description="weekly notes",
                network=("api.github.com",),
            )
        )
        holder = ConfigHolder(config, store)
        view = execute_read_tool(
            "view_skill", {"name": "release-notes"}, store=store, holder=holder
        )
        assert view["name"] == "release-notes"
        assert view["instructions"] == "Summarize merged PRs since the last tag."
        assert view["caste"] == "coding"
        assert "api.github.com" in view["grants"]  # the grant surface is never hidden
        missing = execute_read_tool("view_skill", {"name": "ghost"}, store=store, holder=holder)
        assert "no skill/template" in missing["error"]
    finally:
        store.close()


def test_create_skill_is_carded_and_lands_with_chat_provenance(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call(
        "create_skill",
        {"name": "greet", "instructions": "Say hello politely.", "description": "greeter"},
    )
    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "save this"}).text
    )
    actions = [d for name, d in events if name == "action"]
    assert [a["tool"] for a in actions] == ["create_skill"]
    # The card carries the full instructions — the operator sees what they sign.
    assert actions[0]["args"]["instructions"] == "Say hello politely."
    assert events[-1] == ("done", {"state": "awaiting_confirmation"})

    # NOTHING saved until the verdict.
    store = RunStore(config.db_path)
    try:
        assert store.get_template("greet") is None
    finally:
        store.close()

    action_id = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]
    ollama.script_reply("saved the skill")
    confirm = sse_events(client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm").text)
    assert confirm[-1] == ("done", {"state": "complete"})

    store = RunStore(config.db_path)
    try:
        template = store.get_template("greet")
        assert template is not None
        assert template.provenance == "chat"
        # Zero grants by construction.
        assert template.network == ()
        assert template.shell_allowlist == ()
        assert template.allow_git_mutation is False
    finally:
        store.close()


def _mutate(
    config: SupervisorConfig, store: RunStore, name: str, args: dict[str, object]
) -> object:
    holder = ConfigHolder(config, store)
    return execute_mutation(
        name, args, store=store, holder=holder, runner=Dispatcher(holder, store), actor="tester"
    )


def test_create_skill_never_overwrites(config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        store.add_template(WorkflowTemplate(name="taken", instructions="original"))
        with pytest.raises(ValueError, match="already exists"):
            _mutate(config, store, "create_skill", {"name": "taken", "instructions": "usurper"})
        template = store.get_template("taken")
        assert template is not None and template.instructions == "original"
    finally:
        store.close()


def test_patch_skill_replaces_exactly_once_and_only_instructions(
    config: SupervisorConfig,
) -> None:
    store = RunStore(config.db_path)
    try:
        store.add_template(
            WorkflowTemplate(
                name="notes",
                instructions="check the tag, check the tag",
                network=("api.github.com",),
                provenance="learned",
            )
        )
        result = _mutate(
            config,
            store,
            "patch_skill",
            {"name": "notes", "old_string": "check the tag", "new_string": "diff the tag"},
        )
        assert result == {"patched": "notes"}
        patched = store.get_template("notes")
        assert patched is not None
        assert patched.instructions == "diff the tag, check the tag"  # first match only
        assert patched.network == ("api.github.com",)  # grants untouched
        assert patched.provenance == "learned"
        with pytest.raises(ValueError, match="not found"):
            _mutate(
                config,
                store,
                "patch_skill",
                {"name": "notes", "old_string": "absent", "new_string": "x"},
            )
    finally:
        store.close()


def test_delete_skill_removes_and_errors_honestly(config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        store.add_template(WorkflowTemplate(name="old-skill", instructions="retire me"))
        assert _mutate(config, store, "delete_skill", {"name": "old-skill"}) == {
            "deleted": "old-skill"
        }
        assert store.get_template("old-skill") is None
        with pytest.raises(ValueError, match="no skill/template"):
            _mutate(config, store, "delete_skill", {"name": "old-skill"})
    finally:
        store.close()
