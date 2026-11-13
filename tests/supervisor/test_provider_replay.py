"""ES-F2 (B28): a chat's stored tool history replays cleanly on every protocol.

History stores tool calls Ollama-native; every other protocol translates them
on the send path. The strict fakes (strict_wire) refuse the shapes the real
APIs refuse, so each case drives the real chat engine through a tool round,
a follow-up message (which replays that history) and a confirmed card's
continuation — the three paths that resend stored calls and results.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from skep.supervisor import RunStore, SupervisorConfig

from .conftest import serve_client
from .fake_anthropic import FakeAnthropic
from .fake_bedrock import FakeBedrock
from .fake_ollama import FakeOllama
from .fake_openai import FakeOpenAI
from .fake_responses import FakeResponses
from .test_serve_chat import sse_events

FAKES: dict[str, Any] = {
    "ollama": FakeOllama,
    "openai-compat": FakeOpenAI,
    "anthropic": FakeAnthropic,
    "openai-responses": FakeResponses,
    "bedrock": FakeBedrock,
}


@pytest.fixture(params=sorted(FAKES))
def provider(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    fake = FAKES[request.param]().start()
    fake.protocol = request.param
    yield fake
    fake.stop()


def test_tool_history_replays_on_every_protocol(config: SupervisorConfig, provider: Any) -> None:
    client = serve_client(config)
    client.put(
        "/api/llm/config",
        json={
            "base_url": provider.base_url,
            "default_model": "m",
            "protocol": provider.protocol,
            "api_key": "sk-fake",
        },
    )
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]

    # Turn 1: a read tool round, then an answer.
    provider.script_tool_call("list_runs", {"limit": 5})
    provider.script_reply("no runs yet")
    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "runs?"}).text
    )
    assert events[-1] == ("done", {"state": "complete"})

    # Turn 2 replays turn 1's call and result; its mutation cards.
    provider.script_tool_call("set_policy", {"auto_approve": True})
    events = sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "on"}).text)
    assert events[-1] == ("done", {"state": "awaiting_confirmation"})

    # The confirm continuation replays both calls and both results.
    action_id = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]
    provider.script_reply("done")
    events = sse_events(client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm").text)
    assert events[-1] == ("done", {"state": "complete"})
    assert len(provider.chat_bodies()) == 4


def test_ollama_native_history_replays_after_switching_to_openai_compat(
    config: SupervisorConfig,
) -> None:
    """Native ollama stores its calls with no id; chat/completions requires
    one on every call and a matching tool_call_id on every result."""
    ollama, openai = FakeOllama().start(), FakeOpenAI().start()
    try:
        client = serve_client(config)
        native = {"base_url": ollama.base_url, "default_model": "m", "protocol": "ollama"}
        client.put("/api/llm/config", json=native)
        chat_id = client.post("/api/chats", json={}).json()["chat_id"]
        ollama.script_tool_call("list_runs", {"limit": 5})
        ollama.script_tool_call("list_runs", {"limit": 6})
        ollama.script_reply("none")
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "runs?"})

        compat = {**native, "base_url": openai.base_url, "protocol": "openai-compat"}
        client.put("/api/llm/config", json=compat)
        openai.script_reply("still none")
        events = sse_events(
            client.post(f"/api/chats/{chat_id}/messages", json={"content": "again"}).text
        )
        assert events[-1] == ("done", {"state": "complete"})
        sent = openai.chat_bodies()[0]["messages"]
        calls = [m["tool_calls"][0]["id"] for m in sent if m.get("tool_calls")]
        results = [m["tool_call_id"] for m in sent if m["role"] == "tool"]
        assert len(set(calls)) == 2 and results == calls
    finally:
        ollama.stop()
        openai.stop()


def test_a_stored_garbled_tool_call_replays_in_a_valid_shape(
    config: SupervisorConfig, provider: Any
) -> None:
    """ES-F10 (B84): ES-F8 stores a garbled arguments string as-is; resent
    verbatim it drew a 400 on every later turn (ollama.com, both protocols),
    wedging the chat. The replay sends an empty object instead — the raw
    text already lives in the error row that answered the call."""
    client = serve_client(config)
    client.put(
        "/api/llm/config",
        json={
            "base_url": provider.base_url,
            "default_model": "m",
            "protocol": provider.protocol,
            "api_key": "sk-fake",
        },
    )
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    store = RunStore(config.db_path)
    try:
        # The exact rows ES-F8 writes for a truncated-JSON call (lane M1-20).
        store.add_chat_message(chat_id, role="user", content="runs?")
        garbled = {"id": "call_bad", "function": {"name": "list_runs", "arguments": '{"limit": 5'}}
        store.add_chat_message(chat_id, role="assistant", content="", tool_calls=[garbled])
        error = '{"ok": false, "error": "the arguments for list_runs were not a JSON object"}'
        store.add_chat_message(
            chat_id, role="tool", tool_name="list_runs", tool_call_id="call_bad", content=error
        )
        store.add_chat_message(chat_id, role="assistant", content="sorry, bad call")
    finally:
        store.close()

    for turn in ("say pong", "again"):
        provider.script_reply("pong")
        events = sse_events(
            client.post(f"/api/chats/{chat_id}/messages", json={"content": turn}).text
        )
        assert events[-1] == ("done", {"state": "complete"})
    # The raw text never reaches the wire (json.dumps escapes its quotes).
    assert '{\\"limit\\": 5' not in json.dumps(provider.chat_bodies())


def test_wrapped_outside_rows_replay_on_every_strict_protocol(
    config: SupervisorConfig, provider: Any
) -> None:
    """P4-1 (B26): a webhook row, a worker's report and a script's output sit
    in the transcript as wrapped user turns — next to the operator's own
    message, a tool round and skep's prompt — and every strict provider
    accepts the replay; none of them is ever sent as the assistant."""
    client = serve_client(config)
    client.put(
        "/api/llm/config",
        json={"base_url": provider.base_url, "default_model": "m",
              "protocol": provider.protocol, "api_key": "sk-fake"},
    )  # fmt: skip
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    store = RunStore(config.db_path)
    try:
        store.add_chat_message(chat_id, role="user", content="ci failed", provenance="webhook:ci")
        store.add_chat_message(chat_id, role="user", content="done", provenance="worker:t1")
        store.add_chat_message(chat_id, role="user", content="Continue.", provenance="queen")
        store.add_chat_message(chat_id, role="user", content="x", provenance="schedule:s")
    finally:
        store.close()
    provider.script_tool_call("list_runs", {"limit": 5})
    provider.script_reply("ok")
    events = sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "?"}).text)
    assert events[-1] == ("done", {"state": "complete"})
    sent = json.dumps(provider.chat_bodies()[-1])
    for source in ("webhook:ci", "worker:t1", "schedule:s"):
        assert f'<untrusted source=\\"{source}\\">' in sent, (source, sent)


def test_an_outside_row_landing_while_a_card_waits_replays_after_its_result(
    config: SupervisorConfig, provider: Any
) -> None:
    """P4-1 follow-up: a webhook that fires while a card waits stores its row
    between the card's tool call and the result the confirm writes. Replayed
    in arrival order, the wrapped user turn split the call from its result and
    the confirm's continuation was refused (anthropic, bedrock: the result
    must lead the user turn; openai-compat: the call is unanswered) — and so
    was every later turn. It now replays after the result."""
    client = serve_client(config)
    client.put(
        "/api/llm/config",
        json={"base_url": provider.base_url, "default_model": "m",
              "protocol": provider.protocol, "api_key": "sk-fake"},
    )  # fmt: skip
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    provider.script_tool_call("set_policy", {"auto_approve": True})
    events = sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "on"}).text)
    assert events[-1] == ("done", {"state": "awaiting_confirmation"})
    store = RunStore(config.db_path)
    try:
        store.add_chat_message(chat_id, role="user", content="ci failed", provenance="webhook:ci")
    finally:
        store.close()
    action_id = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]
    provider.script_reply("done")
    events = sse_events(client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm").text)
    assert events[-1] == ("done", {"state": "complete"})
    provider.script_reply("seen")
    events = sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "?"}).text)
    assert events[-1] == ("done", {"state": "complete"})
    assert '<untrusted source=\\"webhook:ci\\">' in json.dumps(provider.chat_bodies()[-1])


def test_a_btw_beside_a_pending_card_replays_on_every_strict_protocol(
    config: SupervisorConfig, provider: Any
) -> None:
    """W23 (B62): a /btw while a card waits replayed the card's tool call with
    no result yet, and four strict protocols refused the side turn. The side
    turn leaves the open round out; the card stays pending, and its confirm
    and the next turn replay cleanly too."""
    client = serve_client(config)
    client.put(
        "/api/llm/config",
        json={"base_url": provider.base_url, "default_model": "m",
              "protocol": provider.protocol, "api_key": "sk-fake"},
    )  # fmt: skip
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    provider.script_tool_call("set_policy", {"auto_approve": True})
    events = sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "on"}).text)
    assert events[-1] == ("done", {"state": "awaiting_confirmation"})
    provider.script_reply("side answer")
    events = sse_events(
        client.post(
            f"/api/chats/{chat_id}/messages", json={"content": "what time?", "read_only": True}
        ).text
    )
    assert events[-1] == ("done", {"state": "complete"}), events
    side = json.dumps(provider.chat_bodies()[-1])
    assert 'auto_approve\\": true' not in side and '"auto_approve": true' not in side
    actions = client.get(f"/api/chats/{chat_id}").json()["actions"]
    assert [a["status"] for a in actions] == ["proposed"]
    provider.script_reply("done")
    confirm = f"/api/chats/{chat_id}/actions/{actions[0]['action_id']}/confirm"
    events = sse_events(client.post(confirm).text)
    assert events[-1] == ("done", {"state": "complete"}), events
    provider.script_reply("seen")
    events = sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "?"}).text)
    assert events[-1] == ("done", {"state": "complete"}), events


def test_a_round_with_no_result_never_wedges_a_turn_on_any_strict_protocol(
    config: SupervisorConfig, provider: Any
) -> None:
    """W23 review: a run-finished continuation (an unattended turn) that
    arrives while a card waits replayed the card's call with no result, and
    four strict protocols refused it — the continuation was lost. A round a
    client disconnect left without a result (B25) refused every later turn
    the same way. Every turn now leaves a round still waiting out of its
    replay; the card's confirm and the next turn replay it whole."""
    from skep.supervisor.serve.chat import ChatEngine
    from skep.supervisor.serve.jobs import Dispatcher
    from skep.supervisor.serve.settings import ConfigHolder

    client = serve_client(config)
    client.put(
        "/api/llm/config",
        json={"base_url": provider.base_url, "default_model": "m",
              "protocol": provider.protocol, "api_key": "sk-fake"},
    )  # fmt: skip
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    provider.script_tool_call("set_policy", {"auto_approve": True})
    events = sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "on"}).text)
    assert events[-1] == ("done", {"state": "awaiting_confirmation"})
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        engine = ChatEngine(
            store=store, holder=holder, runner=Dispatcher(store=store, holder=holder),
            home=config.home,
        )  # fmt: skip
        store.add_chat_message(chat_id, role="user", content="done: ok", provenance="worker:t1")
        store.add_chat_message(chat_id, role="user", content="Continue.", provenance="queen")
        base_url, api_key, model, protocol = engine.resolved_llm(store.get_chat(chat_id))
        provider.script_reply("the run finished")
        events = list(
            engine.turn_events(
                chat_id, base_url=base_url, api_key=api_key, model=model, protocol=protocol,
                unattended=True,
            )
        )  # fmt: skip
        assert events[-1] == ("done", {"state": "complete"}), events
        action_id = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]
        provider.script_reply("done")
        events = sse_events(client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm").text)
        assert events[-1] == ("done", {"state": "complete"}), events

        # B25: a round a disconnect left with one call unanswered and no card
        calls = [{"id": "c1", "function": {"name": "list_repos", "arguments": {}}},
                 {"id": "c2", "function": {"name": "list_runs", "arguments": {}}}]  # fmt: skip
        store.add_chat_message(chat_id, role="assistant", content="", tool_calls=calls)
        store.add_chat_message(
            chat_id, role="tool", tool_name="list_repos", tool_call_id="c1", content="{}"
        )
    finally:
        store.close()
    provider.script_reply("seen")
    events = sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "?"}).text)
    assert events[-1] == ("done", {"state": "complete"}), events


