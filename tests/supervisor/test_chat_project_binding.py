"""v96-F2: a chat knows its project — the composer selector's server side.

The binding column is v56-F4's chats.project_id; this round makes it
operator-writable (PUT, UI-only — the Queen has no tool for it, I6), returns
it shaped for the strip on the chat GET, and rides one context line in the
pinned prompt so the Queen defaults repo/project args to it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.serve.chat import chat_project_line, chat_project_view

from .fake_ollama import FakeOllama
from .test_serve_chat_tools import chat_client

STATIC_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "skep" / "supervisor" / "serve" / "static"
)


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


def _seed_project(config: SupervisorConfig, repo: Path, project_id: str = "cockpit") -> None:
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id=project_id,
            name="cockpit project",
            strategy="trusted_local_dev",
            phase="build",
            policy={"coding_engine": "builtin", "verify_command": "uv run pytest"},
        )
        store.add_project_binding(
            project_id=project_id, binding_kind="repo_path", binding_value=str(repo)
        )
    finally:
        store.close()


def test_put_binds_validates_and_clears(
    config: SupervisorConfig, ollama: FakeOllama, repo: Path
) -> None:
    _seed_project(config, repo)
    client, chat_id = chat_client(config, ollama)

    # Unknown project refuses naming the known ones (I9).
    missing = client.put(f"/api/chats/{chat_id}/project", json={"project_id": "nope"})
    assert missing.status_code == 404
    assert "cockpit" in missing.json()["detail"]

    bound = client.put(f"/api/chats/{chat_id}/project", json={"project_id": "cockpit"})
    assert bound.status_code == 200
    view = bound.json()["project"]
    assert view["project_id"] == "cockpit"
    assert view["phase"] == "build"
    assert view["coding_engine"] == "builtin"
    assert view["repo"] == str(repo)

    # The chat GET carries the same view — the strip reads server truth (I8).
    detail = client.get(f"/api/chats/{chat_id}").json()
    assert detail["project"]["project_id"] == "cockpit"
    assert detail["chat"]["project_id"] == "cockpit"

    cleared = client.put(f"/api/chats/{chat_id}/project", json={"project_id": None})
    assert cleared.status_code == 200
    assert cleared.json()["project"] is None
    assert client.get(f"/api/chats/{chat_id}").json()["project"] is None

    unknown_chat = client.put("/api/chats/no-such-chat/project", json={"project_id": "cockpit"})
    assert unknown_chat.status_code == 404


def test_prompt_carries_the_project_line_iff_bound(
    config: SupervisorConfig, ollama: FakeOllama, repo: Path
) -> None:
    _seed_project(config, repo)
    client, chat_id = chat_client(config, ollama)

    ollama.script_reply("hello")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"})
    unbound_system = ollama.chat_bodies()[-1]["messages"][0]["content"]
    assert "Working project for this chat" not in unbound_system

    assert (
        client.put(f"/api/chats/{chat_id}/project", json={"project_id": "cockpit"}).status_code
        == 200
    )
    ollama.script_reply("again")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi again"})
    system = ollama.chat_bodies()[-1]["messages"][0]["content"]
    assert "Working project for this chat" in system
    assert "cockpit project (cockpit)" in system
    assert "engine builtin" in system
    assert str(repo) in system


def test_view_survives_a_deleted_project(config: SupervisorConfig, repo: Path) -> None:
    """A binding pointing at a removed project renders as unbound, never a
    stale lie (I8) — and the prompt line simply disappears."""
    store = RunStore(config.db_path)
    try:
        assert chat_project_view(store, "ghost") is None
        assert chat_project_line(store, "ghost") == ""
        assert chat_project_view(store, None) is None
    finally:
        store.close()


def test_deck_defaults_repo_to_the_chat_binding() -> None:
    """/policy and /state without an arg read the bound repo first; the
    localStorage guess is the fallback, not the authority."""
    app_js = (STATIC_DIR / "app.js").read_text()
    assert "chatBoundRepo = detail?.project?.repo || null;" in app_js
    assert "args[0] || chatBoundRepo" in app_js
