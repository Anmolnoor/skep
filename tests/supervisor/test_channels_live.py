"""v26: live channels — the operator surface, session binding, and transports.

The v16 adapters stay untouched (their tests pin the contract); everything
here is built AROUND them: config/secret routes (F1), session binding + the
headless chat engine (F2), the Telegram long-poll runtime (F3), the signed
Slack webhook (F4), and the honesty flags for what is actually live (F5).
"""

from __future__ import annotations

from typing import Any

from skep.supervisor import SupervisorConfig
from skep.supervisor.serve.channels import (
    channel_secret_env,
    channel_secret_path,
    resolve_channel_secret,
)

from .conftest import serve_client


def test_channel_config_roundtrip_over_http(config: SupervisorConfig) -> None:
    client = serve_client(config)
    view = client.get("/api/channels").json()["channels"]
    assert set(view) == {"discord", "telegram", "slack"}
    for channel in view.values():
        assert channel["enabled"] is False  # fail-closed defaults
        assert channel["secret_configured"] is False

    updated = client.put(
        "/api/channels/telegram",
        json={"enabled": True, "allowed_identities": ["42", "43"]},
    ).json()
    assert updated["enabled"] is True
    assert updated["allowed_identities"] == ["42", "43"]
    # Partial update: leaving fields out preserves them.
    updated = client.put("/api/channels/telegram", json={"channel_can_confirm": True}).json()
    assert updated["enabled"] is True
    assert updated["allowed_identities"] == ["42", "43"]
    assert updated["channel_can_confirm"] is True

    assert client.put("/api/channels/matrix", json={"enabled": True}).status_code == 404


def test_channel_secrets_are_write_only_and_0600(config: SupervisorConfig) -> None:
    client = serve_client(config)
    updated = client.put("/api/channels/telegram", json={"secret": "bot-token-1"}).json()
    assert updated["secret_configured"] is True
    # The secret value never appears in any GET.
    assert "bot-token-1" not in client.get("/api/channels").text
    path = channel_secret_path(config.home, "telegram")
    assert path.is_file()
    assert (path.stat().st_mode & 0o777) == 0o600
    assert resolve_channel_secret(config.home, "telegram") == "bot-token-1"
    # Empty string clears.
    cleared = client.put("/api/channels/telegram", json={"secret": ""}).json()
    assert cleared["secret_configured"] is False
    assert not path.exists()


def test_slack_signing_secret_is_a_named_part(config: SupervisorConfig) -> None:
    client = serve_client(config)
    updated = client.put(
        "/api/channels/slack",
        json={"secret": "xoxb-token", "signing_secret": "signing-1"},
    ).json()
    assert updated["secret_configured"] is True
    assert updated["signing_secret_configured"] is True
    assert resolve_channel_secret(config.home, "slack") == "xoxb-token"
    assert resolve_channel_secret(config.home, "slack", "signing") == "signing-1"
    assert channel_secret_env("slack", "signing") == "SKEP_SLACK_SIGNING_SECRET"
    # Only slack carries a signing secret.
    rejected = client.put("/api/channels/telegram", json={"signing_secret": "x"})
    assert rejected.status_code == 400


def test_channel_view_reports_live_transports_honestly(config: SupervisorConfig) -> None:
    """The UI must never imply a transport this build does not have."""
    client = serve_client(config)
    view = client.get("/api/channels").json()["channels"]
    from skep.supervisor.serve.channels import LIVE_CHANNELS

    for name, channel in view.items():
        assert channel["live"] is (name in LIVE_CHANNELS)
    # v37-F4: all three channels have a wired transport in this build.
    assert view["discord"]["live"] is True


def test_channel_session_binding_is_durable_and_stable(config: SupervisorConfig) -> None:
    """v26-F2: one messenger conversation = one durable chat session."""
    from skep.supervisor import RunStore

    store = RunStore(config.db_path)
    try:
        assert store.channel_session("telegram:42") is None
        chat = store.create_chat(title="telegram 42", model=None)
        binding = store.bind_channel_session(
            session_key="telegram:42", channel="telegram", identity_id="42", chat_id=chat.chat_id
        )
        assert binding.chat_id == chat.chat_id
        found = store.channel_session("telegram:42")
        assert found is not None
        assert found.channel == "telegram"
        assert found.identity_id == "42"
        assert found.chat_id == chat.chat_id
    finally:
        store.close()


def test_chat_engine_is_the_single_turn_implementation() -> None:
    """v26-F2: the HTTP routes and the channel transports must share one turn
    loop — the engine yields (event, data); only the HTTP layer knows SSE."""
    import inspect

    from skep.supervisor.serve import chat as chat_module

    assert hasattr(chat_module, "ChatEngine")
    engine_source = inspect.getsource(chat_module.ChatEngine)
    routes_source = inspect.getsource(chat_module.add_chat_routes)
    # The model loop lives in the engine, not in the routes — and every
    # provider call rides the v58-F4 retry seam.
    assert "chat_stream_with_retry(" in engine_source
    assert "chat_stream(" not in engine_source  # no provider call skips the retry
    assert "chat_stream(" not in routes_source
    # SSE formatting lives in the routes, not in the engine.
    assert "_sse(" not in engine_source
    assert "_as_sse" in routes_source


# -- F3: the Telegram long-poll runtime (fake fetch/send, fake LLM) -----------


def _telegram_update(update_id: int, chat_id: str, text: str) -> dict[str, object]:
    message = {"chat": {"id": chat_id}, "from": {"id": chat_id}, "text": text}
    return {"update_id": update_id, "message": message}


class _TelegramHarness:
    """A poller over fakes: scripted updates in, delivered texts out."""

    def __init__(
        self,
        config: SupervisorConfig,
        web_ui_url: str = "http://ui.test/",
        spawn: Any = None,
    ) -> None:
        from skep.supervisor import RunStore
        from skep.supervisor.serve.channels.runtime import TelegramPoller
        from skep.supervisor.serve.chat import ChatEngine
        from skep.supervisor.serve.jobs import Dispatcher
        from skep.supervisor.serve.settings import ConfigHolder

        self.store = RunStore(config.db_path)
        self.holder = ConfigHolder(config, self.store)
        self.runner = Dispatcher(self.holder, self.store)
        self.engine = ChatEngine(
            store=self.store, holder=self.holder, runner=self.runner, home=config.home
        )
        self.updates: list[list[dict[str, object]]] = []
        self.fetch_calls: list[tuple[str, int]] = []
        self.sent: list[tuple[str, str, str]] = []
        self.markups: list[dict[str, object] | None] = []
        self.parse_modes: list[str | None] = []
        self.answers: list[tuple[str, str, str]] = []
        self.poller = TelegramPoller(
            self.engine,
            web_ui_url=web_ui_url,
            fetch=self._fetch,
            send=self._send,
            answer=self._answer,
            # Inline unless a test asks for real threads: deterministic replies.
            spawn=spawn if spawn is not None else (lambda work: work()),
        )

    def _fetch(self, token: str, offset: int) -> list[dict[str, object]]:
        self.fetch_calls.append((token, offset))
        return self.updates.pop(0) if self.updates else []

    def _send(
        self,
        token: str,
        chat_id: str,
        text: str,
        reply_markup: dict[str, object] | None = None,
        parse_mode: str | None = None,
    ) -> bool:
        self.sent.append((token, chat_id, text))
        self.markups.append(reply_markup)
        self.parse_modes.append(parse_mode)
        return True

    def _answer(self, token: str, callback_id: str, text: str) -> bool:
        self.answers.append((token, callback_id, text))
        return True

    def close(self) -> None:
        self.runner.shutdown()
        self.store.close()


