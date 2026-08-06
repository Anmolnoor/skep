"""v74-F3: the tool index — stop resending the whole manual every round.

Progressive disclosure, three tiers: a categorized one-line index in the
prompt, full schemas for the core set (+ this chat's described-active tools),
and describe_tools for on-demand schema fetch. Authority does not move: the
tools array was advertisement, never permission — the executor dispatches on
the name, mutations still card, deny space stays unreachable (I5/I6).
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.serve.tools import (
    CORE_TOOL_NAMES,
    DESCRIBE_TOOL_NAME,
    MUTATING_TOOL_NAMES,
    READ_TOOL_NAMES,
    TOOL_CATEGORIES,
    TOOL_INDEX_BLOCK,
    TOOL_SPECS,
    advertised_tool_specs,
)
from skep.supervisor.store import RunStore

from .fake_ollama import FakeOllama
from .test_serve_chat import configured_client, sse_events


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


def _line_for(name: str) -> str:
    (line,) = [
        entry
        for entry in TOOL_INDEX_BLOCK.splitlines()
        if entry.startswith(f"- {name}") and entry[2 + len(name) :][:1] in ("", "*", "(")
    ]
    return line


def test_the_encoder_states_the_confirmation_path_once_not_per_tool() -> None:
    """v99-F1: '*' is derived from MUTATING_TOOL_NAMES, so it cannot disagree
    with the executor — six tools in that set never said 'PROPOSE' in their
    prose, and the old index under-reported them (I8)."""
    assert _line_for("land_run").startswith("- land_run*(")
    assert _line_for("dispatch_run").startswith("- dispatch_run*")  # prose said no
    assert not _line_for("git_diff").startswith("- git_diff*")
    # The boilerplate the legend replaces is gone from every gloss.
    assert "PROPOSE" not in TOOL_INDEX_BLOCK
    assert "requires user confirmation" not in TOOL_INDEX_BLOCK


def test_a_core_tool_is_name_only_because_its_schema_is_in_the_request() -> None:
    """Zero information loss, provably: advertised_tool_specs ships the full
    schema for every core tool in the same request."""
    for name in CORE_TOOL_NAMES:
        mark = "*" if name in MUTATING_TOOL_NAMES else ""
        assert _line_for(name) == f"- {name}{mark}"
    advertised = {t["function"]["name"] for t in advertised_tool_specs([])}
    assert advertised >= set(CORE_TOOL_NAMES)


def test_a_gloss_that_only_restates_the_name_is_dropped() -> None:
    """delete_note's summary was 'deleting a note' — the model just read that."""
    assert _line_for("delete_note") == "- delete_note*(note_id)"
    assert _line_for("attach_policy_group") == "- attach_policy_group*(project_id, name)"
    # An informative gloss survives.
    assert " — " in _line_for("await_runs")


def test_a_wide_arg_list_truncates_and_says_so() -> None:
    """set_policy lists 14 args; describe_tools has the rest."""
    line = _line_for("set_policy")
    assert line.count(",") == 5  # 6 args shown
    assert "…)" in line  # and the rest declared, not silently dropped


def test_the_index_is_generated_from_the_registry() -> None:
    """Every tool appears exactly once, categorized — the index cannot drift
    from the registry (the v25 lockstep lesson)."""
    names = {t["function"]["name"] for t in TOOL_SPECS}
    categorized = [n for cat in TOOL_CATEGORIES.values() for n in cat]
    assert set(categorized) == names  # nothing uncategorized, nothing stale
    assert len(categorized) == len(set(categorized))  # exactly one category each
    for name in names:
        # v99-F1: a core tool renders name-only (its full schema is in the
        # same request); everything else renders name(args). Coverage is what
        # this test protects — a tool missing from the index is invisible.
        mark = "*" if name in MUTATING_TOOL_NAMES else ""
        needle = f"\n- {name}{mark}\n" if name in CORE_TOOL_NAMES else f"{name}{mark}("
        assert needle in TOOL_INDEX_BLOCK + "\n", name
    for category in TOOL_CATEGORIES:
        assert f"[{category}]" in TOOL_INDEX_BLOCK
    # One line per tool: header + category headers + tool lines.
    assert TOOL_INDEX_BLOCK.count("\n- ") == len(names)
    # The index teaches the next step and the card rule (I9). The legend
    # promises the confirmation PATH, not an unconditional card: read_file and
    # search_files are in MUTATING_TOOL_NAMES and are routinely auto-allowed,
    # so "always cards" would be a lie on the majority case (I8).
    assert "describe_tools(names=[...])" in TOOL_INDEX_BLOCK
    assert "confirmation path" in TOOL_INDEX_BLOCK
    assert "UNLESS project policy auto-allows" in TOOL_INDEX_BLOCK
    assert names >= CORE_TOOL_NAMES


