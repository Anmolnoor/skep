"""v37-F4: Discord live inbound over the gateway websocket.

The v16 adapter stays untouched (its tests pin the contract); everything here
exercises ``DiscordGateway`` — the impure edge — against a SCRIPTED gateway:
handshake, identity gating, cards, buttons, reactions, resume. One test runs
the real ``websockets`` client against an in-test server to prove the actual
transport path; no live Discord anywhere.
"""

from __future__ import annotations

import json
from typing import Any

from skep.supervisor import SupervisorConfig

from .conftest import serve_client

HELLO = json.dumps({"op": 10, "d": {"heartbeat_interval": 45_000}})
READY = json.dumps({"op": 0, "t": "READY", "s": 1, "d": {"session_id": "sess-1"}})
RECONNECT = json.dumps({"op": 7})


def _message_create(channel_id: str, content: str, *, seq: int = 2, bot: bool = False) -> str:
    author: dict[str, object] = {"id": "u1"}
    if bot:
        author["bot"] = True
    return json.dumps(
        {
            "op": 0,
            "t": "MESSAGE_CREATE",
            "s": seq,
            "d": {"channel_id": channel_id, "content": content, "author": author},
        }
    )


class _FakeConnection:
    """Scripted frames in, sent frames captured out. The sentinel "TIMEOUT"
    raises like an idle socket so the heartbeat path is exercisable."""

    def __init__(self, frames: list[str]) -> None:
        self._frames = list(frames)
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    def recv(self, timeout: float | None = None) -> str:
        if not self._frames:
            return RECONNECT  # end the session cleanly
        frame = self._frames.pop(0)
        if frame == "TIMEOUT":
            raise TimeoutError
        return frame

    def send(self, frame: str) -> None:
        self.sent.append(json.loads(frame))

    def close(self) -> None:
        self.closed = True


class _DiscordHarness:
    """A gateway over fakes: scripted frames in, REST sends/acks out."""

    def __init__(self, config: SupervisorConfig, web_ui_url: str = "http://ui.test/") -> None:
        from skep.supervisor import RunStore
        from skep.supervisor.serve.channels.runtime import DiscordGateway
        from skep.supervisor.serve.chat import ChatEngine
        from skep.supervisor.serve.jobs import Dispatcher
        from skep.supervisor.serve.settings import ConfigHolder

        self.store = RunStore(config.db_path)
        self.holder = ConfigHolder(config, self.store)
        self.runner = Dispatcher(self.holder, self.store)
        self.engine = ChatEngine(
            store=self.store, holder=self.holder, runner=self.runner, home=config.home
        )
        self.connections: list[_FakeConnection] = []
        self.connect_urls: list[str] = []
        self.sent: list[tuple[str, str, dict[str, object]]] = []
        self.acks: list[tuple[str, str, str]] = []
        self.deferred_acks: list[tuple[str, str]] = []
        self.followups: list[tuple[str, str, str]] = []
        self.spawned_inline = 0
        self.threads: list[tuple[str, str, str, str]] = []
        self.thread_id: str | None = "t-900"
        self.attachment_fetches: list[str] = []
        self.attachment_bytes: bytes | None = b"\x89PNG\r\n\x1a\nfake"
        self.typing_calls: list[tuple[str, str]] = []
        self.typing_raises = False
        self.registrations: list[tuple[str, str, list[dict[str, object]]]] = []
        self.register_status = 200
        self.gateway = DiscordGateway(
            self.engine,
            web_ui_url=web_ui_url,
            connect=self._connect,
            send=self._send,
            ack=self._ack,
            ack_deferred=self._ack_deferred,
            followup=self._followup,
            spawn=self._spawn,
            register=self._register,
            create_thread=self._create_thread,
            fetch_attachment=self._fetch_attachment,
            typing=self._typing,
        )

    def script(self, *frames: str) -> None:
        self.connections.append(_FakeConnection(list(frames)))

    def _connect(self, url: str) -> _FakeConnection:
        self.connect_urls.append(url)
        return self.connections.pop(0)

    def _send(self, token: str, channel_id: str, payload: dict[str, object]) -> bool:
        self.sent.append((token, channel_id, payload))
        return True

    def _ack(self, interaction_id: str, interaction_token: str, text: str) -> bool:
        self.acks.append((interaction_id, interaction_token, text))
        return True

    def _ack_deferred(self, interaction_id: str, interaction_token: str) -> bool:
        self.deferred_acks.append((interaction_id, interaction_token))
        return True

    def _followup(self, application_id: str, interaction_token: str, text: str) -> bool:
        self.followups.append((application_id, interaction_token, text))
        return True

    def _spawn(self, work) -> None:  # type: ignore[no-untyped-def]
        # Inline: the suite must observe the verdict deterministically. The
        # ORDER is still honest — _handle_interaction defers the ack before
        # this runs.
        self.spawned_inline += 1
        work()

    def _register(self, token: str, application_id: str, commands: list[dict[str, object]]) -> int:
        self.registrations.append((token, application_id, commands))
        return self.register_status

    def _create_thread(self, token: str, channel_id: str, message_id: str, name: str) -> str | None:
        self.threads.append((token, channel_id, message_id, name))
        return self.thread_id

    def _fetch_attachment(self, url: str) -> bytes | None:
        self.attachment_fetches.append(url)
        return self.attachment_bytes

    def _typing(self, token: str, channel_id: str) -> bool:
        if self.typing_raises:
            raise ConnectionError("typing endpoint down")
        self.typing_calls.append((token, channel_id))
        return True

    def close(self) -> None:
        self.runner.shutdown()
        self.store.close()