def _enable_telegram(config: SupervisorConfig, ollama: object, can_confirm: bool = False) -> None:
    client = serve_client(config)
    client.put(
        "/api/llm/config",
        json={
            "base_url": ollama.base_url,  # type: ignore[attr-defined]
            "default_model": "qwen3",
            "api_key": "sk-fake",
        },
    )
    client.put(
        "/api/channels/telegram",
        json={
            "enabled": True,
            "allowed_identities": ["42"],
            "allowed_users": ["42"],
            "secret": "bot-token",
            "channel_can_confirm": can_confirm,
        },
    )


def _telegram_callback(update_id: int, chat_id: str, from_id: str, data: str) -> dict[str, object]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb-{update_id}",
            "from": {"id": from_id},
            "message": {"chat": {"id": chat_id}},
            "data": data,
        },
    }


def test_telegram_poller_routes_a_message_through_a_real_turn(
    config: SupervisorConfig,
) -> None:
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama)
        harness = _TelegramHarness(config)
        try:
            ollama.script_reply("hello from the hive")
            harness.updates.append([_telegram_update(7, "42", "status?")])
            assert harness.poller.poll_once() == 1
            assert harness.sent == [("bot-token", "42", "hello from the hive")]
            # The conversation is a durable, bound chat session.
            binding = harness.store.channel_session("telegram:42")
            assert binding is not None
            chat = harness.store.get_chat(binding.chat_id)
            assert chat is not None and chat.source == "telegram"
            roles = [m.role for m in harness.store.chat_messages(binding.chat_id)]
            assert roles == ["user", "assistant"]
            # The offset advanced past the processed update.
            assert harness.poller.poll_once() == 0
            assert harness.fetch_calls == [("bot-token", 0), ("bot-token", 8)]
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_telegram_unknown_identity_fails_closed_but_offset_advances(
    config: SupervisorConfig,
) -> None:
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama)
        harness = _TelegramHarness(config)
        try:
            harness.updates.append([_telegram_update(3, "99", "let me in")])
            assert harness.poller.poll_once() == 0
            assert harness.sent == []  # no reply — silence to strangers
            assert ollama.chat_bodies() == []  # and no model turn at all
            assert harness.poller.poll_once() == 0
            assert harness.fetch_calls[-1] == ("bot-token", 4)  # never re-fetched
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_telegram_poller_is_inert_until_enabled_with_a_secret(
    config: SupervisorConfig,
) -> None:
    harness = _TelegramHarness(config)
    try:
        assert harness.poller.poll_once() == 0
        assert harness.fetch_calls == []  # not ready -> no network call at all
    finally:
        harness.close()


def test_telegram_mutation_points_at_the_web_ui(config: SupervisorConfig) -> None:
    """A proposed mutation over Telegram becomes the ordinary pending card;
    the messenger reply says where to confirm it — never confirms inline."""
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama)
        harness = _TelegramHarness(config)
        try:
            ollama.script_tool_call("set_policy", {"default_execution_mode": "workspace"})
            harness.updates.append([_telegram_update(1, "42", "loosen the policy")])
            assert harness.poller.poll_once() == 1
            assert len(harness.sent) == 1
            # v78-F5 re-pin: the wire text is MarkdownV2 (specials escaped,
            # parse_mode set); the DISPLAY text is unchanged.
            reply = harness.sent[0][2]
            assert "set\\_policy" in reply
            assert "needs confirmation" in reply
            assert "http://ui\\.test/" in reply
            assert harness.parse_modes[0] == "MarkdownV2"
            binding = harness.store.channel_session("telegram:42")
            assert binding is not None
            (action,) = harness.store.pending_chat_actions(binding.chat_id)
            assert action.tool == "set_policy"
            # While the card waits, the channel refuses to talk past it.
            harness.updates.append([_telegram_update(2, "42", "do it anyway")])
            assert harness.poller.poll_once() == 1
            # v129-F4: the pending card is re-sent after the blocking text
            assert "review it in the web UI" in harness.sent[-2][2]
            assert "set\\_policy" in harness.sent[-1][2]
            assert len(ollama.chat_bodies()) == 1  # no second model turn
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_telegram_delivery_failure_never_kills_the_poll(config: SupervisorConfig) -> None:
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama)
        harness = _TelegramHarness(config)

        def broken_send(
            token: str,
            chat_id: str,
            text: str,
            reply_markup: dict[str, object] | None = None,
            parse_mode: str | None = None,
        ) -> bool:
            raise ConnectionError("telegram down")

        harness.poller._send = broken_send
        try:
            ollama.script_reply("ok")
            harness.updates.append([_telegram_update(1, "42", "hi")])
            assert harness.poller.poll_once() == 1  # survived; adapter caught it
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_a_channel_chat_opens_as_assistant_and_cannot_dispatch(
    repo: object, config: SupervisorConfig
) -> None:
    """ADR 0059 decision #4 (B55, F3 lesson 5): a messenger's first contact
    opens an assistant chat — never the widest lanes — so a dispatch is
    refused at proposal, no card, saying what to do instead."""
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama, can_confirm=True)
        harness = _TelegramHarness(config)
        try:
            ollama.script_tool_call(
                "dispatch_run", {"repo": str(repo), "instructions": "Fix the bug. MODE:happy"}
            )
            ollama.script_reply("I cannot dispatch from here")
            harness.updates.append([_telegram_update(1, "42", "fix the bug")])
            assert harness.poller.poll_once() == 1
            binding = harness.store.channel_session("telegram:42")
            assert binding is not None
            chat = harness.store.get_chat(binding.chat_id)
            assert chat is not None and chat.role == "assistant"
            assert harness.store.pending_chat_actions(binding.chat_id) == []
            (refusal,) = [
                m for m in harness.store.chat_messages(binding.chat_id) if m.role == "tool"
            ]
            assert "exceeds the delegator" in refusal.content
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_telegram_button_confirms_a_low_risk_dispatch(
    repo: object, config: SupervisorConfig
) -> None:
    """v41-F2 end to end: confirm-enabled channel → the card carries an inline
    keyboard stamped with the action id; the callback resolves it through the
    shared gate; a second press answers 'gone'."""
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama, can_confirm=True)
        harness = _TelegramHarness(config)
        try:
            # A channel chat opens as assistant, which may not dispatch (ADR
            # 0059 decision #4); the operator gave this one the builder role.
            builder = harness.store.create_chat(
                title="telegram 42", model=None, source="telegram", role="builder"
            )
            harness.store.bind_channel_session(
                session_key="telegram:42", channel="telegram", identity_id="42",
                chat_id=builder.chat_id,
            )  # fmt: skip
            ollama.script_tool_call(
                "dispatch_run",
                {
                    "repo": str(repo),
                    "instructions": "Fix the bug. MODE:happy",
                    "execution_mode": "sandbox",
                },
            )
            harness.updates.append([_telegram_update(1, "42", "fix the bug")])
            assert harness.poller.poll_once() == 1
            binding = harness.store.channel_session("telegram:42")
            assert binding is not None
            (action,) = harness.store.pending_chat_actions(binding.chat_id)
            markup = harness.markups[-1]
            assert markup is not None, "a confirmable card must carry the keyboard"
            buttons = markup["inline_keyboard"][0]  # type: ignore[index]
            assert [b["callback_data"] for b in buttons] == [
                f"confirm:{action.action_id}",
                f"deny:{action.action_id}",
            ]

            ollama.script_reply("dispatched — I will report back")
            harness.updates.append(
                [_telegram_callback(2, "42", "42", f"confirm:{action.action_id}")]
            )
            assert harness.poller.poll_once() == 1
            assert harness.answers[-1][2] == "skep: confirmed."
            refreshed = harness.store.get_chat_action(action.action_id)
            assert refreshed is not None and refreshed.status == "confirmed"
            assert harness.store.recent_runs(5), "the confirmed dispatch_run must dispatch"
            # The model's continuation lands in the conversation as a message.
            assert harness.sent[-1][2] == "dispatched — I will report back"

            # A verdict is final: the same press again answers 'gone'.
            harness.updates.append(
                [_telegram_callback(3, "42", "42", f"confirm:{action.action_id}")]
            )
            assert harness.poller.poll_once() == 1
            assert harness.answers[-1][2] == "skep: that card is gone or resolved."
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_telegram_button_press_is_gated_by_the_v16_allowlist(
    config: SupervisorConfig,
) -> None:
    """A policy change can NEVER be confirmed from Telegram, whatever the
    config: the card grows no keyboard, and even a forged callback is refused
    with the fail-closed reason — the card stays."""
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama, can_confirm=True)
        harness = _TelegramHarness(config)
        try:
            ollama.script_tool_call("set_policy", {"default_execution_mode": "workspace"})
            harness.updates.append([_telegram_update(1, "42", "loosen the policy")])
            assert harness.poller.poll_once() == 1
            assert harness.markups[-1] is None  # no keyboard on a web-UI-only class
            assert "web UI" in harness.sent[-1][2]
            binding = harness.store.channel_session("telegram:42")
            assert binding is not None
            (action,) = harness.store.pending_chat_actions(binding.chat_id)

            harness.updates.append(
                [_telegram_callback(2, "42", "42", f"confirm:{action.action_id}")]
            )
            assert harness.poller.poll_once() == 1
            refusal = harness.answers[-1][2]
            assert "channel.confirm.denied.web_ui_only_action_class" in refusal
            assert "web UI" in refusal
            refreshed = harness.store.get_chat_action(action.action_id)
            assert refreshed is not None and refreshed.status == "proposed"  # card still waits

            # A group bystander (chat listed, presser not) fails closed too.
            harness.updates.append(
                [_telegram_callback(3, "42", "666", f"confirm:{action.action_id}")]
            )
            assert harness.poller.poll_once() == 1
            assert "channel.confirm.denied" in harness.answers[-1][2]
        finally:
            harness.close()
    finally:
        ollama.stop()