def test_a_fresh_chat_advertises_only_the_core_and_carries_the_index(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    ollama.script_reply("hello")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"})
    body = ollama.chat_bodies()[0]
    advertised = {t["function"]["name"] for t in body["tools"]}
    assert advertised == set(CORE_TOOL_NAMES)
    assert "Tool index" in body["messages"][0]["content"]


def test_the_fresh_chat_floor_is_measured_under_24kb(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """The F3 acceptance, pinned by number (I10): system prompt + index +
    core specs — down from ~60KB. v83 re-measured (the review item 6
    mandate): three new tools (get_chat_context, remember,
    delegate_analysis) grew the index to 25.1KB, so the pin moved
    25KB → 26KB — a measured budget, not a drifting one; the next tool
    round re-measures again. v85 re-measured: two pack-ladder tools
    (promote_skill_pack, suspend_skill_pack) grew the index to 26.1KB,
    so the pin moves 26KB → 26.5KB. v95 re-measured: the engine params on
    dispatch_run/setup_project grew the index to 26.7KB, pin moves
    26.5KB → 27KB. v97 re-measured: five policy-group verbs (ADR 0048)
    grew the floor to 27.3KB, pin moves 27KB → 27.5KB.

    v99-F3 is the first move DOWNWARD, and it is a ratchet: re-encoding the
    two indexes (F1 tool, F2 skill) cut the floor to 22.7KB while ADDING
    coverage — all 112 tools, all 91 skills. Leaving the pin at 27.5KB would
    let the next round silently spend the whole 4.7KB win, so it moves
    27.5KB → 23KB. Grow the surface and this fails; that is the point.

    v101-F12 re-measured, and this is the explicit decision the plan required
    rather than a side effect. Generating dispatch_run's per-caste guidance from
    the registry costs 875 chars and takes the floor to 23.5KB. What it buys:
    the enums hardcoded two and three castes, so five of eight were unreachable
    from chat entirely — and a small model that skims tool descriptions reads an
    eight-name enum with no gloss as eight names it has no reason to pick. The
    registry summaries were tightened first (64 chars, and both the Settings
    roster and the schema read them, so the win lands twice); that was not
    enough, so the pin moves 23KB → 24KB. It is still 3.5KB below where v99
    found it.

    v108-F2 re-measured. The provider registry's first operator face — four
    verbs (list/add/use/remove provider) — costs ~350 chars of index lines
    and takes the floor over 24KB; the glosses were tightened to first-
    sentence budgets before the move (I9 forbids gutting them further: the
    small model reads nothing else). The pin moves 24KB → 24.5KB, sized to
    also absorb the two protocol enum values v108-F5/F6 add. Still 3KB
    below where v99 found it.

    v109 re-measured: the round paid byte-for-byte before asking — F7 FOLDED
    its network-remember into allow_command_review instead of shipping a
    second tool (a spec entry costs more than any description trim recovers),
    and F6 trimmed its dispatch_run addition — but F9's revoke_policy_rule is
    a genuinely new carded verb (the first way to SEE and NARROW standing
    grants, I6's other half) and lands the floor at 24.02KB. The pin moves
    24KB → 24.5KB, still 3KB below where v99 found it."""
    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    ollama.script_reply("hello")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"})
    body = ollama.chat_bodies()[0]
    floor = len(body["messages"][0]["content"]) + len(json.dumps(body["tools"]))
    assert floor <= 24_500
    context = client.get(f"/api/chats/{chat_id}").json()["context"]
    assert context["floor_chars"] <= 24_500


def test_describe_tools_activates_and_the_next_round_advertises(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    ollama.script_tool_call(DESCRIBE_TOOL_NAME, {"names": ["list_notes", "add_note"]})
    ollama.script_reply("described")
    response = client.post(f"/api/chats/{chat_id}/messages", json={"content": "notes?"})
    events = sse_events(response.text)
    tool_events = [d for e, d in events if e == "tool"]
    described = tool_events[0]["result"]["tools"]
    assert [t["name"] for t in described] == ["list_notes", "add_note"]
    assert all("parameters" in t and "description" in t for t in described)

    # Persisted on the chat, and the SAME turn's next round advertises them.
    store = RunStore(config.db_path)
    try:
        chat = store.get_chat(chat_id)
        assert chat is not None and chat.active_tools == ["list_notes", "add_note"]
    finally:
        store.close()
    second = ollama.chat_bodies()[1]
    advertised = {t["function"]["name"] for t in second["tools"]}
    assert advertised == set(CORE_TOOL_NAMES) | {"list_notes", "add_note"}


def test_describe_tools_teaches_on_unknown_names(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    ollama.script_tool_call(DESCRIBE_TOOL_NAME, {"names": ["frobnicate"]})
    ollama.script_reply("ok")
    response = client.post(f"/api/chats/{chat_id}/messages", json={"content": "?"})
    result = next(d for e, d in sse_events(response.text) if e == "tool")["result"]
    assert result["tools"] == []
    assert result["unknown"]["names"] == ["frobnicate"]
    assert "tool index" in result["unknown"]["note"]
    store = RunStore(config.db_path)
    try:
        chat = store.get_chat(chat_id)
        assert chat is not None and not chat.active_tools  # nothing activated
    finally:
        store.close()


def test_an_indexed_but_inactive_read_executes_when_called_directly(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """The executor accepts any registered tool by name — a model that read
    the index calls it without describing first."""
    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    assert "list_notes" not in CORE_TOOL_NAMES
    ollama.script_tool_call("list_notes", {})
    ollama.script_reply("no notes")
    response = client.post(f"/api/chats/{chat_id}/messages", json={"content": "notes?"})
    result = next(d for e, d in sse_events(response.text) if e == "tool")["result"]
    assert "error" not in result
    assert "notes" in result


def test_a_mutation_proposed_from_the_index_still_cards(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """I5/I6: advertisement moved, permission did not."""
    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    assert "set_personality" not in CORE_TOOL_NAMES
    ollama.script_tool_call("set_personality", {"personality": "concise"})
    response = client.post(f"/api/chats/{chat_id}/messages", json={"content": "be terse"})
    events = sse_events(response.text)
    actions = [d for e, d in events if e == "action"]
    assert len(actions) == 1 and actions[0]["tool"] == "set_personality"
    assert [d for e, d in events if e == "done"] == [{"state": "awaiting_confirmation"}]


def test_the_unknown_tool_error_teaches(config: SupervisorConfig, ollama: FakeOllama) -> None:
    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    ollama.script_tool_call("frobnicate", {})
    ollama.script_reply("ok")
    response = client.post(f"/api/chats/{chat_id}/messages", json={"content": "?"})
    result = next(d for e, d in sse_events(response.text) if e == "tool")["result"]
    assert "no tool named 'frobnicate'" in result["error"]
    assert "tool index" in result["error"]
    assert "describe_tools" in result["error"]


def test_read_only_turns_get_the_read_only_core(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    ollama.script_reply("side answer")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "btw?", "read_only": True})
    advertised = {t["function"]["name"] for t in ollama.chat_bodies()[0]["tools"]}
    assert advertised == set(CORE_TOOL_NAMES) & READ_TOOL_NAMES
    assert "dispatch_run" not in advertised


def test_the_full_setting_restores_the_old_array_byte_for_byte(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """The escape hatch: one flip back to today's behavior."""
    client = configured_client(config, ollama)
    assert client.get("/api/llm/config").json()["tool_delivery"] == "indexed"
    client.put("/api/llm/config", json={"tool_delivery": "full"})
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    ollama.script_reply("hello")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"})
    body = ollama.chat_bodies()[0]
    assert json.dumps(body["tools"]) == json.dumps(TOOL_SPECS)
    assert "Tool index" not in body["messages"][0]["content"]


def test_advertised_specs_ride_in_registry_order() -> None:
    active = ["forget_memory", "read_file"]
    advertised = [t["function"]["name"] for t in advertised_tool_specs(active)]
    assert set(advertised) == set(CORE_TOOL_NAMES) | set(active)
    order = [t["function"]["name"] for t in TOOL_SPECS]
    assert advertised == [n for n in order if n in set(advertised)]
    # read_only keeps only the read-shaped part.
    read_advertised = {t["function"]["name"] for t in advertised_tool_specs(active, read_only=True)}
    assert read_advertised == (set(CORE_TOOL_NAMES) | set(active)) - MUTATING_TOOL_NAMES
