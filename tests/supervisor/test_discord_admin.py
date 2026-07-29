"""v44-F5: discord_admin moderation verbs — carded, web-UI-confirm-only.

The transport is monkeypatched module attributes; no live Discord. The
security pins matter most here: nothing fires before the human verdict, and
the action classes are NEVER channel-confirmable — a hijacked Discord account
must not be able to confirm its own moderation card.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.serve.channels import channel_confirmation_decision
from skep.supervisor.serve.tools import MUTATING_TOOL_NAMES, READ_TOOL_NAMES

from .fake_ollama import FakeOllama
from .test_serve_chat import sse_events
from .test_serve_chat_tools import chat_client


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


def test_discord_admin_classes_are_mutating_and_never_channel_confirmable() -> None:
    for tool in ("discord_delete_message", "discord_timeout_member"):
        assert tool in MUTATING_TOOL_NAMES and tool not in READ_TOOL_NAMES
        decision = channel_confirmation_decision(
            action_class=tool, channel_can_confirm=True, identity_allowlisted=True
        )
        assert decision.allowed is False
        assert decision.reason == "channel.confirm.denied.web_ui_only_action_class"


def test_confirmed_delete_message_calls_discord_and_not_before(
    config: SupervisorConfig, ollama: FakeOllama, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, chat_id = chat_client(config, ollama)
    client.put("/api/channels/discord", json={"enabled": True, "secret": "bot-tok"})
    calls: list[tuple[str, str, str]] = []

    def _fake_delete(token: str, channel_id: str, message_id: str) -> bool:
        calls.append((token, channel_id, message_id))
        return True

    monkeypatch.setattr("skep.supervisor.serve.discord_admin.delete_message", _fake_delete)
    ollama.script_tool_call("discord_delete_message", {"channel_id": "42", "message_id": "m-9"})
    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "delete that spam"}).text
    )
    assert events[-1] == ("done", {"state": "awaiting_confirmation"})
    assert calls == []  # nothing happens before the human verdict

    action_id = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]
    ollama.script_reply("gone")
    client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm")
    assert calls == [("bot-tok", "42", "m-9")]
    action = client.get(f"/api/chats/{chat_id}").json()["actions"][0]
    assert action["status"] == "confirmed" and action["result"]["ok"] is True


def test_timeout_member_validates_minutes_and_reports_discord_refusals(
    config: SupervisorConfig, ollama: FakeOllama, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, chat_id = chat_client(config, ollama)
    client.put("/api/channels/discord", json={"enabled": True, "secret": "bot-tok"})
    monkeypatch.setattr(
        "skep.supervisor.serve.discord_admin.timeout_member",
        lambda token, guild_id, user_id, minutes: False,  # discord says no
    )
    ollama.script_tool_call(
        "discord_timeout_member", {"guild_id": "g1", "user_id": "u-spam", "minutes": 60}
    )
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "time them out"})
    action_id = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]
    ollama.script_reply("noted")
    client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm")
    result = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["result"]
    assert result["ok"] is False and "rejected the timeout" in result["error"]


def test_unconfigured_discord_channel_yields_a_clean_refusal(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)  # discord never configured
    ollama.script_tool_call("discord_delete_message", {"channel_id": "42", "message_id": "m-9"})
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "delete it"})
    action_id = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]
    ollama.script_reply("understood")
    client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm")
    result = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["result"]
    assert result["ok"] is False and "not enabled/configured" in result["error"]