# -- F4: Slack — signed webhooks, gated button confirm -------------------------


def _slack_headers(secret: str, body: bytes, ts: int | None = None) -> dict[str, str]:
    import hashlib
    import hmac
    import time as _time

    stamp = int(_time.time()) if ts is None else ts
    digest = hmac.new(secret.encode(), f"v0:{stamp}:".encode() + body, hashlib.sha256)
    return {
        "x-slack-request-timestamp": str(stamp),
        "x-slack-signature": "v0=" + digest.hexdigest(),
        "content-type": "application/json",
    }


def _slack_app_client(config: SupervisorConfig) -> object:
    """An UNAUTHENTICATED client — the webhook must work by signature alone."""
    from fastapi.testclient import TestClient

    from skep.supervisor.serve import create_app

    return TestClient(create_app(config, sse_poll_seconds=0.05, start_ticker=False))


def _enable_slack(config: SupervisorConfig, ollama: object, *, can_confirm: bool = False) -> None:
    client = serve_client(config)
    client.put(
        "/api/llm/config",
        json={
            "base_url": ollama.base_url,  # type: ignore[attr-defined]
            "default_model": "qwen3",
            "api_key": "sk-fake",
        },
    )
    client.put(
        "/api/channels/slack",
        json={
            "enabled": True,
            "channel_can_confirm": can_confirm,
            "allowed_identities": ["C1"],
            "allowed_users": ["U1"],
            "secret": "xoxb-fake",
            "signing_secret": "signing-1",
        },
    )


def test_slack_events_authenticate_by_signature_alone(config: SupervisorConfig) -> None:
    import json as _json

    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        webhook = _slack_app_client(config)
        # Unconfigured: nothing to verify against.
        assert webhook.post("/api/channels/slack/events", content=b"{}").status_code == 503  # type: ignore[attr-defined]

        _enable_slack(config, ollama)
        body = _json.dumps({"type": "url_verification", "challenge": "chal-1"}).encode()
        # Wrong secret -> 401. Stale timestamp -> 401. Valid -> the challenge.
        bad = webhook.post(  # type: ignore[attr-defined]
            "/api/channels/slack/events", content=body, headers=_slack_headers("wrong", body)
        )
        assert bad.status_code == 401
        stale = webhook.post(  # type: ignore[attr-defined]
            "/api/channels/slack/events",
            content=body,
            headers=_slack_headers("signing-1", body, ts=1),
        )
        assert stale.status_code == 401
        good = webhook.post(  # type: ignore[attr-defined]
            "/api/channels/slack/events", content=body, headers=_slack_headers("signing-1", body)
        )
        assert good.status_code == 200
        assert good.json()["challenge"] == "chal-1"
        # Everything else under /api/ still demands the serve token.
        assert webhook.get("/api/status").status_code == 401  # type: ignore[attr-defined]
    finally:
        ollama.stop()


def test_slack_message_event_runs_a_turn_and_replies(
    config: SupervisorConfig, monkeypatch: object
) -> None:
    import json as _json

    from skep.supervisor.serve.channels import runtime

    from .fake_ollama import FakeOllama

    sent: list[tuple[str, str, list[dict[str, object]]]] = []

    def fake_send(token: str, channel_id: str, blocks: list[dict[str, object]]) -> bool:
        sent.append((token, channel_id, blocks))
        return True

    monkeypatch.setattr(runtime, "_default_slack_send", fake_send)  # type: ignore[attr-defined]
    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_slack(config, ollama)
        webhook = _slack_app_client(config)
        ollama.script_reply("hive at your service")
        body = _json.dumps(
            {"type": "event_callback", "event": {"channel": "C1", "user": "U1", "text": "status?"}}
        ).encode()
        response = webhook.post(  # type: ignore[attr-defined]
            "/api/channels/slack/events", content=body, headers=_slack_headers("signing-1", body)
        )
        assert response.status_code == 200 and response.json() == {"ok": True}
        assert sent and sent[0][0] == "xoxb-fake" and sent[0][1] == "C1"
        assert "hive at your service" in str(sent[0][2])

        # Unknown channel: acked (no retries) but silently dropped, no turn.
        sent.clear()
        stranger = _json.dumps(
            {"type": "event_callback", "event": {"channel": "C9", "text": "hi"}}
        ).encode()
        acked = webhook.post(  # type: ignore[attr-defined]
            "/api/channels/slack/events",
            content=stranger,
            headers=_slack_headers("signing-1", stranger),
        )
        assert acked.status_code == 200
        assert acked.json()["ignored"] == "channel.reject.identity_not_allowlisted"
        assert sent == []

        # ES (B114, lane F3 f3-24): a stranger IN the allow-listed channel ran a
        # Queen turn. The person must be listed, not just the room.
        intruder = _json.dumps(
            {"type": "event_callback", "event": {"channel": "C1", "user": "UX", "text": "hi"}}
        ).encode()
        refused = webhook.post(  # type: ignore[attr-defined]
            "/api/channels/slack/events",
            content=intruder,
            headers=_slack_headers("signing-1", intruder),
        )
        assert refused.json()["ignored"] == "channel.reject.user_not_allowlisted"
        assert sent == []

        # Bot echoes never become turns.
        echo = _json.dumps(
            {"type": "event_callback", "event": {"channel": "C1", "text": "x", "bot_id": "B1"}}
        ).encode()
        echoed = webhook.post(  # type: ignore[attr-defined]
            "/api/channels/slack/events",
            content=echo,
            headers=_slack_headers("signing-1", echo),
        )
        assert echoed.json()["ignored"] == "bot or non-message event"
    finally:
        ollama.stop()


