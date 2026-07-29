"""The conversation-skill observer (v53-F1, ADR 0029).

Watches completed chat turns for repeatable multi-step procedures and
proposes skill DRAFTS. Three postures, all load-bearing:

- **Opt-in, default OFF** (the v47-F7 precedent for ambient behavior) —
  a settings toggle, carded like every mutation.
- **Heuristic-only, no model call.** v1 detects one honest signal: a turn
  the assistant completed with several tool steps. Correction-detection
  and cross-chat repetition need analysis this sweep deliberately does
  not do (recorded in plans/v53).
- **A candidate never self-promotes (ADR 0016).** The output is a draft
  in the skill queue; the human approves.

The sweep runs on the ticker — never in the request path — and keeps a
message-id cursor so each transcript row is examined once.
"""

from __future__ import annotations

import re
from pathlib import PurePath

from .skills import DRAFT, SkillCandidate, candidate_signature
from .store import RunStore
from .templates import TemplateError, WorkflowTemplate, validate_template

OBSERVER_SETTING = "conversation_skill_observer"
_CURSOR_SETTING = "conversation_skill_observer_cursor"
MIN_TOOL_STEPS = 3
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def observer_enabled(store: RunStore) -> bool:
    return store.get_setting(OBSERVER_SETTING) is True


# -- v72-F4: the observation harvest — wiring the dead ingestion edge --------
#
# v71-F5 built the observation memory class (grants nothing, 14-day TTL,
# injected last, capped) and its governed write; nothing fed it. This sweep
# does, deterministically — no model call, no proposal, exactly the v71-F5
# rationale: content that asks to be ephemeral applies directly, permanence
# keeps the human gate. Always-on BECAUSE it is gated by explicit
# observation phrasing (the curator classifier) — a plain message never
# becomes memory.

OBSERVATION_CHAT_CURSOR = "observation_harvest_chat_cursor"
OBSERVATION_RUN_CURSOR = "observation_harvest_run_cursor"
HARVEST_CAP = 8
_OBSERVATION_CONTENT_CAP = 500
_RUN_TERMINAL_STATES = frozenset(
    {"completed", "failed", "rejected", "worker_timeout", "worker_crashed"}
)


def harvest_observations(store: RunStore) -> list[str]:
    """One sweep, two lanes: observation-shaped chat lines, and run
    terminals. Returns the created contents (for the tick log)."""
    created = _harvest_chat_lines(store)
    created.extend(_harvest_run_terminals(store))
    return created


def _harvest_chat_lines(store: RunStore) -> list[str]:
    from skep.workers.curator import classify_memory_class

    raw_cursor = store.get_setting(OBSERVATION_CHAT_CURSOR)
    if not isinstance(raw_cursor, int):
        # First sweep after upgrade: start at NOW — a history-wide harvest
        # burst would be noise wearing a feature's face.
        newest = store.chat_messages_after(0, limit=1)
        top = 0
        while newest:
            top = newest[-1].id
            newest = store.chat_messages_after(top, limit=500)
        store.set_setting(OBSERVATION_CHAT_CURSOR, top)
        return []
    created: list[str] = []
    cursor = raw_cursor
    # cap+1 so a full batch advances the cursor only through what it wrote.
    for message in store.chat_messages_after(cursor, limit=HARVEST_CAP + 1):
        if len(created) >= HARVEST_CAP:
            break  # cursor stays before this row; the next sweep resumes here
        cursor = message.id
        if message.role not in ("user", "assistant"):
            continue
        text = message.content.strip()
        if not text:
            continue
        chat = store.get_chat(message.chat_id)
        project_id = None if chat is None else chat.project_id
        if classify_memory_class(text, has_project=project_id is not None) != "observation":
            continue
        store.add_memory_item(
            memory_class="observation",
            content=text[:_OBSERVATION_CONTENT_CAP],
            actor="chat-observer",
            project_id=project_id,
        )
        created.append(text[:80])
    if cursor != raw_cursor:
        store.set_setting(OBSERVATION_CHAT_CURSOR, cursor)
    return created


