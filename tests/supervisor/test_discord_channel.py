"""v16 Step 3: the Discord adapter (fake payloads + fake delivery, no live Discord)."""

from __future__ import annotations

from skep.supervisor.serve.channels import ChannelConfig, ChannelIdentity
from skep.supervisor.serve.channels.discord import (
    confirmation_embed,
    deliver,
    handle_reaction,
    normalize_inbound,
)

_CONFIG = ChannelConfig(
    channel="discord",
    enabled=True,
    channel_can_confirm=True,
    allowed_identities=("chan-1",),
)


def test_allowlisted_message_normalizes_to_a_session() -> None:
    result = normalize_inbound(
        {"channel_id": "chan-1", "content": "audit my repo", "author": {"id": "u1"}}, _CONFIG
    )
    assert result.message is not None
    assert result.message.session_key == "discord:chan-1"
    assert result.message.text == "audit my repo"


def test_unknown_channel_fails_closed_and_audits() -> None:
    result = normalize_inbound({"channel_id": "evil", "content": "hi"}, _CONFIG)
    assert result.message is None
    assert result.rejected_reason == "channel.reject.identity_not_allowlisted"
    assert result.audit == {"channel": "discord", "identity": "evil"}


def test_confirm_embed_is_review_only_for_shell_action() -> None:
    embed = confirmation_embed("allow_command_review", [["git", "status"]], _CONFIG)
    assert embed["embeds"][0]["title"] == "Review in the web UI"  # type: ignore[index]
    assert "components" not in embed  # no confirm/deny buttons for a shell action


def test_confirm_embed_has_buttons_for_low_risk_when_enabled() -> None:
    embed = confirmation_embed("dispatch_run", [], _CONFIG)
    assert "components" in embed


def test_confirm_embed_has_buttons_for_read_url() -> None:
    """v66-F2: read_url (the #2 carded class in the field) is low-risk —
    buttons render when the channel is configured to confirm."""
    embed = confirmation_embed("read_url", [], _CONFIG)
    assert "components" in embed


def test_web_ui_only_embed_names_the_link(  # v66-F3
) -> None:
    embed = confirmation_embed("allow_command_review", [], _CONFIG, web_ui_url="http://ui.test/")
    description = embed["embeds"][0]["description"]  # type: ignore[index]
    assert "http://ui.test/" in description
    # Without a URL the adapter stays byte-compatible with the old text.
    bare = confirmation_embed("allow_command_review", [], _CONFIG)
    assert "http" not in str(bare["embeds"][0]["description"])  # type: ignore[index]


def test_reaction_confirms_low_risk_but_never_shell() -> None:
    identity = ChannelIdentity(channel="discord", identity_id="chan-1")
    ok = handle_reaction(
        reaction="✅", action_class="dispatch_run", identity=identity, config=_CONFIG
    )
    assert ok.allowed is True
    shell = handle_reaction(
        reaction="✅", action_class="allow_command_review", identity=identity, config=_CONFIG
    )
    assert shell.allowed is False


def test_reaction_from_a_session_bound_thread_is_admitted() -> None:
    """v66-F1: auto_thread ids are never in the allowlist — the session
    binding is the admission; shell stays denied even session-bound."""
    thread = ChannelIdentity(channel="discord", identity_id="t-900")
    denied = handle_reaction(
        reaction="✅", action_class="dispatch_run", identity=thread, config=_CONFIG
    )
    assert denied.allowed is False  # unknown id, no binding: fail closed
    bound = handle_reaction(
        reaction="✅",
        action_class="dispatch_run",
        identity=thread,
        config=_CONFIG,
        session_bound=True,
    )
    assert bound.allowed is True
    shell = handle_reaction(
        reaction="✅",
        action_class="allow_command_review",
        identity=thread,
        config=_CONFIG,
        session_bound=True,
    )
    assert shell.allowed is False


# v44-F1: routing parity — mention gating, user allowlist, bound threads.
_ROUTED_CONFIG = ChannelConfig(
    channel="discord",
    enabled=True,
    channel_can_confirm=False,
    allowed_identities=("chan-1",),
    require_mention=True,
    allowed_users=("u-op",),
)


def test_guild_message_without_mention_is_silently_skipped() -> None:
    result = normalize_inbound(
        {
            "channel_id": "chan-1",
            "content": "just chatting",
            "author_id": "u-op",
            "guild_id": "g1",
            "bot_user_id": "bot9",
            "mention_user_ids": [],
        },
        _ROUTED_CONFIG,
    )
    assert result.message is None
    assert result.rejected_reason == "channel.reject.mention_required"


