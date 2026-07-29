"""v44-F9: image input in chat — sniffed, capped, stored, honestly degraded.

Attachments are validated by magic bytes (extensions are claims), capped at
5 MiB, stored under home/chat-attachments/<chat_id>/, and reach the model as
images ONLY when the configured model is vision-capable — otherwise the user
message names them and nothing surprises a text-only provider.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.serve.chat import (
    ATTACHMENT_MAX_BYTES,
    save_chat_attachment,
    sniff_image,
)
from skep.supervisor.serve.llm import _openai_image_message

from .fake_ollama import FakeOllama
from .test_serve_chat_tools import chat_client

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes"
JPG = b"\xff\xd8\xff\xe0" + b"fake"
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"fake"


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


def test_sniff_and_save_validate_by_magic_bytes(tmp_path: Path) -> None:
    assert sniff_image(PNG) == ("png", "image/png")
    assert sniff_image(JPG) == ("jpg", "image/jpeg")
    assert sniff_image(WEBP) == ("webp", "image/webp")
    assert sniff_image(b"<script>alert(1)</script>") is None

    name = save_chat_attachment(tmp_path, "chat-1", PNG)
    assert name.endswith(".png")
    assert (tmp_path / "chat-attachments" / "chat-1" / name).read_bytes() == PNG
    with pytest.raises(ValueError, match="not a supported image"):
        save_chat_attachment(tmp_path, "chat-1", b"plain text")
    with pytest.raises(ValueError, match="too large"):
        save_chat_attachment(tmp_path, "chat-1", PNG + b"0" * ATTACHMENT_MAX_BYTES)


def test_upload_serve_and_message_roundtrip(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    uploaded = client.post(f"/api/chats/{chat_id}/attachments", content=PNG)
    assert uploaded.status_code == 201
    name = uploaded.json()["name"]

    served = client.get(f"/api/chats/{chat_id}/attachments/{name}")
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/png"
    assert served.content == PNG
    # Name pattern is the traversal guard.
    assert client.get(f"/api/chats/{chat_id}/attachments/../../serve-token").status_code == 404
    assert client.post(
        f"/api/chats/{chat_id}/attachments", content=b"not an image"
    ).status_code == 400

    # A message referencing the attachment lands it on the stored row…
    ollama.script_reply("looking at it")
    posted = client.post(
        f"/api/chats/{chat_id}/messages", json={"content": "see this", "attachments": [name]}
    )
    assert posted.status_code == 200
    store = RunStore(config.db_path)
    try:
        user_rows = [m for m in store.chat_messages(chat_id) if m.role == "user"]
        assert user_rows[-1].attachments == [name]
    finally:
        store.close()
    # …and an unknown name is refused before anything is written.
    refused = client.post(
        f"/api/chats/{chat_id}/messages",
        json={"content": "x", "attachments": ["deadbeefdeadbeefdeadbeefdeadbeef.png"]},
    )
    assert refused.status_code == 400


def test_model_sees_placeholder_without_vision_and_images_with_it(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    name = client.post(f"/api/chats/{chat_id}/attachments", content=PNG).json()["name"]

    ollama.script_reply("noted")
    client.post(
        f"/api/chats/{chat_id}/messages", json={"content": "look", "attachments": [name]}
    )
    sent = ollama.chat_bodies()[-1]["messages"][-1]
    assert "images" not in sent  # vision off (default): no image payload
    assert f"[image attached: {name}]" in sent["content"]

    client.put("/api/llm/config", json={"vision": True})
    ollama.script_reply("I can see it")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "and now?"})
    history = ollama.chat_bodies()[-1]["messages"]
    with_images = [m for m in history if m.get("images")]
    assert with_images and isinstance(with_images[0]["images"][0], str)
    assert "[image attached" not in with_images[0]["content"]


def test_openai_shape_converts_images_to_typed_data_uris() -> None:
    import base64

    b64 = base64.b64encode(PNG).decode("ascii")
    converted = _openai_image_message({"role": "user", "content": "look", "images": [b64]})
    parts = converted["content"]
    assert parts[0] == {"type": "text", "text": "look"}
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    untouched = {"role": "assistant", "content": "hi"}
    assert _openai_image_message(untouched) is untouched
