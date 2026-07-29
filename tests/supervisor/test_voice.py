"""v53-F6 (ADR 0031): voice as a channel-layer capability.

Server-side TTS is config-gated (default none), providers honestly
labeled (piper local; edge/openai CLOUD — reply text egresses), and a
missing provider is a clean logged skip. The web UI's speech APIs are
pinned as source strings, including the Chrome-cloud honesty tooltip.
"""

from __future__ import annotations

from pathlib import Path

from skep.supervisor import RunStore
from skep.supervisor.serve.channels.outbound import push_to_chat_channel
from skep.supervisor.voice import (
    PROVIDER_EGRESS_NOTES,
    TTS_PROVIDER_SETTING,
    TTS_PROVIDERS,
    render_tts,
)


def test_render_tts_none_and_unknown_are_clean_skips(tmp_path: Path) -> None:
    assert render_tts(tmp_path, "hello", provider="none") is None
    assert render_tts(tmp_path, "hello", provider="") is None
    assert render_tts(tmp_path, "hello", provider="mystery") is None
    assert render_tts(tmp_path, "   ", provider="piper") is None


def test_missing_provider_dependency_never_raises(tmp_path: Path) -> None:
    # Neither edge-tts nor a piper binary is installed in the test env: the
    # render must degrade to None (logged), never an exception.
    assert render_tts(tmp_path, "hello", provider="edge") is None


def test_provider_labels_tell_the_egress_truth() -> None:
    assert set(PROVIDER_EGRESS_NOTES) == set(TTS_PROVIDERS)
    assert "local" in PROVIDER_EGRESS_NOTES["piper"]
    for cloud in ("edge", "openai"):
        assert "CLOUD" in PROVIDER_EGRESS_NOTES[cloud]
        assert "leaves this machine" in PROVIDER_EGRESS_NOTES[cloud]


def _bound_discord_chat(store: RunStore) -> str:
    from skep.supervisor.serve.channels import ChannelConfig

    chat = store.create_chat(title="discord", model=None)
    store.upsert_channel_config(ChannelConfig(channel="discord", enabled=True))
    store.bind_channel_session(
        session_key="discord:chan-1", channel="discord", identity_id="chan-1",
        chat_id=chat.chat_id,
    )
    return chat.chat_id


def test_outbound_voice_rides_only_when_a_provider_is_set(
    tmp_path: Path, monkeypatch: object
) -> None:
    home = tmp_path / "supervisor"
    home.mkdir()
    (home / "discord-secret").write_text("bot-token\n")
    store = RunStore(home / "supervisor.sqlite3")
    try:
        chat_id = _bound_discord_chat(store)
        text_sends: list[str] = []
        file_sends: list[Path] = []

        def fake_send(token: str, channel_id: str, payload: dict[str, object]) -> bool:
            text_sends.append(str(payload.get("content")))
            return True

        def fake_send_file(token: str, channel_id: str, path: Path) -> bool:
            file_sends.append(path)
            return True

        # Default: no provider — text only, no render attempted.
        assert push_to_chat_channel(
            store, home, chat_id, "hello", send_discord=fake_send,
            send_discord_file=fake_send_file,
        )
        assert text_sends == ["hello"] and file_sends == []

        # Provider set + a render that succeeds → the voice file follows.
        store.set_setting(TTS_PROVIDER_SETTING, "piper")
        import skep.supervisor.voice as voice_module

        rendered = home / "audio-cache" / "reply.wav"
        rendered.parent.mkdir(exist_ok=True)
        rendered.write_bytes(b"RIFFfake")
        monkeypatch.setattr(  # type: ignore[attr-defined]
            voice_module, "_render_piper", lambda directory, text: rendered
        )
        assert push_to_chat_channel(
            store, home, chat_id, "hello again", send_discord=fake_send,
            send_discord_file=fake_send_file,
        )
        assert text_sends[-1] == "hello again"
        assert file_sends == [rendered]
    finally:
        store.close()


def test_web_ui_voice_is_present_and_honest() -> None:
    from skep.supervisor.serve.app import STATIC_DIR

    source = (STATIC_DIR / "app.js").read_text()
    assert "speechSynthesis" in source
    assert "SpeechRecognition" in source
    # The honesty tooltip: Chrome recognition is cloud-backed and says so.
    assert "sends audio to Google" in source
    # Voice never rides by default — it's a persisted toggle.
    assert 'localStorage.getItem("skep-voice")' in source