def test_slack_button_confirm_is_gated_by_the_v16_allowlist(
    config: SupervisorConfig, monkeypatch: object
) -> None:
    """A shell approval can NEVER be confirmed from Slack, whatever the config;
    the fail-closed reason string reaches the channel and the card stays."""
    import json as _json
    from urllib.parse import quote_plus

    from skep.supervisor import RunStore
    from skep.supervisor.serve.channels import runtime

    from .fake_ollama import FakeOllama

    sent: list[tuple[str, str, list[dict[str, object]]]] = []

    def fake_send(token: str, channel_id: str, blocks: list[dict[str, object]]) -> bool:
        sent.append((token, channel_id, blocks))
        return True

    monkeypatch.setattr(runtime, "_default_slack_send", fake_send)  # type: ignore[attr-defined]
    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_slack(config, ollama, can_confirm=True)
        store = RunStore(config.db_path)
        try:
            chat = store.create_chat(title="slack C1", model=None)
            action_id = store.add_chat_action(
                chat.chat_id, tool="allow_command_review", args={"review_id": "r-1"}
            )
        finally:
            store.close()
        webhook = _slack_app_client(config)
        payload = {
            "channel": {"id": "C1"},
            "user": {"id": "U1"},
            "actions": [{"action_id": "confirm", "value": action_id}],
        }
        body = ("payload=" + quote_plus(_json.dumps(payload))).encode()
        response = webhook.post(  # type: ignore[attr-defined]
            "/api/channels/slack/interact",
            content=body,
            headers=_slack_headers("signing-1", body),
        )
        assert response.status_code == 200
        assert sent, "the refusal must be delivered back to the channel"
        refusal = str(sent[-1][2])
        assert "channel.confirm.denied.web_ui_only_action_class" in refusal
        assert "web UI" in refusal
        check = RunStore(config.db_path)
        try:
            refreshed = check.get_chat_action(action_id)
        finally:
            check.close()
        assert refreshed is not None and refreshed.status == "proposed"  # card still waits
    finally:
        ollama.stop()


def test_slack_button_confirms_a_low_risk_dispatch(
    repo: object, config: SupervisorConfig, monkeypatch: object
) -> None:
    import json as _json
    from urllib.parse import quote_plus

    from skep.supervisor import RunStore
    from skep.supervisor.serve.channels import runtime

    from .fake_ollama import FakeOllama

    sent: list[tuple[str, str, list[dict[str, object]]]] = []

    def fake_send(token: str, channel_id: str, blocks: list[dict[str, object]]) -> bool:
        sent.append((token, channel_id, blocks))
        return True

    monkeypatch.setattr(runtime, "_default_slack_send", fake_send)  # type: ignore[attr-defined]
    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_slack(config, ollama, can_confirm=True)
        store = RunStore(config.db_path)
        try:
            chat = store.create_chat(
                title="slack C1", model=None, role="builder"
            )  # may dispatch (P3-6)
            store.bind_channel_session(
                session_key="slack:C1", channel="slack", identity_id="C1", chat_id=chat.chat_id
            )
            action_id = store.add_chat_action(
                chat.chat_id,
                tool="dispatch_run",
                args={
                    "repo": str(repo),
                    "instructions": "Fix the bug. MODE:happy",
                    "execution_mode": "sandbox",
                },
            )
        finally:
            store.close()
        webhook = _slack_app_client(config)
        ollama.script_reply("dispatched — I will report back")
        payload = {
            "channel": {"id": "C1"},
            "user": {"id": "U1"},
            "actions": [{"action_id": "confirm", "value": action_id}],
        }
        body = ("payload=" + quote_plus(_json.dumps(payload))).encode()
        assert (
            webhook.post(  # type: ignore[attr-defined]
                "/api/channels/slack/interact",
                content=body,
                headers=_slack_headers("signing-1", body),
            ).status_code
            == 200
        )
        check = RunStore(config.db_path)
        try:
            resolved = check.get_chat_action(action_id)
            dispatched = check.recent_runs(5)
        finally:
            check.close()
        assert resolved is not None and resolved.status == "confirmed"
        assert dispatched, "the confirmed dispatch_run must have dispatched a run"
        assert "dispatched" in str(sent[-1][2])
    finally:
        ollama.stop()


def test_a_slack_button_for_another_chats_card_is_refused(
    config: SupervisorConfig, monkeypatch: object
) -> None:
    """v130-F2: the v129-F3 rule, Slack spelling — a press names its card by
    id, and resolves only a card of the chat its own channel is bound to."""
    import json as _json
    from urllib.parse import quote_plus

    from skep.supervisor import RunStore
    from skep.supervisor.serve.channels import runtime

    from .fake_ollama import FakeOllama

    sent: list[tuple[str, str, list[dict[str, object]]]] = []

    def fake_send(token: str, channel_id: str, blocks: list[dict[str, object]]) -> bool:
        sent.append((token, channel_id, blocks))
        return True

    monkeypatch.setattr(runtime, "_default_slack_send", fake_send)  # type: ignore[attr-defined]
    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_slack(config, ollama, can_confirm=True)
        store = RunStore(config.db_path)
        try:
            bound = store.create_chat(title="slack C1", model=None)
            store.bind_channel_session(
                session_key="slack:C1", channel="slack", identity_id="C1", chat_id=bound.chat_id
            )
            other = store.create_chat(title="desk", model=None)
            action_id = store.add_chat_action(
                other.chat_id, tool="read_url", args={"url": "https://example.com/b"}
            )
        finally:
            store.close()
        webhook = _slack_app_client(config)
        payload = {
            "channel": {"id": "C1"},
            "user": {"id": "U1"},
            "actions": [{"action_id": "deny", "value": action_id}],
        }
        body = ("payload=" + quote_plus(_json.dumps(payload))).encode()
        webhook.post(  # type: ignore[attr-defined]
            "/api/channels/slack/interact", content=body, headers=_slack_headers("signing-1", body)
        )
        assert "belongs to another conversation" in str(sent[-1][2])
        check = RunStore(config.db_path)
        try:
            refreshed = check.get_chat_action(action_id)
        finally:
            check.close()
        assert refreshed is not None and refreshed.status == "proposed"
    finally:
        ollama.stop()