def _harvest_run_terminals(store: RunStore) -> list[str]:
    raw_cursor = store.get_setting(OBSERVATION_RUN_CURSOR)
    if not isinstance(raw_cursor, str):
        latest = store.recent_runs(1)
        store.set_setting(
            OBSERVATION_RUN_CURSOR, latest[0].updated_at if latest else "1970-01-01T00:00:00Z"
        )
        return []
    created: list[str] = []
    cursor = raw_cursor
    # ponytail: second-resolution watermark over recent_runs(50) — two runs
    # sharing a terminal second across a sweep boundary can drop one line;
    # a run-id journal is the upgrade path if the field ever shows it.
    for run in sorted(store.recent_runs(50), key=lambda r: r.updated_at):
        if run.updated_at <= raw_cursor or run.state not in _RUN_TERMINAL_STATES:
            continue
        summary = (run.summary or run.verification_details or "no summary").splitlines()[0]
        content = (
            f"observation: run {run.task_id[:8]} on {PurePath(run.repo).name} "
            f"{run.state}: {summary}"
        )[:_OBSERVATION_CONTENT_CAP]
        store.add_memory_item(memory_class="observation", content=content, actor="run-observer")
        created.append(content[:80])
        cursor = max(cursor, run.updated_at)
    if cursor != raw_cursor:
        store.set_setting(OBSERVATION_RUN_CURSOR, cursor)
    return created


def observe_conversations(store: RunStore) -> list[str]:
    """One sweep: draft proposals for multi-step turns since the cursor.

    Returns the names of the drafts created (for the tick log)."""
    if not observer_enabled(store):
        return []
    raw_cursor = store.get_setting(_CURSOR_SETTING)
    cursor = raw_cursor if isinstance(raw_cursor, int) else 0
    created: list[str] = []
    new_cursor = cursor
    for chat_id, max_id in store.chats_with_messages_after(cursor):
        new_cursor = max(new_cursor, max_id)
        name = _draft_from_last_turn(store, chat_id)
        if name is not None:
            created.append(name)
    if new_cursor != cursor:
        store.set_setting(_CURSOR_SETTING, new_cursor)
    return created


def _draft_from_last_turn(store: RunStore, chat_id: str) -> str | None:
    messages = store.chat_messages(chat_id)
    last_user = next(
        (i for i in range(len(messages) - 1, -1, -1) if messages[i].role == "user"), None
    )
    if last_user is None:
        return None
    turn = messages[last_user:]
    tools = [m.tool_name for m in turn if m.role == "tool" and m.tool_name]
    if len(tools) < MIN_TOOL_STEPS or turn[-1].role != "assistant":
        return None
    ask = turn[0].content.strip()
    if not ask:
        return None
    name = "conv-" + (_SLUG_RE.sub("-", ask.lower()).strip("-")[:40] or chat_id[:8])
    steps = "\n".join(f"{index}. {tool}" for index, tool in enumerate(tools, start=1))
    template = WorkflowTemplate(
        name=name,
        description=f"observed conversation procedure: {ask[:80]}",
        instructions=(
            "Observed multi-step procedure (conversation draft — refine before "
            f"approving).\n\nWhen the user asks: {ask[:400]!r}\n"
            f"the assistant completed it with these tool steps:\n{steps}"
        ),
        provenance="conversation",
    )
    try:
        validate_template(template)
    except TemplateError:
        return None
    signature = candidate_signature(template)
    if store.get_candidate(name) is not None or store.get_template(name) is not None:
        return None
    if any(c.signature == signature for c in store.list_candidates()):
        return None
    store.add_candidate(
        SkillCandidate(
            name=name,
            signature=signature,
            status=DRAFT,
            template=template,
            source_task_ids=(f"chat:{chat_id}",),
            occurrences=1,
        )
    )
    return name
