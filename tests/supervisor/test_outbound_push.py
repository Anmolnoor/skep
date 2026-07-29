"""v44-F2: outbound push — scheduled/system messages reach the bound messenger.

Channels were entrances only; ``push_to_chat_channel`` is the exit door. All
sends are injected fakes — no live platform anywhere. Failure is always soft:
the chat row is the durable copy, so a push can only ever return False.
"""

from __future__ import annotations

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.serve.channels import ChannelConfig, store_channel_secret
from skep.supervisor.serve.channels.outbound import push_to_chat_channel


def _bound_chat(store: RunStore, *, channel: str = "discord", identity: str = "42") -> str:
    chat = store.create_chat(title=f"{channel} {identity}", model=None)
    store.bind_channel_session(
        session_key=f"{channel}:{identity}",
        channel=channel,
        identity_id=identity,
        chat_id=chat.chat_id,
    )
    return chat.chat_id


def test_push_reaches_the_bound_discord_conversation(config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        chat_id = _bound_chat(store)
        store.upsert_channel_config(ChannelConfig(channel="discord", enabled=True))
        store_channel_secret(config.home, "discord", "tok-d")
        sent: list[tuple[str, str, dict[str, object]]] = []

        def _send(token: str, cid: str, payload: dict[str, object]) -> bool:
            sent.append((token, cid, payload))
            return True

        ok = push_to_chat_channel(store, config.home, chat_id, "reminder!", send_discord=_send)
        assert ok is True
        assert sent == [("tok-d", "42", {"content": "reminder!"})]
    finally:
        store.close()


def test_push_speaks_telegram_and_slack_too(config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        tg_chat = _bound_chat(store, channel="telegram", identity="7")
        slack_chat = _bound_chat(store, channel="slack", identity="C9")
        store.upsert_channel_config(ChannelConfig(channel="telegram", enabled=True))
        store.upsert_channel_config(ChannelConfig(channel="slack", enabled=True))
        store_channel_secret(config.home, "telegram", "tok-t")
        store_channel_secret(config.home, "slack", "tok-s")
        tg: list[tuple[str, str, str]] = []
        sl: list[tuple[str, str, list[dict[str, object]]]] = []

        def _tg(
            token: str,
            chat_id: str,
            text: str,
            reply_markup: dict[str, object] | None = None,
            parse_mode: str | None = None,
        ) -> bool:
            tg.append((token, chat_id, text))
            return True

        def _sl(
            token: str,
            channel_id: str,
            blocks: list[dict[str, object]],
            thread_ts: str | None = None,
        ) -> bool:
            sl.append((token, channel_id, blocks))
            return True

        assert push_to_chat_channel(store, config.home, tg_chat, "ping", send_telegram=_tg)
        assert push_to_chat_channel(store, config.home, slack_chat, "ping", send_slack=_sl)
        assert tg == [("tok-t", "7", "ping")]
        assert sl[0][:2] == ("tok-s", "C9")
        assert sl[0][2][0]["text"]["text"] == "ping"  # type: ignore[index]
    finally:
        store.close()


def test_push_fails_soft_at_every_gate(config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        # No binding at all.
        plain = store.create_chat(title="web only", model=None)
        assert push_to_chat_channel(store, config.home, plain.chat_id, "x") is False

        # Bound, but the channel is disabled.
        chat_id = _bound_chat(store)
        assert push_to_chat_channel(store, config.home, chat_id, "x") is False

        # Enabled, but no secret stored.
        store.upsert_channel_config(ChannelConfig(channel="discord", enabled=True))
        assert push_to_chat_channel(store, config.home, chat_id, "x") is False

        # Secret present, but the transport raises: swallowed, False.
        store_channel_secret(config.home, "discord", "tok-d")

        def _boom(token: str, cid: str, payload: dict[str, object]) -> bool:
            raise ConnectionError("discord down")

        assert (
            push_to_chat_channel(store, config.home, chat_id, "x", send_discord=_boom) is False
        )
        # And empty text never sends.
        assert push_to_chat_channel(store, config.home, chat_id, "") is False
    finally:
        store.close()


def test_newest_binding_wins_for_a_rebound_chat(config: SupervisorConfig) -> None:
    """A chat rebound to a fresh thread pushes to the thread, not the old
    parent channel (created_at DESC, session_key DESC tiebreak)."""
    store = RunStore(config.db_path)
    try:
        chat_id = _bound_chat(store, identity="42")
        store.bind_channel_session(
            session_key="discord:t-900", channel="discord", identity_id="t-900", chat_id=chat_id
        )
        store.upsert_channel_config(ChannelConfig(channel="discord", enabled=True))
        store_channel_secret(config.home, "discord", "tok-d")
        sent: list[str] = []

        def _send(token: str, cid: str, payload: dict[str, object]) -> bool:
            sent.append(cid)
            return True

        assert push_to_chat_channel(store, config.home, chat_id, "hi", send_discord=_send)
        assert sent == ["t-900"]
    finally:
        store.close()


# -- v78-F1: notification_level filters delivery at the ONE choke point ----


def test_notification_level_filters_delivery_at_the_choke_point(
    config: SupervisorConfig,
) -> None:
    store = RunStore(config.db_path)
    try:
        chat_id = _bound_chat(store)
        store_channel_secret(config.home, "discord", "tok-d")
        sent: list[str] = []

        def _send(token: str, cid: str, payload: dict[str, object]) -> bool:
            sent.append(str(payload.get("content")))
            return True

        def push(text: str, kind: str) -> bool:
            return push_to_chat_channel(
                store, config.home, chat_id, text, kind=kind, send_discord=_send
            )

        # 'all' (the default): every kind delivers — behavior-identical to today.
        store.upsert_channel_config(ChannelConfig(channel="discord", enabled=True))
        assert push("digest", "info") and push("gate", "action_needed")
        # 'approvals': only action-needed kinds deliver.
        store.upsert_channel_config(
            ChannelConfig(channel="discord", enabled=True, notification_level="approvals")
        )
        assert push("tick digest", "info") is False
        assert push("pending gate", "action_needed") is True
        # 'none': nothing delivers, whatever the kind.
        store.upsert_channel_config(
            ChannelConfig(channel="discord", enabled=True, notification_level="none")
        )
        assert push("anything", "action_needed") is False
        assert sent == ["digest", "gate", "pending gate"]
    finally:
        store.close()


def test_none_level_mutes_delivery_but_the_chat_row_still_lands(
    config: SupervisorConfig, monkeypatch: object
) -> None:
    """The level filters DELIVERY, never the record (I8/I11): a muted channel
    still gets its honest chat row for the web UI."""
    from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task
    from skep.supervisor.serve.run_status import notify_run_terminal

    store = RunStore(config.db_path)
    try:
        chat_id = _bound_chat(store)
        store.upsert_channel_config(
            ChannelConfig(channel="discord", enabled=True, notification_level="none")
        )
        store_channel_secret(config.home, "discord", "tok-d")
        sent: list[str] = []
        def _record_send(token: str, cid: str, payload: object) -> bool:
            sent.append(str(payload))
            return True

        monkeypatch.setattr(  # type: ignore[attr-defined]
            "skep.supervisor.serve.channels.outbound._default_discord_send", _record_send
        )
        task = mint_task(
            workspace=config.home / "ws", instructions="x", budget=DEFAULT_BUDGET
        )
        store.create_run(task, repo=config.home, ref=None, execution_mode="sandbox")
        action_id = store.add_chat_action(chat_id, tool="dispatch_run", args={})
        store.resolve_chat_action(
            action_id,
            status="confirmed",
            result={"ok": True, "result": {"task_id": task.task_id}},
        )
        store.transition(task.task_id, "failed", "boom")

        notify_run_terminal(store, config.home, task.task_id)
        (message,) = store.chat_messages(chat_id)
        assert "failed" in message.content  # the record is intact
        assert sent == []  # delivery muted
    finally:
        store.close()


# -- v78-F3: the discord branch attaches a run embed ------------------------


def test_discord_push_with_run_ref_sends_content_plus_embed(
    config: SupervisorConfig,
) -> None:
    from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task

    store = RunStore(config.db_path)
    try:
        chat_id = _bound_chat(store)
        store.upsert_channel_config(ChannelConfig(channel="discord", enabled=True))
        store_channel_secret(config.home, "discord", "tok-d")
        task = mint_task(workspace=config.home / "ws", instructions="x", budget=DEFAULT_BUDGET)
        store.create_run(task, repo=config.home, ref=None, execution_mode="sandbox")
        store.transition(task.task_id, "completed", None)
        store.record_reverification(
            task.task_id,
            outcome="passed",
            worker_outcome="passed",
            confirmed=True,
            commands=["pytest"],
            exit_codes=[0],
            detail="clean re-run",
        )
        payloads: list[dict[str, object]] = []

        def _send(token: str, cid: str, payload: dict[str, object]) -> bool:
            payloads.append(payload)
            return True

        assert push_to_chat_channel(
            store,
            config.home,
            chat_id,
            "🟢 run done",
            run_ref=task.task_id,
            web_ui_url="http://ui.test/",
            send_discord=_send,
        )
        (payload,) = payloads
        assert payload["content"] == "🟢 run done"  # the honest text always rides
        embeds = payload["embeds"]
        assert isinstance(embeds, list) and len(embeds) == 1
        embed = embeds[0]
        assert str(embed["title"]).startswith(f"run {task.task_id[:12]}")
        assert embed["url"] == f"http://ui.test/#/runs/{task.task_id}"
        assert embed["fields"] == [{"name": "verify", "value": "passed"}]

        # No run_ref (ticker/webhook pushes): text-only, exactly as today.
        assert push_to_chat_channel(
            store, config.home, chat_id, "tick digest", send_discord=_send
        )
        assert payloads[-1] == {"content": "tick digest"}

        # A run deleted between terminal and push degrades to text-only.
        assert push_to_chat_channel(
            store,
            config.home,
            chat_id,
            "gone",
            run_ref="no-such-run",
            web_ui_url="http://ui.test/",
            send_discord=_send,
        )
        assert payloads[-1] == {"content": "gone"}
    finally:
        store.close()


# -- v78-F5: the outbound telegram branch speaks MarkdownV2 with a fallback --


def test_telegram_push_tries_markdown_then_resends_plain(
    config: SupervisorConfig,
) -> None:
    store = RunStore(config.db_path)
    try:
        chat_id = _bound_chat(store, channel="telegram", identity="7")
        store.upsert_channel_config(ChannelConfig(channel="telegram", enabled=True))
        store_channel_secret(config.home, "telegram", "tok-t")
        calls: list[tuple[str, str | None]] = []

        def _accepting(
            token: str,
            chat_id: str,
            text: str,
            reply_markup: dict[str, object] | None = None,
            parse_mode: str | None = None,
        ) -> bool:
            calls.append((text, parse_mode))
            return True

        assert push_to_chat_channel(
            store, config.home, chat_id, "**done** run 1.", send_telegram=_accepting
        )
        # One send: converted text, MarkdownV2 declared.
        assert calls == [("*done* run 1\\.", "MarkdownV2")]

        calls.clear()

        def _rejecting(
            token: str,
            chat_id: str,
            text: str,
            reply_markup: dict[str, object] | None = None,
            parse_mode: str | None = None,
        ) -> bool:
            calls.append((text, parse_mode))
            return parse_mode is None  # Telegram 400s the entities

        assert push_to_chat_channel(
            store, config.home, chat_id, "**done** run 1.", send_telegram=_rejecting
        )
        # The gamble failed; the SAME text arrives plain — never lost.
        assert calls == [("*done* run 1\\.", "MarkdownV2"), ("**done** run 1.", None)]
    finally:
        store.close()


def test_every_expected_delivery_leaves_a_health_breadcrumb(
    config: SupervisorConfig,
) -> None:
    """v87-F3: once a binding exists a delivery is expected — each miss and
    each attempt records why in the per-channel settings breadcrumb, so
    'never configured' stops presenting as 'broken'."""
    store = RunStore(config.db_path)
    key = "channel_last_delivery:discord"

    def _crumb() -> dict[str, object]:
        value = store.get_setting(key)
        assert isinstance(value, dict)
        return value

    try:
        chat_id = _bound_chat(store)

        # Bound but never configured.
        push_to_chat_channel(store, config.home, chat_id, "x")
        crumb = _crumb()
        assert crumb["ok"] is False and crumb["note"] == "channel never configured"

        # Configured but disabled.
        store.upsert_channel_config(ChannelConfig(channel="discord", enabled=False))
        push_to_chat_channel(store, config.home, chat_id, "x")
        assert _crumb()["note"] == "channel disabled"

        # Enabled, secret missing.
        store.upsert_channel_config(ChannelConfig(channel="discord", enabled=True))
        push_to_chat_channel(store, config.home, chat_id, "x")
        assert _crumb()["note"] == "secret missing"

        # The transport raising is recorded too.
        store_channel_secret(config.home, "discord", "tok-d")

        def _boom(token: str, cid: str, payload: dict[str, object]) -> bool:
            raise ConnectionError("discord down")

        push_to_chat_channel(store, config.home, chat_id, "x", send_discord=_boom)
        assert _crumb()["note"] == "send raised"

        # A delivered push records success, with the kind that rode it.
        def _ok(token: str, cid: str, payload: dict[str, object]) -> bool:
            return True

        push_to_chat_channel(
            store, config.home, chat_id, "x", kind="action_needed", send_discord=_ok
        )
        crumb = _crumb()
        assert crumb["ok"] is True and crumb["note"] == "delivered"
        assert crumb["kind"] == "action_needed"

        # The health view surfaces the breadcrumb and the configured flag.
        from skep.supervisor.serve.settings import channel_config_view

        view = channel_config_view(store, config.home)
        assert view["discord"]["configured"] is True
        assert view["discord"]["last_delivery"]["note"] == "delivered"
        assert view["telegram"]["configured"] is False  # never configured, said so
        assert view["telegram"]["last_delivery"] is None
    finally:
        store.close()