def test_a_card_chained_after_a_slack_verdict_keeps_its_buttons(
    config: SupervisorConfig, monkeypatch: object
) -> None:
    """v130-F4: the Slack spelling of the v129-F3 chained-card rule."""
    import json as _json
    from urllib.parse import quote_plus

    from skep.supervisor import RunStore
    from skep.supervisor.serve.channels import runtime

    from .fake_ollama import FakeOllama

    sent: list[tuple[str, str, list[dict[str, object]]]] = []

    def fake_send(token: str, channel_id: str, blocks: list[dict[str, object]]) -> bool:
        sent.append((token, channel_id, blocks))
        return True

    monkeypatch.setattr(runtime, "_default_slack_send", fake_send)  # type: ignore[attr-defined]
    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_slack(config, ollama, can_confirm=True)
        store = RunStore(config.db_path)
        try:
            chat = store.create_chat(title="slack C1", model=None)
            store.bind_channel_session(
                session_key="slack:C1", channel="slack", identity_id="C1", chat_id=chat.chat_id
            )
            action_id = store.add_chat_action(
                chat.chat_id, tool="read_url", args={"url": "https://example.com/a"}
            )
        finally:
            store.close()
        ollama.script_tool_call("read_url", {"url": "https://example.com/next"})
        webhook = _slack_app_client(config)
        payload = {
            "channel": {"id": "C1"},
            "user": {"id": "U1"},
            "actions": [{"action_id": "deny", "value": action_id}],
        }
        body = ("payload=" + quote_plus(_json.dumps(payload))).encode()
        webhook.post(  # type: ignore[attr-defined]
            "/api/channels/slack/interact", content=body, headers=_slack_headers("signing-1", body)
        )
        check = RunStore(config.db_path)
        try:
            (chained,) = check.pending_chat_actions(chat.chat_id)
        finally:
            check.close()
        buttons = [b for _, _, blocks in sent for b in blocks if b.get("type") == "actions"]
        assert chained.action_id in _json.dumps(buttons)
    finally:
        ollama.stop()


def test_docs_describe_the_channel_posture() -> None:
    """F5 (updated v37-F4): what is live, what is confirmable, and the
    Discord gateway note — docs track the build, never the aspiration."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    readme = (root / "README.md").read_text()
    how = (root / "docs" / "how-it-works.md").read_text()
    assert "## Messenger channels" in readme
    assert "never" in readme  # ...confirmable from a messenger
    assert "Discord" in readme
    assert "## Messenger Channels" in how
    assert "fail closed" in how
    assert "dispatch_run" in how and "scheduled_result_ack" in how
    assert "Discord" in how and "gateway websocket" in how
    assert "MESSAGE_CONTENT" in how  # the privileged-intent operator note


def test_settings_ui_exposes_the_channels_card() -> None:
    from skep.supervisor.serve.app import STATIC_DIR

    source = (STATIC_DIR / "app.js").read_text()
    assert 'api("GET", "/api/channels")' in source
    assert "`/api/channels/${name}`" in source
    assert "allowed identities" in source
    assert "bot token (write-only)" in source
    assert "signing secret (write-only)" in source
    assert "web-UI-only (no live transport in this build)" in source


def test_notification_level_over_http_validates_and_merges(config: SupervisorConfig) -> None:
    """v78-F1: the PUT validates against the accepted values (the 400 teaches),
    merges partially, and the view reports the level."""
    client = serve_client(config)
    view = client.get("/api/channels").json()["channels"]
    assert all(channel["notification_level"] == "all" for channel in view.values())

    updated = client.put("/api/channels/telegram", json={"notification_level": "approvals"}).json()
    assert updated["notification_level"] == "approvals"
    # Partial update: leaving the field out preserves it.
    updated = client.put("/api/channels/telegram", json={"enabled": True}).json()
    assert updated["notification_level"] == "approvals"
    assert updated["enabled"] is True

    response = client.put("/api/channels/telegram", json={"notification_level": "loud"})
    assert response.status_code == 400
    assert "all, approvals, none" in response.json()["detail"]


def test_notification_level_select_is_in_the_channels_ui() -> None:
    from skep.supervisor.serve.app import STATIC_DIR

    app_js = (STATIC_DIR / "app.js").read_text()
    assert "notification_level: notifyLevel.value" in app_js
    for option in ("all — every push", "approvals — only action-needed pushes", "none — no pushes"):
        assert option in app_js
    # The teaching copy names what 'none' does and does not silence.
    assert "the web UI and the chat " in app_js


def test_card_replies_never_consult_the_notification_level(
    config: SupervisorConfig,
) -> None:
    """v78-F1: interactive confirm cards are in-turn transport replies, never
    outbound notifications — a level of 'none' must not mute a card the
    operator's own message just provoked."""
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama)
        serve_client(config).put("/api/channels/telegram", json={"notification_level": "none"})
        harness = _TelegramHarness(config)
        try:
            ollama.script_tool_call("set_policy", {"default_execution_mode": "workspace"})
            harness.updates.append([_telegram_update(1, "42", "loosen the policy")])
            assert harness.poller.poll_once() == 1
            assert len(harness.sent) == 1  # the card reply still arrives
            assert "needs confirmation" in harness.sent[0][2]
        finally:
            harness.close()
    finally:
        ollama.stop()


# -- v78-F6: thread replies -------------------------------------------------


def test_thread_ref_round_trips_and_migrates(config: SupervisorConfig) -> None:
    import sqlite3

    from skep.supervisor import RunStore

    store = RunStore(config.db_path)
    try:
        chat = store.create_chat(title="slack C1", model=None)
        store.bind_channel_session(
            session_key="slack:C1", channel="slack", identity_id="C1", chat_id=chat.chat_id
        )
        binding = store.channel_session("slack:C1")
        assert binding is not None and binding.thread_ref is None  # top-level default
        store.set_channel_session_thread("slack:C1", "1721594000.000100")
        binding = store.channel_binding_for_chat(chat.chat_id)
        assert binding is not None and binding.thread_ref == "1721594000.000100"
    finally:
        store.close()

    # An old-schema store (no thread_ref column) opens clean.
    db = config.home / "old-sessions.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE channel_sessions (session_key TEXT PRIMARY KEY, channel TEXT NOT NULL,"
        " identity_id TEXT NOT NULL, chat_id TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO channel_sessions VALUES ('slack:C9', 'slack', 'C9', 'c-1',"
        " '2026-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    migrated = RunStore(db)
    try:
        binding = migrated.channel_session("slack:C9")
        assert binding is not None and binding.thread_ref is None
    finally:
        migrated.close()


def test_slack_event_captures_the_thread_anchor_after_a_turn(
    config: SupervisorConfig, monkeypatch: object
) -> None:
    """The events route stores thread_ts (or ts) on the session AFTER the turn
    binds it; conversational replies themselves stay top-level."""
    import json as _json

    from skep.supervisor.serve.channels import runtime

    from .fake_ollama import FakeOllama

    sent_threads: list[str | None] = []

    def fake_send(
        token: str,
        channel_id: str,
        blocks: list[dict[str, object]],
        thread_ts: str | None = None,
    ) -> bool:
        sent_threads.append(thread_ts)
        return True

    monkeypatch.setattr(runtime, "_default_slack_send", fake_send)  # type: ignore[attr-defined]
    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_slack(config, ollama)
        webhook = _slack_app_client(config)
        ollama.script_reply("noted")
        body = _json.dumps(
            {
                "type": "event_callback",
                "event": {"channel": "C1", "user": "U1", "text": "hi", "ts": "1721594000.000100"},
            }
        ).encode()
        response = webhook.post(  # type: ignore[attr-defined]
            "/api/channels/slack/events", content=body, headers=_slack_headers("signing-1", body)
        )
        assert response.status_code == 200
        assert sent_threads == [None]  # the reply IS the conversation — top-level
        from skep.supervisor import RunStore

        store = RunStore(config.db_path)
        try:
            binding = store.channel_session("slack:C1")
            assert binding is not None and binding.thread_ref == "1721594000.000100"
        finally:
            store.close()
    finally:
        ollama.stop()