def test_a_chat_replays_on_every_strict_protocol_after_trust(
    config: SupervisorConfig, provider: Any
) -> None:
    """W20 review: /trust writes a system line into the chat for the
    operator. Replayed as a system message mid-conversation, openai-compat
    refused every later turn. A stored system row is a note for whoever reads
    the transcript and is not replayed; the outside row stays wrapped."""
    client = serve_client(config)
    client.put(
        "/api/llm/config",
        json={"base_url": provider.base_url, "default_model": "m",
              "protocol": provider.protocol, "api_key": "sk-fake"},
    )  # fmt: skip
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    provider.script_reply("hi")
    sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "a"}).text)
    store = RunStore(config.db_path)
    try:
        store.add_chat_message(chat_id, role="user", content="ci failed", provenance="webhook:ci")
    finally:
        store.close()
    assert client.post(f"/api/chats/{chat_id}/trust", json={"bits": []}).status_code == 200
    provider.script_reply("ok")
    events = sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "b"}).text)
    assert events[-1] == ("done", {"state": "complete"}), events
    body = json.dumps(provider.chat_bodies()[-1])
    assert "trusted this chat again" not in body and "ci failed" in body


# --- D18 (ES-F106): a card click is recorded at once; its follow-up waits --------------


def _d18_rig(config: SupervisorConfig, provider: Any) -> tuple[Any, str, RunStore, Any]:
    from skep.supervisor.serve.chat import ChatEngine
    from skep.supervisor.serve.jobs import Dispatcher
    from skep.supervisor.serve.settings import ConfigHolder

    client = serve_client(config)
    client.put(
        "/api/llm/config",
        json={"base_url": provider.base_url, "default_model": "m",
              "protocol": provider.protocol, "api_key": "sk-fake"},
    )  # fmt: skip
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    store = RunStore(config.db_path)
    holder = ConfigHolder(config, store)
    engine = ChatEngine(
        store=store, holder=holder, runner=Dispatcher(store=store, holder=holder),
        home=config.home,
    )  # fmt: skip
    return client, chat_id, store, engine


