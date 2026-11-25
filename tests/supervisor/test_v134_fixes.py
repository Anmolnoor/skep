"""v134: the 2.1.0 live-test round (plan: skep-design plans/v134/README.md).

F1 (S3, I8) a turn that raises anything still ends with a stored reply.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.serve.chat import ChatEngine

from .conftest import serve_client
from .fake_ollama import FakeOllama
from .test_serve_chat import sse_events


@dataclass
class Rig:
    client: TestClient
    store: RunStore
    ollama: FakeOllama


@pytest.fixture()
def rig(config: SupervisorConfig) -> Iterator[Rig]:
    ollama = FakeOllama(api_key="sk-fake").start()
    client = serve_client(config)
    client.put(
        "/api/llm/config",
        json={"base_url": ollama.base_url, "default_model": "qwen3", "api_key": "sk-fake"},
    )
    store = RunStore(config.db_path)
    yield Rig(client, store, ollama)
    store.close()
    ollama.stop()


# ---------- F1 (S3): an unexpected error never leaves the chat silent ----------


def test_a_turn_that_raises_stores_a_reply_and_ends_with_an_error(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self: ChatEngine, chat_id: str, off_menu: frozenset[str] = frozenset()) -> str:
        raise OSError(36, "File name too long")

    monkeypatch.setattr(ChatEngine, "_system_prompt", boom)
    chat_id = rig.store.create_chat(title="f1", model=None).chat_id

    response = rig.client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"})

    assert response.status_code == 200
    events = sse_events(response.text)
    assert any(event == "error" for event, _ in events)
    replies = [m for m in rig.store.chat_messages(chat_id) if m.role == "assistant"]
    assert replies and "OSError" in replies[-1].content


def test_a_too_long_worktree_word_is_dead_not_a_crash(tmp_path: Path) -> None:
    from skep.supervisor.shell_prefixes import references_dead_worktree

    (tmp_path / ".skep" / "worktrees").mkdir(parents=True)
    word = f"{tmp_path}/.skep/worktrees/{'a' * 300}/f"  # ENAMETOOLONG, not ENOENT
    assert references_dead_worktree(["cat", word])