def test_slack_push_threads_under_the_conversation_with_rich_blocks(
    config: SupervisorConfig,
) -> None:
    from skep.supervisor import RunStore
    from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task
    from skep.supervisor.serve.channels import ChannelConfig, store_channel_secret
    from skep.supervisor.serve.channels.outbound import push_to_chat_channel

    store = RunStore(config.db_path)
    try:
        chat = store.create_chat(title="slack C1", model=None)
        store.bind_channel_session(
            session_key="slack:C1", channel="slack", identity_id="C1", chat_id=chat.chat_id
        )
        store.set_channel_session_thread("slack:C1", "1721594000.000100")
        store.upsert_channel_config(ChannelConfig(channel="slack", enabled=True))
        store_channel_secret(config.home, "slack", "xoxb-fake")
        task = mint_task(workspace=config.home / "ws", instructions="x", budget=DEFAULT_BUDGET)
        store.create_run(task, repo=config.home, ref=None, execution_mode="sandbox")
        store.transition(task.task_id, "completed", None)
        sends: list[tuple[list[dict[str, object]], str | None]] = []

        def _sl(
            token: str,
            channel_id: str,
            blocks: list[dict[str, object]],
            thread_ts: str | None = None,
        ) -> bool:
            sends.append((blocks, thread_ts))
            return True

        assert push_to_chat_channel(
            store,
            config.home,
            chat.chat_id,
            "🟢 run done",
            run_ref=task.task_id,
            web_ui_url="http://ui.test/",
            send_slack=_sl,
        )
        ((blocks, thread_ts),) = sends
        assert thread_ts == "1721594000.000100"  # the thread reply
        assert blocks[0]["text"]["text"] == "🟢 run done"  # type: ignore[index]
        assert any(b["type"] == "actions" for b in blocks)  # the URL button rode along
        assert "confirm" not in str(blocks) and "deny" not in str(blocks)

        # A chat with no prior inbound ts still receives the push, top-level.
        other = store.create_chat(title="slack C2", model=None)
        store.bind_channel_session(
            session_key="slack:C2", channel="slack", identity_id="C2", chat_id=other.chat_id
        )
        assert push_to_chat_channel(store, config.home, other.chat_id, "plain line", send_slack=_sl)
        blocks, thread_ts = sends[-1]
        assert thread_ts is None
        assert [b["type"] for b in blocks] == ["section"]  # single-section text
    finally:
        store.close()


# P4-2 (ADR 0059 D6): a HIGH card is never confirmable from a channel — the
# server refuses the press and says to use the web UI; the card waits.
_HIGH_DISPATCH = {"instructions": "Fix the bug. MODE:happy", "network": ["*"]}


def test_telegram_never_confirms_a_high_card(repo: object, config: SupervisorConfig) -> None:
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama, can_confirm=True)
        harness = _TelegramHarness(config)
        try:
            chat = harness.store.create_chat(title="telegram 42", model=None)
            # v129-F3: a press resolves only its own conversation's card
            harness.store.bind_channel_session(
                session_key="telegram:42", channel="telegram", identity_id="42",
                chat_id=chat.chat_id,
            )  # fmt: skip
            action_id = harness.store.add_chat_action(
                chat.chat_id, tool="dispatch_run", args={"repo": str(repo), **_HIGH_DISPATCH}
            )
            harness.updates.append([_telegram_callback(1, "42", "42", f"confirm:{action_id}")])
            assert harness.poller.poll_once() == 1
            answer = harness.answers[-1][2]
            assert "HIGH risk" in answer and "web UI" in answer
            refreshed = harness.store.get_chat_action(action_id)
            assert refreshed is not None and refreshed.status == "proposed"
            assert harness.store.recent_runs(5) == []
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_slack_never_confirms_a_high_card(
    repo: object, config: SupervisorConfig, monkeypatch: object
) -> None:
    import json as _json
    from urllib.parse import quote_plus

    from skep.supervisor import RunStore
    from skep.supervisor.serve.channels import runtime

    from .fake_ollama import FakeOllama

    sent: list[list[dict[str, object]]] = []

    def send(_token: str, _channel: str, blocks: list[dict[str, object]]) -> bool:
        sent.append(blocks)
        return True

    monkeypatch.setattr(runtime, "_default_slack_send", send)  # type: ignore[attr-defined]
    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_slack(config, ollama, can_confirm=True)
        store = RunStore(config.db_path)
        try:
            chat = store.create_chat(title="slack C1", model=None)
            store.bind_channel_session(
                session_key="slack:C1", channel="slack", identity_id="C1", chat_id=chat.chat_id
            )
            action_id = store.add_chat_action(
                chat.chat_id, tool="dispatch_run", args={"repo": str(repo), **_HIGH_DISPATCH}
            )
        finally:
            store.close()
        payload = {
            "channel": {"id": "C1"},
            "user": {"id": "U1"},
            "actions": [{"action_id": "confirm", "value": action_id}],
        }
        body = ("payload=" + quote_plus(_json.dumps(payload))).encode()
        webhook = _slack_app_client(config)
        response = webhook.post(  # type: ignore[attr-defined]
            "/api/channels/slack/interact", content=body, headers=_slack_headers("signing-1", body)
        )
        assert response.status_code == 200
        assert "HIGH risk" in str(sent[-1]) and "web UI" in str(sent[-1])
        check = RunStore(config.db_path)
        try:
            refreshed = check.get_chat_action(action_id)
            assert refreshed is not None and refreshed.status == "proposed"
            assert check.recent_runs(5) == []
        finally:
            check.close()
    finally:
        ollama.stop()


# -- v129: a chat bound to a messenger is one conversation with two windows --


def _desk_sends(monkeypatch: object) -> list[str]:
    """Capture outbound Telegram pushes as display text (the MarkdownV2 try is
    refused, so the plain resend is what lands)."""
    from skep.supervisor.serve.channels import outbound

    sent: list[str] = []

    def fake_send(token, chat_id, text, reply_markup=None, parse_mode=None):  # type: ignore[no-untyped-def]
        if parse_mode is not None:
            return False
        sent.append(text)
        return True

    monkeypatch.setattr(outbound, "_default_send", fake_send)  # type: ignore[attr-defined]
    return sent


def _wait_desk_pushes() -> None:
    import threading

    for thread in threading.enumerate():
        if thread.name.startswith(("skep-desk-", "skep-verdict-")):
            thread.join(10)
            assert not thread.is_alive()


def _bound_chat_with_a_card(harness: _TelegramHarness, ollama: object) -> tuple[str, str]:
    """A Telegram turn raises a web-only card; returns (chat id, action id)."""
    ollama.script_tool_call("set_policy", {"default_execution_mode": "workspace"})  # type: ignore[attr-defined]
    harness.updates.append([_telegram_update(1, "42", "do the thing")])
    assert harness.poller.poll_once() == 1
    binding = harness.store.channel_session("telegram:42")
    assert binding is not None
    (action,) = harness.store.pending_chat_actions(binding.chat_id)
    return binding.chat_id, action.action_id