def _card(client: Any, provider: Any, chat_id: str, call: tuple[str, dict[str, Any]]) -> str:
    provider.script_tool_call(*call)
    events = sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "go"}).text)
    assert events[-1] == ("done", {"state": "awaiting_confirmation"}), events
    (action,) = client.get(f"/api/chats/{chat_id}").json()["actions"]
    return str(action["action_id"])


def _two_call_round(store: RunStore, chat_id: str, *, fold: bool) -> list[str]:
    """The rows a round with two carded calls writes (one fake per protocol
    scripts one call per reply): two cards, or one card folding both."""
    calls = [("set_personality", {"value": "terse"}), ("set_personality", {"value": "warm"})]
    store.add_chat_message(chat_id, role="user", content="go")
    wire = [
        {"id": f"call_d18_{n}", "function": {"name": name, "arguments": args}}
        for n, (name, args) in enumerate(calls)
    ]
    store.add_chat_message(chat_id, role="assistant", content="", tool_calls=wire)
    ids = [
        store.add_chat_action(chat_id, tool=name, args=args, tool_call_id=f"call_d18_{n}")
        for n, (name, args) in enumerate(calls[:1] if fold else calls)
    ]
    if fold:
        store.fold_into_chat_action(ids[0], args=calls[1][1], tool_call_id="call_d18_1")
    return ids


