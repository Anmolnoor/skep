"""v130-F12: /compact, /usage and /model on the channel deck.

Away from the desk the phone had no way to see what a chat costs or which
model answers it, nor to fold an overflowing chat before the next turn.
Each command reuses what the desk already has: the ADR 0037 deterministic
compaction, the v74 usage tally, the chat's model or the default. /model is
show-only here — changing it stays a desk act.
"""

from __future__ import annotations

from skep.supervisor import SupervisorConfig

from .test_channels_live import _enable_telegram, _telegram_update, _TelegramHarness


def _say(harness: _TelegramHarness, text: str, update_id: int = 1) -> str:
    n = len(harness.sent)
    harness.updates.append([_telegram_update(update_id, "42", text)])
    harness.poller.poll_once()
    return "\n".join(s[2] for s in harness.sent[n:]).replace("\\", "")


def _bound(harness: _TelegramHarness) -> str:
    chat = harness.store.create_chat(title="telegram 42", model=None)
    harness.store.bind_channel_session(
        session_key="telegram:42", channel="telegram", identity_id="42", chat_id=chat.chat_id
    )
    return chat.chat_id


def test_compact_folds_the_overflow_now_and_reports_before_and_after(
    config: SupervisorConfig,
) -> None:
    from skep.supervisor.serve.chat import chat_turn_lock

    from .conftest import serve_client
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama)
        serve_client(config).put("/api/llm/config", json={"num_ctx": 1024})  # 8000-char budget
        harness = _TelegramHarness(config)
        try:
            assert "no session" in _say(harness, "/compact")
            chat_id = _bound(harness)
            assert "nothing to compact" in _say(harness, "/compact", 2)
            for _ in range(30):
                harness.store.add_chat_message(chat_id, role="user", content="q" * 400)
                harness.store.add_chat_message(chat_id, role="assistant", content="a" * 400)
            lock = chat_turn_lock(chat_id)
            lock.acquire()
            try:
                assert "running" in _say(harness, "/compact", 3)
            finally:
                lock.release()
            chat = harness.store.get_chat(chat_id)
            assert chat is not None and not chat.compacted_through
            reply = _say(harness, "/compact", 4)
            assert "compacted" in reply and "→" in reply
            chat = harness.store.get_chat(chat_id)
            assert chat is not None and chat.compacted_through > 0 and chat.context_summary
            assert len(harness.store.chat_messages(chat_id)) == 60  # rows never change (I8)
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_usage_and_model_answer_without_a_model_turn(config: SupervisorConfig) -> None:
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()  # nothing scripted
    try:
        _enable_telegram(config, ollama)
        harness = _TelegramHarness(config)
        try:
            assert "default: qwen3" in _say(harness, "/model")
            chat_id = _bound(harness)
            harness.store.record_llm_usage(
                chat_id=chat_id, model="qwen3", prompt_tokens=1200, completion_tokens=300
            )
            usage = _say(harness, "/usage", 2)
            assert "5h" in usage and "7d" in usage and "1,500" in usage
            assert "this chat" in usage
            harness.store.set_chat_model(chat_id, "big-model")
            assert "big-model" in _say(harness, "/model", 3)
            assert "desk" in _say(harness, "/model other-model", 4)
            chat = harness.store.get_chat(chat_id)
            assert chat is not None and chat.model == "big-model"  # show-only
        finally:
            harness.close()
    finally:
        ollama.stop()