def _enable_discord(config: SupervisorConfig, ollama: object, *, can_confirm: bool = False) -> None:
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
        "/api/channels/discord",
        json={
            "enabled": True,
            "channel_can_confirm": can_confirm,
            "allowed_identities": ["42"],
            "secret": "discord-bot-token",
        },
    )


def test_discord_gateway_inert_until_enabled_with_a_secret(config: SupervisorConfig) -> None:
    harness = _DiscordHarness(config)
    try:
        assert harness.gateway.session_once() is False
        assert harness.connect_urls == []  # not ready -> no connection at all
    finally:
        harness.close()


def test_discord_identifies_and_routes_a_message_through_a_real_turn(
    config: SupervisorConfig,
) -> None:
    from skep.supervisor.serve.channels.runtime import DISCORD_INTENTS

    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_discord(config, ollama)
        harness = _DiscordHarness(config)
        try:
            ollama.script_reply("hello from the hive")
            harness.script(HELLO, READY, _message_create("42", "status?"))
            connection = harness.connections[0]
            assert harness.gateway.session_once() is True

            assert len(harness.connect_urls) == 1
            # IDENTIFY carried the stored token and the declared intents.
            identify = next(f for f in connection.sent if f.get("op") == 2)
            assert identify["d"]["token"] == "discord-bot-token"
            assert identify["d"]["intents"] == DISCORD_INTENTS
            # The reply went back to the channel over REST.
            assert harness.sent == [("discord-bot-token", "42", {"content": "hello from the hive"})]
            # The conversation is a durable, bound chat session.
            binding = harness.store.channel_session("discord:42")
            assert binding is not None
            roles = [m.role for m in harness.store.chat_messages(binding.chat_id)]
            assert roles == ["user", "assistant"]
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_discord_typing_pulses_during_the_turn_and_never_breaks_it(
    config: SupervisorConfig,
) -> None:
    """v47-F8: 'skep is typing…' fires while the Queen thinks, in the channel
    the reply will land in — and a broken typing endpoint costs nothing."""
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_discord(config, ollama)
        harness = _DiscordHarness(config)
        try:
            ollama.script_reply("thinking done")
            harness.script(HELLO, READY, _message_create("42", "status?"))
            assert harness.gateway.session_once() is True
            assert ("discord-bot-token", "42") in harness.typing_calls
            assert harness.sent[-1][2] == {"content": "thinking done"}

            # A typing failure is swallowed; the reply still lands.
            harness.typing_raises = True
            ollama.script_reply("still here")
            harness.script(HELLO, READY, _message_create("42", "and now?"))
            assert harness.gateway.session_once() is True
            assert harness.sent[-1][2] == {"content": "still here"}
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_discord_unknown_identity_fails_closed_and_bots_are_ignored(
    config: SupervisorConfig,
) -> None:
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_discord(config, ollama)
        harness = _DiscordHarness(config)
        try:
            harness.script(
                HELLO,
                READY,
                _message_create("99", "let me in"),  # stranger
                _message_create("42", "echo", bot=True),  # our own bot
            )
            assert harness.gateway.session_once() is True
            assert harness.sent == []  # silence to strangers, no self-replies
            assert ollama.chat_bodies() == []  # and no model turn at all
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_discord_mutation_renders_a_card_and_heartbeats_flow(
    config: SupervisorConfig,
) -> None:
    """A proposed mutation becomes the ordinary pending card, rendered as the
    adapter's embed; the scripted idle gaps prove the heartbeat path."""
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_discord(config, ollama)
        harness = _DiscordHarness(config)
        try:
            ollama.script_tool_call("set_policy", {"default_execution_mode": "workspace"})
            # interval 0: any idle timeout is instantly "past due" — the
            # heartbeat path fires deterministically, no sleeping in the test.
            hello_fast = json.dumps({"op": 10, "d": {"heartbeat_interval": 0}})
            harness.script(
                hello_fast,
                READY,
                "TIMEOUT",  # idle past the (1ms) interval -> heartbeat
                _message_create("42", "loosen the policy"),
                json.dumps({"op": 1}),  # server demands a heartbeat
            )
            connection = harness.connections[0]
            assert harness.gateway.session_once() is True
            heartbeats = [f for f in connection.sent if f.get("op") == 1]
            assert len(heartbeats) >= 2  # one from idle, one on demand
            # The card: an embed payload whose buttons are stamped with the
            # chat-action id (set_policy is web-UI-only -> no buttons at all).
            binding = harness.store.channel_session("discord:42")
            assert binding is not None
            (action,) = harness.store.pending_chat_actions(binding.chat_id)
            assert action.tool == "set_policy"
            card = next(p for _, _, p in harness.sent if "embeds" in p)
            assert "web UI" in str(card)
            assert "components" not in card  # never confirmable from a channel
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_discord_button_confirm_is_gated_by_the_v16_allowlist(
    config: SupervisorConfig,
) -> None:
    """A shell approval can NEVER be confirmed from Discord, whatever the
    config; the fail-closed reason reaches the interaction ack, card stays."""
    from skep.supervisor import RunStore

    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_discord(config, ollama, can_confirm=True)
        store = RunStore(config.db_path)
        try:
            chat = store.create_chat(title="discord 42", model=None)
            action_id = store.add_chat_action(
                chat.chat_id, tool="allow_command_review", args={"review_id": "r-1"}
            )
        finally:
            store.close()
        harness = _DiscordHarness(config)
        try:
            interaction = json.dumps(
                {
                    "op": 0,
                    "t": "INTERACTION_CREATE",
                    "s": 2,
                    "d": {
                        "id": "i-1",
                        "token": "itok-1",
                        "channel_id": "42",
                        "data": {"custom_id": f"confirm:{action_id}"},
                    },
                }
            )
            harness.script(HELLO, READY_APP, interaction)
            assert harness.gateway.session_once() is True
            # v106-F13: the click is deferred-acked inside Discord's 3s
            # window; the refusal text arrives as the follow-up.
            assert harness.deferred_acks, "the click must be acked before any work"
            reply = harness.followups[-1][2]
            assert "channel.confirm.denied.web_ui_only_action_class" in reply
            assert "web UI" in reply
            refreshed = harness.store.get_chat_action(action_id)
            assert refreshed is not None and refreshed.status == "proposed"  # card waits
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_discord_button_confirms_a_low_risk_dispatch(
    repo: object, config: SupervisorConfig
) -> None:
    from skep.supervisor import RunStore

    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_discord(config, ollama, can_confirm=True)
        store = RunStore(config.db_path)
        try:
            chat = store.create_chat(title="discord 42", model=None)
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
        harness = _DiscordHarness(config)
        try:
            ollama.script_reply("dispatched — I will report back")
            interaction = json.dumps(
                {
                    "op": 0,
                    "t": "INTERACTION_CREATE",
                    "s": 2,
                    "d": {
                        "id": "i-2",
                        "token": "itok-2",
                        "channel_id": "42",
                        "data": {"custom_id": f"confirm:{action_id}"},
                    },
                }
            )
            harness.script(HELLO, READY_APP, interaction)
            assert harness.gateway.session_once() is True
            resolved = harness.store.get_chat_action(action_id)
            assert resolved is not None and resolved.status == "confirmed"
            assert harness.store.recent_runs(5), "the confirmed dispatch_run must dispatch"
            assert harness.deferred_acks, "the click must be acked before any work"
            assert "dispatched" in harness.followups[-1][2]
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_discord_button_from_a_session_bound_thread_confirms_its_own_card(
    repo: object, config: SupervisorConfig
) -> None:
    """v66-F1: auto_thread re-homes the conversation into a thread id the
    allowlist never heard of — the session binding to the card's OWN chat is
    the admission; the confirm gate must honor it like inbound routing does."""
    from skep.supervisor import RunStore

    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_discord(config, ollama, can_confirm=True)  # allowlist is ["42"]
        store = RunStore(config.db_path)
        try:
            chat = store.create_chat(title="discord t-900", model=None)
            store.bind_channel_session(
                session_key="discord:t-900",
                channel="discord",
                identity_id="t-900",
                chat_id=chat.chat_id,
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
        harness = _DiscordHarness(config)
        try:
            ollama.script_reply("dispatched from the thread")
            interaction = json.dumps(
                {
                    "op": 0,
                    "t": "INTERACTION_CREATE",
                    "s": 2,
                    "d": {
                        "id": "i-3",
                        "token": "itok-3",
                        "channel_id": "t-900",  # NOT in allowed_identities
                        "data": {"custom_id": f"confirm:{action_id}"},
                    },
                }
            )
            harness.script(HELLO, READY_APP, interaction)
            assert harness.gateway.session_once() is True
            resolved = harness.store.get_chat_action(action_id)
            assert resolved is not None and resolved.status == "confirmed"
            assert harness.deferred_acks, "the click must be acked before any work"
            assert "dispatched" in harness.followups[-1][2]
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_discord_button_from_a_foreign_thread_stays_gated(
    config: SupervisorConfig,
) -> None:
    """v66-F1 fail-closed edge: a binding to a DIFFERENT chat cannot resolve
    this card — and an unbound channel id never could."""
    from skep.supervisor import RunStore

    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_discord(config, ollama, can_confirm=True)
        store = RunStore(config.db_path)
        try:
            card_chat = store.create_chat(title="discord 42", model=None)
            other_chat = store.create_chat(title="discord t-777", model=None)
            store.bind_channel_session(
                session_key="discord:t-777",
                channel="discord",
                identity_id="t-777",
                chat_id=other_chat.chat_id,
            )
            action_id = store.add_chat_action(
                card_chat.chat_id, tool="dispatch_run", args={"repo": "r", "instructions": "x"}
            )
        finally:
            store.close()
        harness = _DiscordHarness(config)
        try:
            for channel_id, interaction_id in (("t-777", "i-4"), ("t-000", "i-5")):
                interaction = json.dumps(
                    {
                        "op": 0,
                        "t": "INTERACTION_CREATE",
                        "s": 2,
                        "d": {
                            "id": interaction_id,
                            "token": f"tok-{interaction_id}",
                            "channel_id": channel_id,
                            "data": {"custom_id": f"confirm:{action_id}"},
                        },
                    }
                )
                harness.script(HELLO, READY_APP, interaction)
                assert harness.gateway.session_once() is True
                assert "channel.confirm.denied.identity_not_allowlisted" in harness.followups[-1][2]
            refreshed = harness.store.get_chat_action(action_id)
            assert refreshed is not None and refreshed.status == "proposed"  # card waits
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_discord_reaction_confirms_the_waiting_card(repo: object, config: SupervisorConfig) -> None:
    from skep.supervisor import RunStore

    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_discord(config, ollama, can_confirm=True)
        store = RunStore(config.db_path)
        try:
            chat = store.create_chat(title="discord 42", model=None)
            store.bind_channel_session(
                session_key="discord:42", channel="discord", identity_id="42", chat_id=chat.chat_id
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
        harness = _DiscordHarness(config)
        try:
            ollama.script_reply("on it")
            reaction = json.dumps(
                {
                    "op": 0,
                    "t": "MESSAGE_REACTION_ADD",
                    "s": 2,
                    "d": {"channel_id": "42", "emoji": {"name": "✅"}},
                }
            )
            harness.script(HELLO, READY, reaction)
            assert harness.gateway.session_once() is True
            resolved = harness.store.get_chat_action(action_id)
            assert resolved is not None and resolved.status == "confirmed"
            assert any("on it" in str(p) for _, _, p in harness.sent)
        finally:
            harness.close()
    finally:
        ollama.stop()


# v44-F1: routing parity over the gateway — mention gating + auto-threads.
READY_WITH_USER = json.dumps(
    {"op": 0, "t": "READY", "s": 1, "d": {"session_id": "sess-1", "user": {"id": "bot9"}}}
)


def _guild_message(
    channel_id: str,
    content: str,
    *,
    message_id: str = "m-1",
    mention_bot: bool = False,
    seq: int = 2,
) -> str:
    return json.dumps(
        {
            "op": 0,
            "t": "MESSAGE_CREATE",
            "s": seq,
            "d": {
                "id": message_id,
                "channel_id": channel_id,
                "guild_id": "g1",
                "content": content,
                "author": {"id": "u-op"},
                "mentions": [{"id": "bot9"}] if mention_bot else [],
            },
        }
    )


def _routing_on(config: SupervisorConfig, ollama: object) -> None:
    _enable_discord(config, ollama)
    serve_client(config).put(
        "/api/channels/discord",
        json={"require_mention": True, "auto_thread": True, "allowed_users": ["u-op"]},
    )


def test_discord_mention_spawns_a_thread_and_binds_the_conversation_there(
    config: SupervisorConfig,
) -> None:
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _routing_on(config, ollama)
        harness = _DiscordHarness(config)
        try:
            ollama.script_reply("threaded hello")
            harness.script(
                HELLO, READY_WITH_USER, _guild_message("42", "<@bot9> status?", mention_bot=True)
            )
            assert harness.gateway.session_once() is True
            # The thread came off the mention message, named after the text.
            assert harness.threads == [("discord-bot-token", "42", "m-1", "status?")]
            # The reply went to the THREAD, and the session is bound to it.
            assert harness.sent == [("discord-bot-token", "t-900", {"content": "threaded hello"})]
            binding = harness.store.channel_session("discord:t-900")
            assert binding is not None
            roles = [m.role for m in harness.store.chat_messages(binding.chat_id)]
            assert roles == ["user", "assistant"]
            # Follow-up inside the thread routes without a mention.
            ollama.script_reply("still here")
            harness.script(
                HELLO,
                json.dumps({"op": 0, "t": "RESUMED", "s": 3, "d": {}}),
                _guild_message("t-900", "and the runs?", message_id="m-2", seq=4),
            )
            assert harness.gateway.session_once() is True
            # no re-thread for the bound thread:
            assert harness.threads == [("discord-bot-token", "42", "m-1", "status?")]
            assert harness.sent[-1] == (
                "discord-bot-token",
                "t-900",
                {"content": "still here"},
            )
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_discord_require_mention_silences_unaddressed_guild_chatter(
    config: SupervisorConfig,
) -> None:
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _routing_on(config, ollama)
        harness = _DiscordHarness(config)
        try:
            harness.script(
                HELLO, READY_WITH_USER, _guild_message("42", "humans talking amongst themselves")
            )
            assert harness.gateway.session_once() is True
            assert harness.sent == []  # no reply
            assert harness.threads == []  # no thread
            assert ollama.chat_bodies() == []  # and no model turn at all
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_discord_thread_creation_failure_falls_back_in_channel(
    config: SupervisorConfig,
) -> None:
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _routing_on(config, ollama)
        harness = _DiscordHarness(config)
        harness.thread_id = None  # REST thread creation fails
        try:
            ollama.script_reply("in-channel fallback")
            harness.script(
                HELLO, READY_WITH_USER, _guild_message("42", "<@bot9> hi", mention_bot=True)
            )
            assert harness.gateway.session_once() is True
            assert harness.sent == [("discord-bot-token", "42", {"content": "in-channel fallback"})]
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_discord_image_attachment_rides_the_user_message(
    config: SupervisorConfig,
) -> None:
    """v44-F9: an image on the Discord message is downloaded (injected fetch),
    size/type gated, and stored on the chat's user row; non-images are ignored."""
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_discord(config, ollama)
        harness = _DiscordHarness(config)
        try:
            ollama.script_reply("nice screenshot")
            frame = json.dumps(
                {
                    "op": 0,
                    "t": "MESSAGE_CREATE",
                    "s": 2,
                    "d": {
                        "channel_id": "42",
                        "content": "look at this",
                        "author": {"id": "u1"},
                        "attachments": [
                            {
                                "url": "https://cdn.discord.test/shot.png",
                                "content_type": "image/png",
                                "size": 1234,
                            },
                            {
                                "url": "https://cdn.discord.test/notes.pdf",
                                "content_type": "application/pdf",
                                "size": 99,
                            },
                        ],
                    },
                }
            )
            harness.script(HELLO, READY, frame)
            assert harness.gateway.session_once() is True
            assert harness.attachment_fetches == ["https://cdn.discord.test/shot.png"]
            binding = harness.store.channel_session("discord:42")
            assert binding is not None
            (user_row,) = [
                m for m in harness.store.chat_messages(binding.chat_id) if m.role == "user"
            ]
            assert user_row.attachments is not None and len(user_row.attachments) == 1
            stored = config.home / "chat-attachments" / binding.chat_id / user_row.attachments[0]
            assert stored.read_bytes().startswith(b"\x89PNG")
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_discord_resumes_after_a_reconnect_request(config: SupervisorConfig) -> None:
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_discord(config, ollama)
        harness = _DiscordHarness(config)
        try:
            harness.script(HELLO, READY, RECONNECT)
            harness.script(HELLO, json.dumps({"op": 0, "t": "RESUMED", "s": 5, "d": {}}))
            first = harness.connections[0]
            second = harness.connections[1]
            assert harness.gateway.session_once() is True
            assert harness.gateway.session_once() is True
            assert first.sent[0]["op"] == 2  # fresh session: IDENTIFY
            resume = second.sent[0]
            assert resume["op"] == 6  # after op 7: RESUME with session + seq
            assert resume["d"]["session_id"] == "sess-1"
            assert resume["d"]["seq"] == 1
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_discord_gateway_speaks_real_websockets(config: SupervisorConfig) -> None:
    """The transport proof: the DEFAULT connect factory against an in-test
    ``websockets`` server — the dependency's actual wire path, no fakes."""
    import threading

    from websockets.sync.server import serve as ws_serve

    from skep.supervisor.serve.channels import runtime

    from .fake_ollama import FakeOllama

    received: list[dict[str, Any]] = []
    served = threading.Event()

    def handler(connection: Any) -> None:
        connection.send(HELLO)
        received.append(json.loads(connection.recv(timeout=10)))  # IDENTIFY
        connection.send(READY)
        connection.send(RECONNECT)
        served.set()

    ollama = FakeOllama(api_key="sk-fake").start()
    server = ws_serve(handler, "127.0.0.1", 0)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        port = server.socket.getsockname()[1]
        _enable_discord(config, ollama)
        harness = _DiscordHarness(config)
        harness.gateway._gateway_url = f"ws://127.0.0.1:{port}/"
        harness.gateway._connect = runtime._default_gateway_connect
        try:
            assert harness.gateway.session_once() is True
            assert served.wait(timeout=10)
            assert received and received[0]["op"] == 2
            assert received[0]["d"]["token"] == "discord-bot-token"
        finally:
            harness.close()
    finally:
        server.shutdown()
        ollama.stop()


# -- v78-F4: /skep slash commands ------------------------------------------

READY_APP = json.dumps(
    {
        "op": 0,
        "t": "READY",
        "s": 1,
        "d": {"session_id": "sess-1", "application": {"id": "app-1"}},
    }
)


def _slash(sub: str, channel_id: str = "42", *, user_id: str = "u1", seq: int = 2) -> str:
    return json.dumps(
        {
            "op": 0,
            "t": "INTERACTION_CREATE",
            "s": seq,
            "d": {
                "id": f"i-{sub}-{seq}",
                "token": f"itok-{sub}-{seq}",
                "type": 2,
                "channel_id": channel_id,
                "member": {"user": {"id": user_id}},
                "data": {"name": "skep", "options": [{"name": sub, "type": 1}]},
            },
        }
    )


def test_discord_registers_the_skep_tree_once_after_ready(config: SupervisorConfig) -> None:
    """The bulk-overwrite PUT: four subcommands, once per session; a 403
    teaches the missing invite scope and never breaks the session."""
    import logging as logging_mod

    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_discord(config, ollama)
        harness = _DiscordHarness(config)
        try:
            harness.script(HELLO, READY_APP)
            assert harness.gateway.session_once() is True
            (registration,) = harness.registrations  # once, not per event
            token, application_id, commands = registration
            assert token == "discord-bot-token"
            assert application_id == "app-1"
            (root,) = commands
            assert root["name"] == "skep"
            options = root["options"]
            assert isinstance(options, list)
            assert [o["name"] for o in options] == ["status", "runs", "approve", "deny"]
        finally:
            harness.close()

        # A 403 logs the teaching line and the session still comes up.
        harness = _DiscordHarness(config)
        try:
            harness.register_status = 403
            harness.script(HELLO, READY_APP)
            records: list[logging_mod.LogRecord] = []

            class _Capture(logging_mod.Handler):
                def emit(self, record: logging_mod.LogRecord) -> None:
                    records.append(record)

            capture = _Capture()
            logging_mod.getLogger("skep.serve").addHandler(capture)
            try:
                assert harness.gateway.session_once() is True
            finally:
                logging_mod.getLogger("skep.serve").removeHandler(capture)
            assert any("applications.commands scope" in r.getMessage() for r in records)
        finally:
            harness.close()

        # A READY without an application id registers nothing (old wire shape).
        harness = _DiscordHarness(config)
        try:
            harness.script(HELLO, READY)
            assert harness.gateway.session_once() is True
            assert harness.registrations == []
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_discord_slash_from_a_stranger_gets_no_ack(config: SupervisorConfig) -> None:
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_discord(config, ollama)  # allowlist is ["42"]
        harness = _DiscordHarness(config)
        try:
            harness.script(HELLO, READY, _slash("status", channel_id="99"))
            assert harness.gateway.session_once() is True
            assert harness.acks == []  # we volunteer nothing to strangers
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_discord_slash_status_and_runs_answer_without_a_model_turn(
    config: SupervisorConfig,
) -> None:
    """The v25 deck pattern, server-side: deterministic store reads, zero
    model calls, F2 emoji on the lines."""
    from pathlib import Path as PathMod

    from skep.supervisor import RunStore
    from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task

    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_discord(config, ollama)
        store = RunStore(config.db_path)
        try:
            done = mint_task(
                workspace=PathMod("/tmp/ws-a"), instructions="x", budget=DEFAULT_BUDGET
            )
            store.create_run(done, repo=PathMod("/tmp/r"), ref=None, execution_mode="sandbox")
            store.transition(done.task_id, "completed", None)
            gated = mint_task(
                workspace=PathMod("/tmp/ws-b"), instructions="y", budget=DEFAULT_BUDGET
            )
            store.create_run(gated, repo=PathMod("/tmp/r"), ref=None, execution_mode="sandbox")
            store.enqueue_approval(gated.task_id, action="shell.run", reason="wants: make")
            store.transition(gated.task_id, "pending_approval", "gate")
        finally:
            store.close()
        harness = _DiscordHarness(config)
        try:
            harness.script(HELLO, READY, _slash("status", seq=2), _slash("runs", seq=3))
            assert harness.gateway.session_once() is True
            status_ack, runs_ack = harness.acks[0][2], harness.acks[1][2]
            assert "1 approval(s) waiting" in status_ack
            assert "needs your approval" in status_ack and "🟡" in status_ack
            assert "🟢" in runs_ack and done.task_id[:13] in runs_ack
            assert gated.task_id[:13] in runs_ack
            assert ollama.chat_bodies() == []  # the model was never asked
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_discord_slash_approve_confirms_the_bound_chats_card(
    repo: object, config: SupervisorConfig
) -> None:
    from skep.supervisor import RunStore

    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_discord(config, ollama, can_confirm=True)
        store = RunStore(config.db_path)
        try:
            chat = store.create_chat(title="discord 42", model=None)
            store.bind_channel_session(
                session_key="discord:42",
                channel="discord",
                identity_id="42",
                chat_id=chat.chat_id,
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
        harness = _DiscordHarness(config)
        try:
            ollama.script_reply("dispatched — I will report back")
            harness.script(HELLO, READY_APP, _slash("approve"))
            assert harness.gateway.session_once() is True
            resolved = harness.store.get_chat_action(action_id)
            assert resolved is not None and resolved.status == "confirmed"
            assert harness.store.recent_runs(5), "the confirmed dispatch_run must dispatch"
            # v106-F13: /skep approve rides the same deferred flow as buttons.
            assert harness.deferred_acks, "the verdict must be acked before any work"
            assert "dispatched" in harness.followups[-1][2]
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_discord_slash_approve_with_a_web_only_card_stays_gated(
    config: SupervisorConfig,
) -> None:
    """THE pin that the gate is the same gate: a shell-class card answered
    web-UI-only through /skep approve exactly as through the button; the card
    stays proposed."""
    from skep.supervisor import RunStore

    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_discord(config, ollama, can_confirm=True)
        store = RunStore(config.db_path)
        try:
            chat = store.create_chat(title="discord 42", model=None)
            store.bind_channel_session(
                session_key="discord:42",
                channel="discord",
                identity_id="42",
                chat_id=chat.chat_id,
            )
            action_id = store.add_chat_action(
                chat.chat_id, tool="allow_command_review", args={"review_id": "r-1"}
            )
        finally:
            store.close()
        harness = _DiscordHarness(config)
        try:
            harness.script(HELLO, READY_APP, _slash("approve"))
            assert harness.gateway.session_once() is True
            reply = harness.followups[-1][2]
            assert "channel.confirm.denied.web_ui_only_action_class" in reply
            assert "web UI" in reply
            refreshed = harness.store.get_chat_action(action_id)
            assert refreshed is not None and refreshed.status == "proposed"
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_discord_slash_deny_and_the_empty_queue_teach(
    repo: object, config: SupervisorConfig
) -> None:
    from skep.supervisor import RunStore

    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_discord(config, ollama, can_confirm=True)
        store = RunStore(config.db_path)
        try:
            chat = store.create_chat(title="discord 42", model=None)
            store.bind_channel_session(
                session_key="discord:42",
                channel="discord",
                identity_id="42",
                chat_id=chat.chat_id,
            )
            action_id = store.add_chat_action(
                chat.chat_id,
                tool="dispatch_run",
                args={"repo": str(repo), "instructions": "x", "execution_mode": "sandbox"},
            )
        finally:
            store.close()
        harness = _DiscordHarness(config)
        try:
            ollama.script_reply("okay, not dispatching")
            harness.script(HELLO, READY_APP, _slash("deny", seq=2), _slash("approve", seq=3))
            assert harness.gateway.session_once() is True
            resolved = harness.store.get_chat_action(action_id)
            assert resolved is not None and resolved.status == "denied"
            assert harness.store.recent_runs(5) == []  # nothing executed
            # The follow-up /skep approve finds no card and says so (I9).
            assert "no card is waiting" in harness.followups[-1][2]
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_gateway_session_outcome_leaves_a_health_breadcrumb(
    config: SupervisorConfig,
) -> None:
    """v87-F3: session_once records its outcome in settings — the health
    line can say 'idle: disabled or secret missing' instead of nothing."""
    rig = _DiscordHarness(config)

    def _crumb() -> dict[str, object]:
        value = rig.store.get_setting("channel_gateway_state:discord")
        assert isinstance(value, dict)
        return value

    try:
        # No config, no secret: idle, said so.
        assert rig.gateway.session_once() is False
        assert _crumb()["state"] == "idle: channel disabled or secret missing"

        # Enabled with a token: a scripted READY session records "session ok".
        from skep.supervisor.serve.channels import ChannelConfig, store_channel_secret

        rig.store.upsert_channel_config(
            ChannelConfig(channel="discord", enabled=True, allowed_identities=("42",))
        )
        store_channel_secret(config.home, "discord", "tok-d")
        rig.script(HELLO, READY)
        assert rig.gateway.session_once() is True
        assert _crumb()["state"] == "session ok"
    finally:
        rig.close()


def test_button_click_is_acked_before_the_verdict_runs(
    config: SupervisorConfig,
) -> None:
    """v106-F13 (field, 2026-07-29): Discord invalidates an unacknowledged
    interaction after 3 seconds, and a verdict runs a mutation plus a model
    continuation. The old ack-last flow meant every Allow click read 'This
    interaction failed' and the reply died with the token. The deferred ack
    must land BEFORE any verdict work starts."""
    from skep.supervisor import RunStore

    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_discord(config, ollama, can_confirm=True)
    finally:
        ollama.stop()
    store = RunStore(config.db_path)
    try:
        chat = store.create_chat(title="discord 42", model=None)
        action_id = store.add_chat_action(chat.chat_id, tool="read_url", args={"url": "x"})
    finally:
        store.close()
    harness = _DiscordHarness(config)
    try:
        timeline: list[str] = []
        real_deferred = harness._ack_deferred

        def deferred(interaction_id: str, interaction_token: str) -> bool:
            timeline.append("deferred-ack")
            return real_deferred(interaction_id, interaction_token)

        def verdict(*_a: object, **_k: object) -> str:
            timeline.append("verdict")
            return "skep: done."

        harness.gateway._ack_deferred = deferred
        harness.gateway._resolve_verdict = verdict  # type: ignore[method-assign]
        interaction = json.dumps(
            {
                "op": 0,
                "t": "INTERACTION_CREATE",
                "s": 2,
                "d": {
                    "id": "i-9",
                    "token": "itok-9",
                    "channel_id": "42",
                    "data": {"custom_id": f"confirm:{action_id}"},
                },
            }
        )
        harness.script(HELLO, READY_APP, interaction)
        assert harness.gateway.session_once() is True
        assert timeline == ["deferred-ack", "verdict"]
        assert harness.followups[-1][2] == "skep: done."
        assert harness.followups[-1][0] == "app-1"  # READY_APP's application id
    finally:
        harness.close()


def test_followup_failure_falls_back_to_a_channel_message(
    config: SupervisorConfig,
) -> None:
    """v106-F13: a verdict can outlive the follow-up webhook's 15 minutes —
    the reply must still reach the channel instead of dying with the token."""
    from skep.supervisor import RunStore

    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_discord(config, ollama, can_confirm=True)
    finally:
        ollama.stop()
    store = RunStore(config.db_path)
    try:
        chat = store.create_chat(title="discord 42", model=None)
        action_id = store.add_chat_action(chat.chat_id, tool="read_url", args={"url": "x"})
    finally:
        store.close()
    harness = _DiscordHarness(config)
    try:
        harness.gateway._resolve_verdict = (  # type: ignore[method-assign]
            lambda *a, **k: "skep: late but honest."
        )
        harness.gateway._followup = lambda *a: False
        interaction = json.dumps(
            {
                "op": 0,
                "t": "INTERACTION_CREATE",
                "s": 2,
                "d": {
                    "id": "i-10",
                    "token": "itok-10",
                    "channel_id": "42",
                    "data": {"custom_id": f"deny:{action_id}"},
                },
            }
        )
        harness.script(HELLO, READY_APP, interaction)
        assert harness.gateway.session_once() is True
        sent_texts = [str(payload.get("content", "")) for _, _, payload in harness.sent]
        assert any("late but honest" in text for text in sent_texts)
    finally:
        harness.close()