def _other_turn(engine: Any, store: RunStore, chat_id: str) -> Any:
    """A run-finish follow-up's rows and its (unattended) turn — the caller
    holds the chat's turn lock, as run_completion_turn does."""
    store.add_chat_message(chat_id, role="user", content="done: ok", provenance="worker:t1")
    store.add_chat_message(chat_id, role="user", content="Continue.", provenance="queen")
    base_url, api_key, model, protocol = engine.resolved_llm(store.get_chat(chat_id))
    return engine.turn_events(
        chat_id, base_url=base_url, api_key=api_key, model=model, protocol=protocol,
        unattended=True,
    )  # fmt: skip


def _until_open_round(turn: Any) -> None:
    """Run the turn to the point where its own tool round is open (its call
    stored, its result not yet)."""
    for event, data in turn:
        if event == "turn_status" and data.get("state") == "tool":
            return
    raise AssertionError("the turn never opened a tool round")


def _wait_follow_ups() -> None:
    import threading

    for thread in threading.enumerate():
        if thread.name.startswith("skep-verdict-"):
            thread.join(10)
            assert not thread.is_alive()


def _next_turn_replays(client: Any, provider: Any, chat_id: str) -> list[dict[str, Any]]:
    from .strict_wire import REJECTIONS

    provider.script_reply("seen")
    events = sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "?"}).text)
    assert events[-1] == ("done", {"state": "complete"}), events
    assert REJECTIONS == []
    bodies: list[dict[str, Any]] = provider.chat_bodies()
    return bodies


