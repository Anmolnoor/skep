"""v53-F1 (ADR 0029): the conversation-skill observer + curator.

Observer: opt-in (default OFF), heuristic-only, runs on the ticker,
proposes DRAFTS. Curator: surfaces the aging queue, never acts. The human
gate is unchanged for learned skills; conversation drafts get the human
gate without the (meaningless) worker test — the v51-F4 reasoning.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from skep.supervisor.observe import OBSERVER_SETTING, observe_conversations
from skep.supervisor.skill_cmds import SkillError, approve
from skep.supervisor.skill_curator import stale_drafts
from skep.supervisor.skills import DRAFT, SkillCandidate, candidate_signature
from skep.supervisor.store import RunStore
from skep.supervisor.templates import WorkflowTemplate


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[RunStore]:
    store = RunStore(tmp_path / "supervisor.sqlite3")
    yield store
    store.close()


def _seed_multi_tool_turn(store: RunStore, *, tools: int = 3) -> str:
    chat = store.create_chat(title="ops", model=None)
    store.add_chat_message(chat.chat_id, role="user", content="run my release checklist")
    for index in range(tools):
        store.add_chat_message(chat.chat_id, role="tool", content="{}", tool_name=f"tool_{index}")
    store.add_chat_message(chat.chat_id, role="assistant", content="done, all steps ran")
    return chat.chat_id


def test_observer_is_off_by_default(store: RunStore) -> None:
    _seed_multi_tool_turn(store)
    assert observe_conversations(store) == []
    assert store.list_candidates() == []


def test_observer_proposes_a_draft_for_a_multi_step_turn(store: RunStore) -> None:
    store.set_setting(OBSERVER_SETTING, True)
    chat_id = _seed_multi_tool_turn(store)

    created = observe_conversations(store)

    assert created == ["conv-run-my-release-checklist"]
    (candidate,) = store.list_candidates()
    assert candidate.status == DRAFT
    assert candidate.template.provenance == "conversation"
    assert candidate.source_task_ids == (f"chat:{chat_id}",)
    assert "1. tool_0" in candidate.template.instructions
    # A draft never reaches the registry by itself (ADR 0016).
    assert store.get_template(candidate.name) is None

    # The cursor advanced: a second sweep proposes nothing new.
    assert observe_conversations(store) == []


def test_observer_skips_short_turns(store: RunStore) -> None:
    store.set_setting(OBSERVER_SETTING, True)
    _seed_multi_tool_turn(store, tools=2)
    assert observe_conversations(store) == []


def test_conversation_draft_gets_the_human_gate_without_a_worker_test(
    store: RunStore,
) -> None:
    store.set_setting(OBSERVER_SETTING, True)
    _seed_multi_tool_turn(store)
    (name,) = observe_conversations(store)

    _candidate, registry_name = approve(store, name, actor="tester")

    assert registry_name == name
    template = store.get_template(name)
    assert template is not None
    assert template.provenance == "conversation"  # the generator is recorded

    # A LEARNED draft still needs its test first — the gate is unchanged.
    learned = WorkflowTemplate(name="learned-x", instructions="do x", provenance="learned")
    store.add_candidate(
        SkillCandidate(
            name="learned-x",
            signature=candidate_signature(learned),
            status=DRAFT,
            template=learned,
            source_task_ids=("t-1",),
            occurrences=2,
        )
    )
    with pytest.raises(SkillError, match="test it first"):
        approve(store, "learned-x", actor="tester")


def test_curator_surfaces_stale_drafts_and_never_acts(store: RunStore) -> None:
    store.set_setting(OBSERVER_SETTING, True)
    _seed_multi_tool_turn(store)
    (name,) = observe_conversations(store)

    fresh = stale_drafts(store)
    assert fresh == []  # just created — not stale

    future = datetime(2099, 1, 1, tzinfo=UTC)
    stale = stale_drafts(store, now=future)
    assert [candidate.name for candidate in stale] == [name]
    # Surfacing changed nothing: the draft is still a draft, still stored.
    (candidate,) = store.list_candidates()
    assert candidate.status == DRAFT


def test_digest_names_the_aging_skill_queue(
    store: RunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import skill_curator
    from skep.supervisor.scheduler import compose_digest

    store.set_setting(OBSERVER_SETTING, True)
    _seed_multi_tool_turn(store)
    (name,) = observe_conversations(store)

    monkeypatch.setattr(skill_curator, "STALE_DRAFT_DAYS", -1)  # everything is stale
    digest = compose_digest(store)
    assert f"skill drafts waiting >30d: {name}" in digest
