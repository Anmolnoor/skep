"""v130-F6: the messenger shows what skep is doing while a turn runs.

The operator asked (2026-11-19) to see "thinking / working / typing" in
Telegram and Discord the way the web UI's working line shows it. Each
message turn now keeps one status message — "🤔 thinking…", then
"🔧 working: <tool>…" — edited as the turn moves and deleted when the reply
lands; Telegram also shows "typing…" (Discord already did). Cosmetic by
contract: a failing status call never breaks or delays the reply.
"""

from __future__ import annotations

import json as _json
from typing import Any

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.serve.channels import runtime
from skep.supervisor.serve.channels.runtime import LiveStatus, StatusOps, status_line

from .test_channels_live import (
    _enable_slack,
    _enable_telegram,
    _slack_headers,
    _telegram_update,
    _TelegramHarness,
)
from .test_discord_gateway import HELLO, READY, _DiscordHarness, _enable_discord, _message_create


class _Ops:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.fail = fail

    def post(self, token: str, chat: str, text: str) -> str | None:
        self.calls.append(("post", chat, text))
        if self.fail:
            raise ConnectionError("down")
        return "m1"

    def edit(self, token: str, chat: str, message_id: str, text: str) -> bool:
        self.calls.append(("edit", chat, message_id, text))
        return True

    def delete(self, token: str, chat: str, message_id: str) -> bool:
        self.calls.append(("delete", chat, message_id))
        return True

    def ops(self) -> StatusOps:
        return StatusOps(post=self.post, edit=self.edit, delete=self.delete)


def test_status_line_names_the_phase() -> None:
    assert status_line("turn_status", {"state": "thinking"}) == "🤔 thinking…"
    assert status_line("turn_status", {"state": "tool", "tool": "list_runs"}) == (
        "🔧 working: list_runs…"
    )
    assert status_line(None, {"content": "hi"}) is None


def test_live_status_posts_once_edits_on_change_and_deletes() -> None:
    fake = _Ops()
    now = [0.0]
    status = LiveStatus(fake.ops(), "tok", "42", min_interval=1.0, clock=lambda: now[0])
    status.update("🤔 thinking…")
    status.update("🤔 thinking…")  # unchanged: nothing sent
    now[0] = 0.5
    status.update("🔧 working: a…")  # inside the interval: skipped
    now[0] = 2.0
    status.update("🔧 working: b…")
    status.close()
    status.close()  # idempotent
    assert fake.calls == [
        ("post", "42", "🤔 thinking…"),
        ("edit", "42", "m1", "🔧 working: b…"),
        ("delete", "42", "m1"),
    ]


def test_a_failing_status_never_breaks_the_turn() -> None:
    fake = _Ops(fail=True)
    status = LiveStatus(fake.ops(), "tok", "42", min_interval=0)
    status.update("🤔 thinking…")
    status.update("🔧 working: x…")
    status.close()
    assert [c[0] for c in fake.calls] == ["post", "post"]  # retried, never raised
    LiveStatus(None, "tok", "42").update("ignored")  # no ops: a no-op


