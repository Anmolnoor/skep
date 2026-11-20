"""v130-F15: a Telegram photo reaches the model, as a Discord image does.

Before, ``normalize_inbound`` dropped any message without ``text``, so a
photo (its words ride in ``caption``) never arrived. Now the caption is the
text, the largest size within the attachment cap is fetched (getFile + the
file download, injected here — never the network), and a photo with no
caption still lands as an image. Admission is the text rule, unchanged.
"""

from __future__ import annotations

import logging

from skep.supervisor import SupervisorConfig
from skep.supervisor.serve.channels import ChannelConfig
from skep.supervisor.serve.channels.telegram import image_file_id, normalize_inbound
from skep.supervisor.serve.chat import ATTACHMENT_MAX_BYTES

from .test_channels_live import _enable_telegram, _TelegramHarness

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_CONFIG = ChannelConfig(
    channel="telegram", enabled=True, allowed_identities=("42",), allowed_users=("42",)
)
_SIZES = [
    {"file_id": "small", "width": 90, "height": 90, "file_size": 1_000},
    {"file_id": "huge", "width": 4000, "height": 4000, "file_size": ATTACHMENT_MAX_BYTES + 1},
    {"file_id": "big", "width": 1280, "height": 1280, "file_size": 200_000},
]


def _photo(update_id: int, *, chat: str = "42", caption: str | None = None) -> dict[str, object]:
    message: dict[str, object] = {"chat": {"id": chat}, "from": {"id": chat}, "photo": _SIZES}
    if caption is not None:
        message["caption"] = caption
    return {"update_id": update_id, "message": message}


def test_a_photo_is_admitted_like_text_and_its_caption_is_the_text() -> None:
    routed = normalize_inbound(_photo(1, caption="what is this?"), _CONFIG)
    assert routed.message is not None and routed.message.text == "what is this?"
    bare = normalize_inbound(_photo(2), _CONFIG)
    assert bare.message is not None and bare.message.text == ""
    stranger = normalize_inbound(_photo(3, chat="99"), _CONFIG)
    assert stranger.rejected_reason == "channel.reject.identity_not_allowlisted"
    sticker: dict[str, object] = {
        "message": {"chat": {"id": "42"}, "from": {"id": "42"}, "sticker": {}}
    }
    assert normalize_inbound(sticker, _CONFIG).rejected_reason == "channel.reject.malformed_payload"


def test_the_largest_size_within_the_cap_is_chosen() -> None:
    assert image_file_id(_photo(1)["message"], ATTACHMENT_MAX_BYTES) == "big"  # type: ignore[arg-type]
    image_doc = {"document": {"file_id": "d1", "mime_type": "image/png", "file_size": 10}}
    assert image_file_id(image_doc, ATTACHMENT_MAX_BYTES) == "d1"
    pdf = {"document": {"file_id": "d2", "mime_type": "application/pdf", "file_size": 10}}
    assert image_file_id(pdf, ATTACHMENT_MAX_BYTES) is None
    too_big = {"document": {"file_id": "d3", "mime_type": "image/png", "file_size": 10**9}}
    assert image_file_id(too_big, ATTACHMENT_MAX_BYTES) is None
    assert image_file_id({"text": "hi"}, ATTACHMENT_MAX_BYTES) is None


def _poller(harness: _TelegramHarness, fetch_file: object) -> object:
    from skep.supervisor.serve.channels.runtime import TelegramPoller

    return TelegramPoller(
        harness.engine,
        web_ui_url="http://ui.test/",
        fetch=harness._fetch,
        send=harness._send,
        answer=harness._answer,
        fetch_file=fetch_file,  # type: ignore[arg-type]
        spawn=lambda work: work(),  # v130-F10: turns inline in the suite
    )


def test_a_photo_without_a_caption_reaches_the_model(config: SupervisorConfig) -> None:
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama)
        harness = _TelegramHarness(config)
        fetched: list[tuple[str, str]] = []

        def fetch_file(token: str, file_id: str) -> bytes | None:
            fetched.append((token, file_id))
            return PNG

        try:
            ollama.script_reply("a cat")
            harness.updates.append([_photo(1)])
            assert _poller(harness, fetch_file).poll_once() == 1  # type: ignore[attr-defined]
            assert fetched == [("bot-token", "big")]
            assert [s[2] for s in harness.sent] == ["a cat"]
            binding = harness.store.channel_session("telegram:42")
            assert binding is not None
            (user, _reply) = harness.store.chat_messages(binding.chat_id)
            assert user.content == "" and len(user.attachments or []) == 1
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_a_failed_fetch_keeps_the_caption_and_never_logs_the_token(
    config: SupervisorConfig, caplog: object
) -> None:
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama)
        harness = _TelegramHarness(config)

        def fetch_file(token: str, file_id: str) -> bytes | None:
            raise ConnectionError(f"https://api.telegram.org/file/bot{token}/photos/x.jpg")

        try:
            ollama.script_reply("no image, but I read your words")
            harness.updates.append([_photo(1, caption="look"), _photo(2)])
            with caplog.at_level(logging.DEBUG, logger="skep.serve"):  # type: ignore[attr-defined]
                assert _poller(harness, fetch_file).poll_once() == 1  # type: ignore[attr-defined]
            assert [s[2] for s in harness.sent] == ["no image, but I read your words"]
            text = caplog.text  # type: ignore[attr-defined]
            assert "telegram photo fetch failed" in text
            assert "bot-token" not in text
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_the_default_fetch_reads_get_file_then_the_file(monkeypatch: object) -> None:
    import httpx

    from skep.supervisor.serve.channels import runtime

    urls: list[str] = []

    class _Resp:
        def __init__(self, status: int, payload: object = None, content: bytes = b"") -> None:
            self.status_code = status
            self._payload = payload
            self.content = content

        def json(self) -> object:
            return self._payload

    def get(url: str, **kwargs: object) -> _Resp:
        urls.append(url)
        if url.endswith("/getFile"):
            return _Resp(200, {"ok": True, "result": {"file_path": "photos/a.png"}})
        return _Resp(200, content=PNG)

    monkeypatch.setattr(runtime.httpx, "get", get)  # type: ignore[attr-defined]
    assert runtime._default_fetch_telegram_file("tok", "f1") == PNG
    assert urls == [
        "https://api.telegram.org/bottok/getFile",
        "https://api.telegram.org/file/bottok/photos/a.png",
    ]

    def down(url: str, **kwargs: object) -> _Resp:
        raise httpx.ConnectError(f"cannot reach {url}")

    monkeypatch.setattr(runtime.httpx, "get", down)  # type: ignore[attr-defined]
    assert runtime._default_fetch_telegram_file("tok", "f1") is None

    def no_path(url: str, **kwargs: object) -> _Resp:
        return _Resp(200, {"ok": True, "result": {}})

    monkeypatch.setattr(runtime.httpx, "get", no_path)  # type: ignore[attr-defined]
    assert runtime._default_fetch_telegram_file("tok", "f1") is None

    def oversize(url: str, **kwargs: object) -> _Resp:
        if url.endswith("/getFile"):
            return _Resp(200, {"ok": True, "result": {"file_path": "p"}})
        return _Resp(200, content=b"x" * (ATTACHMENT_MAX_BYTES + 1))

    monkeypatch.setattr(runtime.httpx, "get", oversize)  # type: ignore[attr-defined]
    assert runtime._default_fetch_telegram_file("tok", "f1") is None