def test_d18_a_verdict_landing_mid_turn_is_recorded_at_once_and_replays(
    config: SupervisorConfig, provider: Any
) -> None:
    from skep.supervisor.serve.chat import chat_turn_lock

    client, chat_id, store, engine = _d18_rig(config, provider)
    try:
        action_id = _card(client, provider, chat_id, ("set_policy", {"auto_approve": True}))
        lock = chat_turn_lock(chat_id)
        lock.acquire()
        provider.script_tool_call("list_runs", {"limit": 1})
        provider.script_reply("the run finished")
        turn = _other_turn(engine, store, chat_id)
        _until_open_round(turn)  # the click lands inside the other turn's open round
        answer = client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm")
        assert answer.status_code == 200, answer.text
        events = sse_events(answer.text)
        assert events[-1] == ("done", {"state": "queued"}), events
        card = store.get_chat_action(action_id)
        assert card is not None and card.status == "confirmed"  # recorded now, card closed
        provider.script_reply("noted the policy change")
        assert list(turn)[-1] == ("done", {"state": "complete"})
        lock.release()
        _wait_follow_ups()
        assert any("noted the policy change" in m.content for m in store.chat_messages(chat_id))
        bodies = _next_turn_replays(client, provider, chat_id)
        assert "auto_approve" in json.dumps(bodies[-1])  # the call and its result ride
        if provider.protocol == "ollama":  # the other fakes reuse one call id
            sent = bodies[-1]["messages"]
            at = next(
                i for i, m in enumerate(sent) if "set_policy" in json.dumps(m.get("tool_calls"))
            )
            assert sent[at + 1]["tool_name"] == "set_policy"
            assert sent[at + 1]["tool_call_id"] == sent[at]["tool_calls"][0]["id"]
    finally:
        store.close()


def test_d18_two_verdicts_mid_turn_queue_one_follow_up(
    config: SupervisorConfig, provider: Any
) -> None:
    from skep.supervisor.serve.chat import chat_turn_lock

    client, chat_id, store, engine = _d18_rig(config, provider)
    try:
        first, second = _two_call_round(store, chat_id, fold=False)
        lock = chat_turn_lock(chat_id)
        lock.acquire()
        provider.script_reply("the run finished")
        turn = _other_turn(engine, store, chat_id)
        one = sse_events(client.post(f"/api/chats/{chat_id}/actions/{second}/confirm").text)
        assert one[-1] == ("done", {"state": "awaiting_confirmation"})  # another card waits
        two = sse_events(client.post(f"/api/chats/{chat_id}/actions/{first}/deny").text)
        assert two[-1] == ("done", {"state": "queued"})
        provider.script_reply("both answered")
        list(turn)
        lock.release()
        _wait_follow_ups()
        _next_turn_replays(client, provider, chat_id)
    finally:
        store.close()