def test_a_telegram_turn_shows_typing_and_a_live_status(config: SupervisorConfig) -> None:
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama)
        harness = _TelegramHarness(config)
        fake = _Ops()
        typing: list[str] = []
        harness.poller._status_ops = fake.ops()
        harness.poller._status_interval = 0

        def fire(_token: str, chat: str) -> bool:
            typing.append(chat)
            return True

        harness.poller._typing = fire
        try:
            ollama.script_tool_call("list_runs", {})
            ollama.script_reply("nothing ran yet")
            harness.updates.append([_telegram_update(1, "42", "what ran?")])
            harness.poller.poll_once()
            kinds = [c[0] for c in fake.calls]
            assert kinds[0] == "post" and fake.calls[0][2] == "🤔 thinking…"
            assert ("edit", "42", "m1", "🔧 working: list_runs…") in fake.calls
            assert kinds[-1] == "delete"
            assert typing and typing[0] == "42"
            assert any("nothing ran yet" in s[2] for s in harness.sent)
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_a_discord_turn_shows_a_live_status(config: SupervisorConfig) -> None:
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_discord(config, ollama)
        harness = _DiscordHarness(config)
        fake = _Ops()
        harness.gateway._status_ops = fake.ops()
        harness.gateway._status_interval = 0
        try:
            ollama.script_tool_call("list_runs", {})
            ollama.script_reply("nothing ran yet")
            harness.script(HELLO, READY, _message_create("42", "what ran?"))
            harness.gateway.session_once()
            assert fake.calls[0] == ("post", "42", "🤔 thinking…")
            assert ("edit", "42", "m1", "🔧 working: list_runs…") in fake.calls
            assert fake.calls[-1] == ("delete", "42", "m1")
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_a_slack_turn_shows_a_live_status(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from skep.supervisor import RunStore
    from skep.supervisor.serve.chat import ChatEngine
    from skep.supervisor.serve.jobs import Dispatcher
    from skep.supervisor.serve.settings import ConfigHolder

    from .fake_ollama import FakeOllama

    monkeypatch.setattr(runtime, "_default_slack_send", lambda *a, **k: True)
    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_slack(config, ollama)
        store = RunStore(config.db_path)
        holder = ConfigHolder(config, store)
        dispatcher = Dispatcher(holder, store)
        engine = ChatEngine(store=store, holder=holder, runner=dispatcher, home=config.home)
        app = FastAPI()
        fake = _Ops()
        runtime.add_slack_routes(
            app, engine, web_ui_url="http://ui.test/", status_ops=fake.ops(), status_interval=0
        )
        try:
            ollama.script_tool_call("list_runs", {})
            ollama.script_reply("nothing ran yet")
            body = _json.dumps(
                {"type": "event_callback", "event": {"channel": "C1", "user": "U1", "text": "?"}}
            ).encode()
            TestClient(app).post(
                "/api/channels/slack/events",
                content=body,
                headers=_slack_headers("signing-1", body),
            )
            assert fake.calls[0] == ("post", "C1", "🤔 thinking…")
            assert fake.calls[-1] == ("delete", "C1", "m1")
        finally:
            dispatcher.shutdown()
            store.close()
    finally:
        ollama.stop()


_HTTPX = "skep.supervisor.serve.channels.runtime.httpx."


class _Resp:
    def __init__(self, status: int, payload: dict[str, Any]) -> None:
        self.status_code = status
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


def test_the_default_status_calls_speak_each_platforms_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def fake(method: str, payload: dict[str, Any]) -> Any:
        def call(url: str, **kwargs: Any) -> _Resp:
            calls.append((method, url, kwargs.get("json") or {}))
            return _Resp(200, payload)

        return call

    monkeypatch.setattr(_HTTPX + "post", fake("post", {"result": {"message_id": 7}}))
    tg = runtime.TELEGRAM_STATUS_OPS
    assert tg.post("tok", "42", "🤔") == "7"
    tg.edit("tok", "42", "7", "🔧")
    tg.delete("tok", "42", "7")
    assert runtime._default_telegram_typing("tok", "42")
    urls = [u.rsplit("/", 1)[1] for _, u, _ in calls]
    assert urls == ["sendMessage", "editMessageText", "deleteMessage", "sendChatAction"]
    assert calls[0][2]["disable_notification"] is True

    calls.clear()
    monkeypatch.setattr(_HTTPX + "post", fake("post", {"id": "d9", "ok": True, "ts": "1.5"}))
    monkeypatch.setattr(_HTTPX + "patch", fake("patch", {}))
    monkeypatch.setattr(_HTTPX + "delete", fake("delete", {}))
    dc = runtime.DISCORD_STATUS_OPS
    assert dc.post("tok", "42", "🤔") == "d9"
    dc.edit("tok", "42", "d9", "🔧")
    dc.delete("tok", "42", "d9")
    assert [m for m, _, _ in calls] == ["post", "patch", "delete"]

    calls.clear()
    sl = runtime.SLACK_STATUS_OPS
    assert sl.post("tok", "C1", "🤔") == "1.5"
    sl.edit("tok", "C1", "1.5", "🔧")
    sl.delete("tok", "C1", "1.5")
    assert [u.rsplit("/", 1)[1] for _, u, _ in calls] == [
        "chat.postMessage",
        "chat.update",
        "chat.delete",
    ]

    monkeypatch.setattr(_HTTPX + "post", fake("post", {}))
    assert tg.post("tok", "42", "x") is None  # no id in the answer: nothing to edit