def test_guild_mention_routes_and_strips_the_mention() -> None:
    result = normalize_inbound(
        {
            "channel_id": "chan-1",
            "content": "<@bot9> status please",
            "author_id": "u-op",
            "guild_id": "g1",
            "bot_user_id": "bot9",
            "mention_user_ids": ["bot9"],
        },
        _ROUTED_CONFIG,
    )
    assert result.message is not None
    assert result.message.text == "status please"


def test_mention_only_message_is_malformed() -> None:
    result = normalize_inbound(
        {
            "channel_id": "chan-1",
            "content": "<@!bot9>",
            "author_id": "u-op",
            "guild_id": "g1",
            "bot_user_id": "bot9",
            "mention_user_ids": ["bot9"],
        },
        _ROUTED_CONFIG,
    )
    assert result.message is None
    assert result.rejected_reason == "channel.reject.malformed_payload"


def test_dm_from_allowlisted_user_needs_no_mention_and_no_channel_entry() -> None:
    result = normalize_inbound(
        {
            "channel_id": "dm-77",
            "content": "hello",
            "author_id": "u-op",
            "bot_user_id": "bot9",
        },
        _ROUTED_CONFIG,
    )
    assert result.message is not None
    assert result.message.session_key == "discord:dm-77"


def test_user_allowlist_fails_closed_in_guilds_and_dms() -> None:
    guild = normalize_inbound(
        {
            "channel_id": "chan-1",
            "content": "<@bot9> hi",
            "author_id": "u-stranger",
            "guild_id": "g1",
            "bot_user_id": "bot9",
            "mention_user_ids": ["bot9"],
        },
        _ROUTED_CONFIG,
    )
    assert guild.message is None
    assert guild.rejected_reason == "channel.reject.user_not_allowlisted"
    dm = normalize_inbound(
        {"channel_id": "dm-9", "content": "hi", "author_id": "u-stranger"}, _ROUTED_CONFIG
    )
    assert dm.message is None


def test_bound_thread_is_admitted_without_mention_or_channel_entry() -> None:
    result = normalize_inbound(
        {
            "channel_id": "thread-5",
            "content": "follow-up",
            "author_id": "u-op",
            "guild_id": "g1",
            "bot_user_id": "bot9",
            "session_bound": True,
        },
        _ROUTED_CONFIG,
    )
    assert result.message is not None
    assert result.message.session_key == "discord:thread-5"


def test_delivery_uses_injected_send_and_survives_failure() -> None:
    sent: list[tuple[str, dict[str, object]]] = []

    def _send(cid: str, body: dict[str, object]) -> bool:
        sent.append((cid, body))
        return True

    ok = deliver("done", channel_id="chan-1", send=_send)
    assert ok.ok is True and sent[0][0] == "chan-1"

    def boom(_cid: str, _body: dict[str, object]) -> bool:
        raise ConnectionError("discord down")

    result = deliver("done", channel_id="chan-1", send=boom)
    assert result.ok is False and "discord down" in (result.detail or "")


# -- v78-F3: run result embeds --------------------------------------------


def test_run_status_embed_shape_per_state() -> None:
    """Pure, pinned like confirmation_embed: color by state class, 12-char id
    in the title, summary truncated at 200, verify/url only when present."""
    from skep.supervisor.serve.channels.discord import run_status_embed

    view: dict[str, object] = {
        "task_id": "0f3a9b2c4d5e6f70",
        "state": "completed",
        "summary": "did the thing",
    }
    embed = run_status_embed(view, "http://ui.test/")
    assert embed["title"] == "run 0f3a9b2c4d5e: completed"
    assert embed["color"] == 0x86C99A  # --ok
    assert embed["description"] == "did the thing"
    assert embed["url"] == "http://ui.test/#/runs/0f3a9b2c4d5e6f70"
    assert "fields" not in embed  # no re-verification row -> no verify field

    long_summary = "x" * 500
    failed = run_status_embed(
        {"task_id": "t-1", "state": "worker_crashed", "summary": long_summary, "verify": "failed"}
    )
    assert failed["color"] == 0xE08F96  # --bad
    assert len(str(failed["description"])) == 200  # truncated, never the full dump
    assert failed["fields"] == [{"name": "verify", "value": "failed"}]
    assert "url" not in failed  # no web_ui_url provided -> no link invented

    waiting = run_status_embed({"task_id": "t-2", "state": "pending_approval", "summary": ""})
    assert waiting["color"] == 0xD7B06A  # --warn
    assert "description" not in waiting  # empty summary -> no empty field

    unknown = run_status_embed({"task_id": "t-3", "state": "superseded", "summary": ""})
    assert unknown["color"] == 0x9A9384  # --muted fallback