def test_d18_a_verdict_on_a_folded_card_mid_turn_replays(
    config: SupervisorConfig, provider: Any
) -> None:
    from skep.supervisor.serve.chat import chat_turn_lock

    client, chat_id, store, engine = _d18_rig(config, provider)
    try:
        (first,) = _two_call_round(store, chat_id, fold=True)
        lock = chat_turn_lock(chat_id)
        lock.acquire()
        provider.script_reply("the run finished")
        turn = _other_turn(engine, store, chat_id)
        events = sse_events(client.post(f"/api/chats/{chat_id}/actions/{first}/confirm").text)
        assert events[-1] == ("done", {"state": "queued"}), events
        assert len([e for e, _ in events if e == "tool"]) == 2  # every item, once
        provider.script_reply("set")
        list(turn)
        lock.release()
        _wait_follow_ups()
        _next_turn_replays(client, provider, chat_id)
    finally:
        store.close()


def test_d18_a_verdict_racing_the_end_of_the_turn_replays(
    config: SupervisorConfig, provider: Any
) -> None:
    from skep.supervisor.serve.chat import chat_turn_lock

    from .race import race

    client, chat_id, store, engine = _d18_rig(config, provider)
    try:
        action_id = _card(client, provider, chat_id, ("set_policy", {"auto_approve": True}))
        lock = chat_turn_lock(chat_id)
        lock.acquire()
        provider.script_reply("the run finished")
        provider.script_reply("done")
        turn = _other_turn(engine, store, chat_id)

        def end_turn() -> None:
            list(turn)
            lock.release()

        def click() -> list[Any]:
            return sse_events(client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm").text)

        _ended, clicked = race(end_turn, click)
        assert clicked[-1] in (("done", {"state": "queued"}), ("done", {"state": "complete"}))
        _wait_follow_ups()
        _next_turn_replays(client, provider, chat_id)
    finally:
        store.close()


def test_d18_a_serve_restart_with_a_queued_follow_up_replays(
    config: SupervisorConfig, provider: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The follow-up waits on a thread; a restart loses it. The verdict and
    its result were stored at the click, so the next turn replays them."""
    from skep.supervisor.serve import chat as chat_module

    client, chat_id, store, _engine = _d18_rig(config, provider)
    try:
        action_id = _card(client, provider, chat_id, ("set_policy", {"auto_approve": True}))
        lock = chat_module.chat_turn_lock(chat_id)
        lock.acquire()
        lost: list[Any] = []
        monkeypatch.setattr(chat_module, "_queue_follow_up", lambda *a, **k: lost.append(a))
        events = sse_events(client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm").text)
        assert events[-1] == ("done", {"state": "queued"}) and lost
        lock.release()
        restarted = serve_client(config)
        bodies = _next_turn_replays(restarted, provider, chat_id)
        assert "auto_approve" in json.dumps(bodies[-1])
    finally:
        store.close()


def test_d18_a_card_from_the_round_still_open_cannot_be_answered_yet(
    config: SupervisorConfig, provider: Any
) -> None:
    """A fold is built while its round is open: a click on a card that round
    raised still waits for the turn (409), as before."""
    from fastapi import HTTPException

    from skep.supervisor.serve import chat as chat_module

    _client, chat_id, store, engine = _d18_rig(config, provider)
    try:
        lock = chat_module.chat_turn_lock(chat_id)
        lock.acquire()
        provider.script_tool_call("set_policy", {"auto_approve": True})
        base_url, api_key, model, protocol = engine.resolved_llm(store.get_chat(chat_id))
        store.add_chat_message(chat_id, role="user", content="on")
        turn = engine.turn_events(
            chat_id, base_url=base_url, api_key=api_key, model=model, protocol=protocol
        )
        for event, _data in turn:
            if event == "action":
                break
        (pending,) = store.pending_chat_actions(chat_id)
        with pytest.raises(HTTPException) as busy:
            engine.verdict_events(chat_id, store.get_chat(chat_id), pending.action_id, confirm=True)
        assert busy.value.status_code == 409
        list(turn)
        lock.release()
    finally:
        store.close()