def test_a_web_message_in_a_telegram_bound_chat_reaches_telegram(
    config: SupervisorConfig, monkeypatch: object
) -> None:
    """v129-F1: a turn typed at the desk in a Telegram-bound chat is heard on
    the phone, with the operator's line quoted."""
    from .fake_ollama import FakeOllama

    sent = _desk_sends(monkeypatch)
    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama)
        harness = _TelegramHarness(config)
        try:
            ollama.script_reply("hi from the phone turn")
            harness.updates.append([_telegram_update(1, "42", "hello")])
            harness.poller.poll_once()
            binding = harness.store.channel_session("telegram:42")
            assert binding is not None
            ollama.script_reply("WEB TURN REPLY")
            r = serve_client(config).post(
                f"/api/chats/{binding.chat_id}/messages", json={"content": "from the desk"}
            )
            assert r.status_code == 200 and "REPLY" in r.text
            _wait_desk_pushes()
            assert len(sent) == 1
            assert "(web) you: from the desk" in sent[0]
            assert "WEB TURN REPLY" in sent[0]
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_a_web_confirm_and_deny_in_a_telegram_bound_chat_reach_telegram(
    config: SupervisorConfig, monkeypatch: object
) -> None:
    """v129-F1, the Alfin incident: a Telegram turn raises a card, the operator
    confirms it at the desk, and the continuation reaches the phone once."""
    from .fake_ollama import FakeOllama

    sent = _desk_sends(monkeypatch)
    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama)
        harness = _TelegramHarness(config)
        try:
            client = serve_client(config)
            chat, action = _bound_chat_with_a_card(harness, ollama)
            ollama.script_reply("HERE IS YOUR DOSSIER")
            r = client.post(f"/api/chats/{chat}/actions/{action}/confirm")
            assert r.status_code == 200 and "DOSSIER" in r.text
            _wait_desk_pushes()
            assert [s for s in sent if "HERE IS YOUR DOSSIER" in s] == sent
            assert len(sent) == 1
            assert "(web) you" not in sent[0]  # a click quotes nothing

            ollama.script_tool_call("set_policy", {"default_execution_mode": "sandbox"})
            harness.updates.append([_telegram_update(2, "42", "again")])
            harness.poller.poll_once()
            (again,) = harness.store.pending_chat_actions(chat)
            ollama.script_reply("LEFT THE POLICY ALONE")
            r = client.post(f"/api/chats/{chat}/actions/{again.action_id}/deny")
            assert r.status_code == 200
            _wait_desk_pushes()
            assert len(sent) == 2 and "LEFT THE POLICY ALONE" in sent[1]
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_a_queued_web_confirm_pushes_once(config: SupervisorConfig, monkeypatch: object) -> None:
    """v129-F1/F2: a desk confirm that finds the chat busy is queued; the
    queued follow-up (F2) is the one push, the confirm stream (F1) none."""
    from skep.supervisor.serve.chat import chat_turn_lock

    from .fake_ollama import FakeOllama

    sent = _desk_sends(monkeypatch)
    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama)
        harness = _TelegramHarness(config)
        try:
            client = serve_client(config)
            chat, action = _bound_chat_with_a_card(harness, ollama)
            ollama.script_reply("QUEUED CONTINUATION")
            lock = chat_turn_lock(chat)
            lock.acquire()  # another turn runs
            try:
                r = client.post(f"/api/chats/{chat}/actions/{action}/confirm")
                assert r.status_code == 200 and '"queued"' in r.text
                _wait_for_no_desk_push()
                assert sent == []
            finally:
                lock.release()
            _wait_desk_pushes()
            assert len(sent) == 1 and "QUEUED CONTINUATION" in sent[0]  # F2's push
        finally:
            harness.close()
    finally:
        ollama.stop()


def _wait_for_no_desk_push() -> None:
    import threading

    for thread in threading.enumerate():
        if thread.name.startswith("skep-desk-"):
            thread.join(10)


def test_a_web_confirm_that_finds_the_chat_busy_pushes_nothing_from_f1(
    config: SupervisorConfig, monkeypatch: object
) -> None:
    """v129-F1: another turn holds the chat and its reply is committed;
    another card still pends, so the click answers awaiting_confirmation —
    nothing is pushed, and the other turn's rows are never picked up."""
    from skep.supervisor.serve.chat import chat_turn_lock

    from .fake_ollama import FakeOllama

    sent = _desk_sends(monkeypatch)
    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama)
        harness = _TelegramHarness(config)
        try:
            client = serve_client(config)
            ollama.script_tool_calls(
                ("set_policy", {"default_execution_mode": "workspace"}),
                ("read_url", {"url": "https://example.com/never-fetched"}),
            )
            harness.updates.append([_telegram_update(1, "42", "two things")])
            harness.poller.poll_once()
            binding = harness.store.channel_session("telegram:42")
            assert binding is not None
            chat = binding.chat_id
            (first,) = [
                a for a in harness.store.pending_chat_actions(chat) if a.tool == "set_policy"
            ]
            assert len(harness.store.pending_chat_actions(chat)) == 2
            lock = chat_turn_lock(chat)
            lock.acquire()  # another turn runs, and has said something
            try:
                harness.store.add_chat_message(chat, role="assistant", content="OTHER TURN")
                r = client.post(f"/api/chats/{chat}/actions/{first.action_id}/confirm")
                assert r.status_code == 200 and '"awaiting_confirmation"' in r.text
            finally:
                lock.release()
            _wait_desk_pushes()
            assert sent == []
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_desk_turn_push_honors_level_taint_and_unbound(
    config: SupervisorConfig, monkeypatch: object
) -> None:
    """v129-F1: notification_level=none silences the desk; a private chat
    hears only that the reply was withheld; an unbound chat sends nothing; a
    provider error turn sends its error line."""
    from .fake_ollama import FakeOllama

    sent = _desk_sends(monkeypatch)
    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama)
        harness = _TelegramHarness(config)
        try:
            client = serve_client(config)
            ollama.script_reply("phone turn")
            harness.updates.append([_telegram_update(1, "42", "hello")])
            harness.poller.poll_once()
            binding = harness.store.channel_session("telegram:42")
            assert binding is not None
            chat = binding.chat_id

            def desk(text: str) -> None:
                r = client.post(f"/api/chats/{chat}/messages", json={"content": text})
                assert r.status_code == 200
                _wait_desk_pushes()

            client.put("/api/channels/telegram", json={"notification_level": "none"})
            ollama.script_reply("SILENCED")
            desk("one")
            assert sent == []
            client.put("/api/channels/telegram", json={"notification_level": "all"})

            ollama.script_stream_error("model melted", after="half a")
            desk("two")
            assert len(sent) == 1 and "skep error: model melted" in sent[0]

            harness.store.add_chat_taint(chat, private=1)
            ollama.script_reply("THE PRIVATE FILE SAYS X")
            desk("three")
            assert len(sent) == 2
            assert "THE PRIVATE FILE" not in sent[1]
            assert "stayed in the web UI because the chat has read private files" in sent[1]

            web = client.post("/api/chats", json={}).json()["chat_id"]
            ollama.script_reply("UNBOUND")
            r = client.post(f"/api/chats/{web}/messages", json={"content": "four"})
            assert r.status_code == 200
            _wait_desk_pushes()
            assert len(sent) == 2
        finally:
            harness.close()
    finally:
        ollama.stop()


