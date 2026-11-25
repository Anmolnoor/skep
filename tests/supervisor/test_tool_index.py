"""v74-F3: the tool index — stop resending the whole manual every round.

Progressive disclosure, three tiers: a categorized one-line index in the
prompt, full schemas for the core set (+ this chat's described-active tools),
and describe_tools for on-demand schema fetch. Authority does not move: the
tools array was advertisement, never permission — the executor dispatches on
the name, mutations still card, deny space stays unreachable (I5/I6).
"""

from __future__ import annotations

import functools
import json
from collections.abc import Iterator

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.serve import tools
from skep.supervisor.serve.tools import (
    CORE_TOOL_NAMES,
    DESCRIBE_TOOL_NAME,
    MUTATING_TOOL_NAMES,
    POLICY_LINE_TOOLS,
    RARE_ADMIN_TOOLS,
    READ_TOOL_NAMES,
    TOOL_CATEGORIES,
    TOOL_INDEX_BLOCK,
    TOOL_SPECS,
    advertised_tool_specs,
    tool_index_block,
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
    assert _line_for("reject_memory_proposal") == "- reject_memory_proposal*(proposal_id, reason)"
    # An informative gloss survives.
    assert " — " in _line_for("await_runs")


def test_a_wide_arg_list_truncates_and_says_so() -> None:
    """open_pr lists 8 args; describe_tools has the rest."""
    line = _line_for("open_pr")
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
    chat_id = client.post("/api/chats", json={"role": "builder"}).json()["chat_id"]
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
    24KB → 24.5KB, still 3KB below where v99 found it.
    P0-3 (ADR 0052) re-measured: three read verbs — policy_registry,
    policy_rsop, policy_why — are the chat face of the one policy engine the
    rewrite is building toward (the handoff asks for all three faces; a
    verb the Queen cannot reach is a verb it cannot explain a gate with). Their
    glosses were cut to the first-sentence budget first (20 chars paid); the
    three index lines still cost 190 chars and land the floor at 24.6KB. The
    pin moves 24.5KB → 25KB, 2.5KB below where v99 found it.
    L1 re-measured after R3 (which paid its own way to ~24,997): list_agents
    is the Queen's only answer to "what agents are running and which are
    governed" (the L1 gate). Its description was cut first, but an index line
    costs ~16 chars even with no gloss, so the floor lands at ~25,055. The pin
    moves 25KB → 25.1KB, 2.4KB below where v99 found it.

    Phase 06 (6.2) paid before asking, and the pin does NOT move: promoting
    search_web + read_url into the core costs ~2KB of schema, and 39 rare
    admin verbs (RARE_ADMIN_TOOLS) now index as name*(…) — describe_tools
    first — which buys it back with zero net growth. Measured, role-less
    (assistant) chat: 20,822 → 20,818; builder (no hidden tools, the widest
    core): 24,664 → 24,584; operator: 24,584 → 24,504. All three are pinned
    now — the builder chat is the heavier floor and was never measured."""
    client = configured_client(config, ollama)
    for create in ({}, {"role": "builder"}, {"role": "operator"}):
        chat_id = client.post("/api/chats", json=create).json()["chat_id"]
        ollama.script_reply("hello")
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"})
        body = ollama.chat_bodies()[-1]
        floor = len(body["messages"][0]["content"]) + len(json.dumps(body["tools"]))
        assert floor <= 25_100, (create, floor)
        context = client.get(f"/api/chats/{chat_id}").json()["context"]
        assert context["floor_chars"] <= 25_100, create


def test_describe_tools_activates_and_the_next_round_advertises(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={"role": "builder"}).json()["chat_id"]
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
    chat_id = client.post("/api/chats", json={"role": "builder"}).json()["chat_id"]
    ollama.script_reply("hello")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"})
    body = ollama.chat_bodies()[0]
    # P3-6 (ADR 0059 D7): byte for byte but each scoped tool's "In this chat:" line
    sent = json.loads(json.dumps(body["tools"]))
    for spec in sent:
        description = spec["function"]["description"]
        spec["function"]["description"] = description.split(" In this chat: ")[0]
    assert json.dumps(sent) == json.dumps(TOOL_SPECS)
    lined = [t["function"]["name"] for t in body["tools"]
             if " In this chat: " in t["function"]["description"]]  # fmt: skip
    assert set(lined) == POLICY_LINE_TOOLS
    assert "Tool index" not in body["messages"][0]["content"]


def test_advertised_specs_ride_core_first_then_activations_in_order() -> None:
    """v134-F10 (T3): core in registry order, then each described tool in the
    order it was described — an activation only ever appends."""
    active = ["forget_memory", "read_file"]
    advertised = [t["function"]["name"] for t in advertised_tool_specs(active)]
    assert set(advertised) == set(CORE_TOOL_NAMES) | set(active)
    order = [t["function"]["name"] for t in TOOL_SPECS]
    core = [n for n in order if n in CORE_TOOL_NAMES]
    assert advertised == [*core, *active]
    # read_only keeps only the read-shaped part.
    read_advertised = {t["function"]["name"] for t in advertised_tool_specs(active, read_only=True)}
    assert read_advertised == (set(CORE_TOOL_NAMES) | set(active)) - MUTATING_TOOL_NAMES


def test_search_web_and_read_url_ride_the_core_for_roles_that_may_use_them(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """Phase 06 (6.2): the two web verbs every role may use are core — full
    schema on turn 1, no describe_tools round-trip. read_url still cards
    (it is in MUTATING_TOOL_NAMES), so a read-only turn keeps only
    search_web (I5: advertisement moved, permission did not)."""
    assert {"search_web", "read_url"} <= CORE_TOOL_NAMES
    assert "read_url" in MUTATING_TOOL_NAMES
    client = configured_client(config, ollama)
    for create in ({}, {"role": "builder"}, {"role": "assistant"}):
        chat_id = client.post("/api/chats", json=create).json()["chat_id"]
        ollama.script_reply("hello")
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"})
        advertised = {t["function"]["name"] for t in ollama.chat_bodies()[-1]["tools"]}
        assert {"search_web", "read_url"} <= advertised, create
    assert _line_for("search_web") == "- search_web"
    assert _line_for("read_url") == "- read_url*"
    read_only = {t["function"]["name"] for t in advertised_tool_specs([], read_only=True)}
    assert "search_web" in read_only and "read_url" not in read_only


def test_a_role_that_denies_the_web_is_offered_neither_web_verb(
    config: SupervisorConfig, ollama: FakeOllama, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Role-filtered (P3-6): core never overrides a role's deny — the verbs a
    role can never use are not in its specs and not in its index."""
    from skep.supervisor.policy_engine import Layer, LayerRule
    from skep.supervisor.roles import Role

    original = Role.layer

    def layer(self: Role) -> Layer | None:
        base = original(self)
        if self.name != "assistant" or base is None:
            return base
        denies = (
            LayerRule("network", "deny", "no-search", "search", "*"),
            LayerRule("network", "deny", "no-fetch", "fetch", "*"),
        )
        web = {("network", "search"), ("network", "fetch")}
        kept = tuple(r for r in base.rules if (r.scope, r.action) not in web)
        return Layer(base.name, base.label, rules=(*denies, *kept))

    monkeypatch.setattr(Role, "layer", layer)
    monkeypatch.setattr(
        tools, "_hidden_tools", functools.lru_cache(tools._hidden_tools.__wrapped__)
    )
    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={"role": "assistant"}).json()["chat_id"]
    ollama.script_reply("hello")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"})
    body = ollama.chat_bodies()[-1]
    advertised = {t["function"]["name"] for t in body["tools"]}
    assert not {"search_web", "read_url"} & advertised
    index = body["messages"][0]["content"].split("Tool index", 1)[1]
    assert "- search_web" not in index and "- read_url" not in index


def test_rare_admin_verbs_index_as_name_only_with_elided_args() -> None:
    """Out of front-line prominence, never out of reach: a rare admin verb's
    line is name*(…) and the legend says to describe it first."""
    assert len(RARE_ADMIN_TOOLS) >= 30
    assert {t["function"]["name"] for t in TOOL_SPECS} >= RARE_ADMIN_TOOLS
    assert not RARE_ADMIN_TOOLS & CORE_TOOL_NAMES
    assert _line_for("set_policy") == "- set_policy*(…)"
    assert _line_for("policy_registry") == "- policy_registry(…)"
    assert _line_for("discord_timeout_member") == "- discord_timeout_member*(…)"
    assert "'(…)'=a rare admin verb" in TOOL_INDEX_BLOCK
    assert "- set_policy*(" not in tool_index_block(frozenset({"set_policy"}))
