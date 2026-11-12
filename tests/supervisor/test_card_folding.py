"""ADR 0059 P4-2 (D6, V10): folding — one question, one card.

Pending cards in one chat with the same ``(tool, scope, matcher subject)``
fold into ONE card listing every item; one confirm or deny resolves them all
through the one claim (PR #61). HIGH cards never fold. A fold lives for one
model round: its card is shown when the round closes, nothing can confirm it
before (the chat's turn lock), and a same-subject call in a later round is a
new card — so no item ever joins a card the operator has already seen."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException

from skep.supervisor.serve import chat as chat_module
from skep.supervisor.serve.cards import fold_subject
from skep.supervisor.serve.ticker import Ticker

from .card_rig import Live, cards, live_fixture  # noqa: F401  (the `live` fixture)
from .race import race
from .taint_grid import set_taint

A1, A2, A3 = (f"https://unknown.example/docs/{n}" for n in (1, 2, 3))


def _fetch_calls(urls: tuple[str, ...]) -> tuple[tuple[str, dict[str, Any]], ...]:
    return tuple(("read_url", {"url": url}) for url in urls)


def _tool_rows(env: Live, chat: str, tool: str) -> list[dict[str, Any]]:
    return [
        json.loads(m.content)
        for m in env.store.chat_messages(chat)
        if m.role == "tool" and m.tool_name == tool
    ]


def test_same_host_fetches_in_one_round_fold_into_one_card_and_each_runs_once(
    live: Any,
) -> None:
    env = live("builder")
    chat = env.chat()
    env.ollama.script_tool_calls(*_fetch_calls((A1, A2, A3)))
    (card,) = cards(env.turn(chat, "read the three docs pages"))
    assert card["card"]["items"] == [f"read_url — {u}" for u in (A1, A2, A3)]
    assert card["card"]["headline"] == f"read_url — {A1} (+2 more)"
    assert "unknown.example/docs/" in card["card"]["keep_covers"]
    (pending,) = env.store.pending_chat_actions(chat)
    assert [item["args"]["url"] for item in pending.fold_items()] == [A1, A2, A3]

    env.ollama.script_reply("read them")
    events = env.verdict(chat, card["action_id"], confirm=True, keep="once")
    assert [f["url"] for f in env.fetched] == [A1, A2, A3]  # each exactly once
    assert len([e for e, d in events if e == "tool" and d["tool"] == "read_url"]) == 3
    assert [row["ok"] for row in _tool_rows(env, chat, "read_url")] == [True] * 3
    resolved = env.store.get_chat_action(card["action_id"])
    assert resolved is not None and resolved.status == "confirmed"
    assert len(resolved.result["items"]) == 3


def test_deny_denies_every_item(live: Any) -> None:
    env = live("builder")
    chat = env.chat()
    env.ollama.script_tool_calls(*_fetch_calls((A1, A2)))
    (card,) = cards(env.turn(chat, "read both"))
    env.ollama.script_reply("ok, not reading")
    env.verdict(chat, card["action_id"], confirm=False)
    assert env.fetched == []
    rows = _tool_rows(env, chat, "read_url")
    assert len(rows) == 2 and all(row.get("denied") for row in rows)


def test_different_subjects_do_not_fold(live: Any) -> None:
    env = live("builder")
    chat = env.chat()
    env.ollama.script_tool_calls(
        ("read_url", {"url": A1}),
        ("read_url", {"url": "https://other.example/x"}),
        ("read_url", {"url": "http://unknown.example/docs/1"}),  # another scheme
        ("read_url", {"url": "https://unknown.example:8443/docs/1"}),  # another port
    )
    found = cards(env.turn(chat, "read them"))
    assert len(found) == 4
    assert all("items" not in card["card"] for card in found)


def test_an_identical_call_in_the_round_is_not_a_second_item(live: Any) -> None:
    env = live("builder")
    chat = env.chat()
    env.ollama.script_tool_calls(*_fetch_calls((A1, A2, A2)))
    (card,) = cards(env.turn(chat, "read them"))
    assert len(card["card"]["items"]) == 2
    env.ollama.script_reply("done")
    env.verdict(chat, card["action_id"], confirm=True, keep="once")
    assert [f["url"] for f in env.fetched] == [A1, A2]


def test_taint_asked_searches_in_a_round_fold_and_run_once_each(live: Any) -> None:
    """P4-1 waited the second search on the first card; P4-2 lists it."""
    env = live("builder")
    chat = env.chat()
    set_taint(env.store, chat, "private")
    env.ollama.script_tool_calls(("search_web", {"query": "a"}), ("search_web", {"query": "b"}))
    (card,) = cards(env.turn(chat, "search both"))
    assert card["decision"]["decided_by"] == "taint/private-egress"
    assert card["card"]["items"] == ["search_web — a", "search_web — b"]
    assert env.searched == []
    env.ollama.script_reply("found")
    env.verdict(chat, card["action_id"], confirm=True)
    assert env.searched == ["a", "b"]


def test_a_high_card_never_folds(live: Any) -> None:
    env = live("builder")
    chat = env.chat()
    set_taint(env.store, chat, "both")
    env.ollama.script_tool_calls(*_fetch_calls((A1, A2)))
    found = cards(env.turn(chat, "read both"))
    assert len(found) == 2  # two HIGH cards, each its own question
    assert all(c["card"]["tier"] == "high" and "items" not in c["card"] for c in found)
    assert all(len(p.fold_items()) == 1 for p in env.store.pending_chat_actions(chat))


def test_a_later_round_never_joins_a_card_already_shown(live: Any) -> None:
    """The late item: a pending card from an earlier round is never widened —
    the same subject asked again later is a card of its own."""
    env = live("builder")
    chat = env.chat()
    env.ollama.script_tool_call("read_url", {"url": A1})
    (first,) = cards(env.turn(chat, "read one"))
    env.ollama.script_tool_call("read_url", {"url": A2})
    (second,) = cards(env.turn(chat, "and the other"))  # bypasses the composer's 409
    assert second["action_id"] != first["action_id"]
    shown = env.store.get_chat_action(first["action_id"])
    assert shown is not None and len(shown.fold_items()) == 1


def test_a_claimed_card_takes_no_new_item(live: Any) -> None:
    env = live("builder")
    chat = env.chat()
    action_id = env.store.add_chat_action(chat, tool="read_url", args={"url": A1})
    assert env.store.claim_chat_action(action_id)
    assert not env.store.fold_into_chat_action(action_id, args={"url": A2}, tool_call_id=None)
    action = env.store.get_chat_action(action_id)
    assert action is not None and len(action.fold_items()) == 1


def test_no_confirm_can_land_while_the_round_that_folds_is_open(live: Any) -> None:
    """The fold is built while the turn holds the chat's lock; a confirm needs
    that lock first, so it cannot act on a half-built card."""
    env = live("builder")
    chat = env.chat()
    action_id = env.store.add_chat_action(chat, tool="read_url", args={"url": A1})
    lock = chat_module.take_turn(chat)
    try:
        with pytest.raises(HTTPException) as busy:
            env.verdict(chat, action_id, confirm=True, keep="once")
        assert busy.value.status_code == 409
    finally:
        lock.release()
    assert env.fetched == []


def test_fold_subject_per_tool() -> None:
    same = fold_subject("read_url", {"url": A1}, "x")
    assert same == fold_subject("read_url", {"url": "https://UNKNOWN.example:443/z?q=1"}, "x")
    assert same != fold_subject("read_url", {"url": A1}, "y")  # another rule asked
    assert fold_subject("read_url", {"url": "https://u:p@unknown.example/"}, "x") is None
    assert fold_subject("read_url", {"url": "ftp://unknown.example/"}, "x") is None
    assert fold_subject("read_url", {"url": "https://h:notaport/"}, "x") is None
    assert fold_subject("search_web", {"query": "a"}, "t") == fold_subject(
        "search_web", {"query": "b"}, "t"
    )
    for tool in ("run_shell", "dispatch_run", "read_file", "call_mcp_tool"):
        assert fold_subject(tool, {"command": "ls"}, "x") is None


# --- races: every item runs at most once, the card ends in one final state ---------------


class _Held:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, tool: str, args: dict[str, Any], **_: Any) -> Any:
        self.calls.append(str(args.get("url")))
        self.entered.set()
        self.release.wait(3)
        return {"text": "page"}


def _folded_card(env: Live, chat: str, *, age_seconds: int = 0) -> str:
    stamp = (datetime.now(UTC) - timedelta(seconds=age_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with pytest.MonkeyPatch.context() as m:
        m.setattr("skep.supervisor.store._now", lambda: stamp)
        action_id = env.store.add_chat_action(chat, tool="read_url", args={"url": A1})
    env.store.fold_into_chat_action(action_id, args={"url": A2}, tool_call_id="c2")
    return action_id


@pytest.mark.parametrize("second", ["web", "channel"])
def test_two_confirms_on_a_folded_card_run_each_item_once(
    live: Any, monkeypatch: pytest.MonkeyPatch, second: str
) -> None:
    env = live("builder")
    held = _Held()
    monkeypatch.setattr(chat_module, "execute_mutation", held)
    chat = env.chat()
    action_id = _folded_card(env, chat)
    env.ollama.script_reply("done")
    actors = {"web": "chat-user", "channel": "channel:discord:42"}

    def confirm(actor: str) -> Any:
        try:
            return env.verdict(chat, action_id, confirm=True, keep="once", actor=actor)
        finally:
            held.release.set()

    outcomes = race(lambda: confirm("chat-user"), lambda: confirm(actors[second]))
    assert sorted(held.calls) == [A1, A2]
    losers = [o for o in outcomes if isinstance(o, HTTPException)]
    assert len(losers) == 1 and losers[0].status_code == 409
    action = env.store.get_chat_action(action_id)
    assert action is not None and action.status == "confirmed"
    assert len(_tool_rows(env, chat, "read_url")) == 2


def test_a_deny_loses_to_a_folded_confirm_already_executing(
    live: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = live("builder")
    held = _Held()
    monkeypatch.setattr(chat_module, "execute_mutation", held)
    chat = env.chat()
    action_id = _folded_card(env, chat)
    env.ollama.script_reply("done")
    confirmer = threading.Thread(
        target=env.verdict, args=(chat, action_id), kwargs={"confirm": True, "keep": "once"}
    )
    confirmer.start()
    assert held.entered.wait(3)
    with pytest.raises(HTTPException) as lost:
        env.verdict(chat, action_id, confirm=False)
    held.release.set()
    confirmer.join(5)
    assert lost.value.status_code == 409
    assert sorted(held.calls) == [A1, A2]
    action = env.store.get_chat_action(action_id)
    assert action is not None and action.status == "confirmed"
    assert not any(row.get("denied") for row in _tool_rows(env, chat, "read_url"))


def test_the_ticker_never_denies_a_folded_card_mid_confirm_and_expires_every_item(
    live: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = live("builder")
    held = _Held()
    monkeypatch.setattr(chat_module, "execute_mutation", held)
    chat = env.chat()
    running = _folded_card(env, chat, age_seconds=3600)
    env.ollama.script_reply("done")
    ticker = Ticker(env.holder, env.store)
    monkeypatch.setattr(ticker, "_notify", lambda *a, **k: None)
    confirmer = threading.Thread(
        target=env.verdict, args=(chat, running), kwargs={"confirm": True, "keep": "once"}
    )
    confirmer.start()
    assert held.entered.wait(3)
    ticker._expire_cards()
    held.release.set()
    confirmer.join(5)
    done = env.store.get_chat_action(running)
    assert done is not None and done.status == "confirmed"
    assert sorted(held.calls) == [A1, A2]

    other = env.chat()
    idle = _folded_card(env, other, age_seconds=3600)
    ticker._expire_cards()
    expired = env.store.get_chat_action(idle)
    assert expired is not None and expired.status == "denied"
    rows = _tool_rows(env, other, "read_url")
    assert len(rows) == 2 and all(row.get("auto") for row in rows)


def test_a_restart_mid_fold_marks_every_item_interrupted(live: Any) -> None:
    env = live("builder")
    chat = env.chat()
    action_id = _folded_card(env, chat)
    assert env.store.claim_chat_action(action_id)
    assert env.store.interrupt_executing_chat_actions() == 1
    rows = _tool_rows(env, chat, "read_url")
    assert len(rows) == 2 and all(row.get("interrupted") for row in rows)