def _builder_bound_to_telegram(harness: _TelegramHarness) -> str:
    builder = harness.store.create_chat(
        title="telegram 42", model=None, source="telegram", role="builder"
    )
    harness.store.bind_channel_session(
        session_key="telegram:42", channel="telegram", identity_id="42", chat_id=builder.chat_id
    )
    return builder.chat_id


def test_a_card_chained_after_a_telegram_confirm_reaches_telegram(
    repo: object, config: SupervisorConfig
) -> None:
    """v129-F3: the continuation of a button confirm raises another card —
    it reaches the phone with its own buttons, or as the pointer when the
    class is web-only, never dropped."""
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama, can_confirm=True)
        harness = _TelegramHarness(config)
        try:
            chat = _builder_bound_to_telegram(harness)
            ollama.script_tool_call(
                "dispatch_run",
                {"repo": str(repo), "instructions": "Fix. MODE:happy", "execution_mode": "sandbox"},
            )
            harness.updates.append([_telegram_update(1, "42", "fix the bug")])
            harness.poller.poll_once()
            (action,) = harness.store.pending_chat_actions(chat)
            ollama.script_tool_call("read_url", {"url": "https://example.com/next"})
            n = len(harness.sent)
            harness.updates.append(
                [_telegram_callback(2, "42", "42", f"confirm:{action.action_id}")]
            )
            harness.poller.poll_once()
            (chained,) = harness.store.pending_chat_actions(chat)
            assert chained.tool == "read_url"
            stamped = [
                b["callback_data"]
                for m in harness.markups[n:]
                if m
                for b in m["inline_keyboard"][0]  # type: ignore[index]
            ]
            assert stamped == [f"confirm:{chained.action_id}", f"deny:{chained.action_id}"]

            ollama.script_tool_call("set_policy", {"default_execution_mode": "workspace"})
            n = len(harness.sent)
            harness.updates.append([_telegram_callback(3, "42", "42", f"deny:{chained.action_id}")])
            harness.poller.poll_once()
            (web_only,) = harness.store.pending_chat_actions(chat)
            assert web_only.tool == "set_policy"
            assert any("set\\_policy" in s[2] for s in harness.sent[n:])
            assert harness.markups[n:] and all(m is None for m in harness.markups[n:])
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_a_telegram_callback_for_another_chats_card_is_refused(config: SupervisorConfig) -> None:
    """v129-F3: callback data is client-supplied — a button press resolves
    only a card of the chat its own conversation is bound to."""
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama, can_confirm=True)
        harness = _TelegramHarness(config)
        try:
            _builder_bound_to_telegram(harness)
            other = harness.store.create_chat(title="desk", model=None)
            action_id = harness.store.add_chat_action(
                other.chat_id, tool="read_url", args={"url": "https://example.com/b"}
            )
            ollama.script_reply("denied it")
            harness.updates.append([_telegram_callback(1, "42", "42", f"deny:{action_id}")])
            harness.poller.poll_once()
            assert harness.answers[-1][2] == "skep: that card belongs to another conversation."
            refreshed = harness.store.get_chat_action(action_id)
            assert refreshed is not None and refreshed.status == "proposed"
            assert harness.sent == []
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_a_pending_card_is_resent_when_the_phone_speaks(
    repo: object, config: SupervisorConfig, monkeypatch: object
) -> None:
    """v129-F4: a card that reached the chat but not the phone (here: raised
    by a desk turn) is re-sent, under the channel's posture, the moment the
    phone speaks — no model turn runs past it."""
    from .fake_ollama import FakeOllama

    _desk_sends(monkeypatch)
    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama, can_confirm=True)
        harness = _TelegramHarness(config)
        try:
            client = serve_client(config)
            ollama.script_reply("phone turn")
            harness.updates.append([_telegram_update(1, "42", "hello")])
            harness.poller.poll_once()
            binding = harness.store.channel_session("telegram:42")
            assert binding is not None
            chat = binding.chat_id
            ollama.script_tool_call("read_url", {"url": "https://example.com/desk"})
            r = client.post(f"/api/chats/{chat}/messages", json={"content": "read it"})
            assert r.status_code == 200
            _wait_desk_pushes()
            (card,) = harness.store.pending_chat_actions(chat)
            turns = len(ollama.chat_bodies())

            def phone(update_id: int) -> tuple[list[str], list[dict[str, object] | None]]:
                n = len(harness.sent)
                harness.updates.append([_telegram_update(update_id, "42", "and?")])
                harness.poller.poll_once()
                return [s[2] for s in harness.sent[n:]], harness.markups[n:]

            def resolve(action_id: str) -> None:
                harness.store.resolve_chat_action(action_id, status="denied", result={})

            texts, markups = phone(2)
            assert "here it is again" in texts[0]
            assert markups[-1] is not None
            assert [b["callback_data"] for b in markups[-1]["inline_keyboard"][0]] == [  # type: ignore[index]
                f"confirm:{card.action_id}",
                f"deny:{card.action_id}",
            ]
            assert len(ollama.chat_bodies()) == turns  # no model turn
            resolve(card.action_id)

            high = harness.store.add_chat_action(
                chat, tool="dispatch_run", args={"repo": str(repo), **_HIGH_DISPATCH}
            )
            texts, markups = phone(3)
            assert all(m is None for m in markups)
            assert "HIGH risk" in texts[-1]
            resolve(high)

            operator = harness.store.add_chat_action(
                chat, tool="set_policy", args={"default_execution_mode": "workspace"},
                source="operator",
            )  # fmt: skip
            texts, markups = phone(4)
            assert len(texts) == 1 and "here it is again" not in texts[0]
            assert markups == [None]
            resolve(operator)

            harness.store.add_chat_action(
                chat, tool="read_url", args={"url": "https://example.com/secret-name"}
            )
            harness.store.add_chat_taint(chat, private=1)
            texts, markups = phone(5)
            assert len(texts) == 1 and markups == [None]
            assert "private files" in texts[0] and "secret" not in texts[0]
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_a_desk_push_skips_a_revoked_identity(
    config: SupervisorConfig, monkeypatch: object
) -> None:
    """v129-F6: desk text goes only to a conversation that is still allowed —
    a binding outlives its allow-list entry."""
    from .fake_ollama import FakeOllama

    sent = _desk_sends(monkeypatch)
    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama)
        harness = _TelegramHarness(config)
        try:
            client = serve_client(config)
            ollama.script_reply("phone turn")
            harness.updates.append([_telegram_update(1, "42", "hello")])
            harness.poller.poll_once()
            binding = harness.store.channel_session("telegram:42")
            assert binding is not None
            client.put("/api/channels/telegram", json={"allowed_identities": ["43"]})
            ollama.script_reply("NOT FOR A REVOKED ROOM")
            r = client.post(f"/api/chats/{binding.chat_id}/messages", json={"content": "hi"})
            assert r.status_code == 200
            _wait_desk_pushes()
            assert sent == []
            crumb = harness.store.get_setting("channel_last_delivery:telegram")
            assert isinstance(crumb, dict) and crumb["note"] == "not in allowed_identities"
        finally:
            harness.close()
    finally:
        ollama.stop()
