"""v134: the 2.1.0 live-test round (plan: skep-design plans/v134/README.md).

F1 (S3, I8) a turn that raises anything still ends with a stored reply.
F2 (S2) skep's own secrets never enter a chat; titles never carry a credential.
F3 (D1) the worktree TTL sweep never counts a tree that is already gone.
F4 (S1, I2 I8) a landing card never shows a syntax-only G10 pass as clean.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.serve.chat import ChatEngine
from skep.supervisor.serve.jobs import Dispatcher
from skep.supervisor.serve.settings import ConfigHolder

from .conftest import serve_client
from .fake_ollama import FakeOllama
from .test_serve_chat import sse_events


@dataclass
class Rig:
    client: TestClient
    store: RunStore
    ollama: FakeOllama
    config: SupervisorConfig


@pytest.fixture()
def rig(config: SupervisorConfig) -> Iterator[Rig]:
    ollama = FakeOllama(api_key="sk-fake").start()
    client = serve_client(config)
    client.put(
        "/api/llm/config",
        json={"base_url": ollama.base_url, "default_model": "qwen3", "api_key": "sk-fake"},
    )
    store = RunStore(config.db_path)
    yield Rig(client, store, ollama, config)
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


# ---------- F2 (S2): skep's own secrets never enter a chat ----------


def test_a_message_carrying_the_serve_token_is_refused_and_stored_nowhere(rig: Rig) -> None:
    token = (rig.config.home / "serve-token").read_text(encoding="utf-8").strip()
    chat_id = rig.store.create_chat(title="New chat", model=None).chat_id

    response = rig.client.post(f"/api/chats/{chat_id}/messages", json={"content": token})

    assert response.status_code == 400
    assert "serve-token" in response.json()["detail"]
    assert rig.store.chat_messages(chat_id) == []
    chat = rig.store.get_chat(chat_id)
    assert chat is not None and token not in chat.title
    assert rig.ollama.chat_bodies() == []


def test_a_channel_message_carrying_a_channel_secret_is_refused(rig: Rig) -> None:
    from skep.supervisor.serve.channels import ChannelIdentity, ChannelMessage
    from skep.supervisor.serve.channels.runtime import run_channel_turn

    secret = "tg-" + "x7Kq" * 10
    (rig.config.home / "telegram-secret").write_text(secret, encoding="utf-8")
    engine = ChatEngine(
        store=rig.store,
        holder=ConfigHolder(rig.config, rig.store),
        runner=Dispatcher(store=rig.store, holder=ConfigHolder(rig.config, rig.store)),
        home=rig.config.home,
    )
    message = ChannelMessage(
        channel="telegram",
        identity=ChannelIdentity(channel="telegram", identity_id="u1"),
        text=f"here it is {secret}",
        session_key="telegram:t1",
    )

    reply, cards = run_channel_turn(engine, message, web_ui_url="http://x")

    assert "telegram-secret" in reply and cards == []
    assert all(
        secret not in m.content
        for c in rig.store.list_chats()
        for m in rig.store.chat_messages(c.chat_id)
    )


def test_a_serve_env_value_counts_as_a_skep_secret(tmp_path: Path) -> None:
    from skep.supervisor.secret_floor import own_secret_in

    home = tmp_path / "supervisor"
    home.mkdir()
    (home / "llm-secret").mkdir()  # unreadable as text: skipped, not a crash
    secret = "c" * 20  # built here: a literal assignment trips the secret scanners
    (tmp_path / "serve.env").write_text(
        f"# comment\nexport CLAUDE_CODE_OAUTH_TOKEN='{secret}'\nSHORT=abc\n", encoding="utf-8"
    )
    assert own_secret_in(f"use {secret} now", home) == ("serve.env CLAUDE_CODE_OAUTH_TOKEN")
    assert own_secret_in("abc is not a secret, it is too short", home) is None


def test_a_title_never_carries_a_credential_shaped_word() -> None:
    from skep.supervisor.serve.chat import DEFAULT_TITLE, _title_from

    assert _title_from("Zq9Xr2Lm7Tb4Vw8Nc3Hp6Ks1Dy5Fg0Ju_Ae7Bo4Ci9W") == DEFAULT_TITLE
    assert _title_from("use key sk-abcdefghijkl1234 please") == "use key *** please"
    assert _title_from("fix the README typo") == "fix the README typo"


# ---------- F3 (D1): the sweep never "removes" a tree that is gone ----------


def test_the_ttl_sweep_skips_a_workspace_that_no_longer_exists(
    repo: Path, config: SupervisorConfig
) -> None:
    """Live test: a resume reused its parent's workspace, the tree was gone,
    and the sweep logged "swept 1" every 30 s for days."""
    import shutil

    from skep.supervisor.dispatch import sweep_expired_preserved_worktrees

    from .test_recover import _strand_run

    store = RunStore(config.db_path)
    try:
        task_id, workspace = _strand_run(store, config, repo)
        store.transition(task_id, "failed", "agent exited 1")
        store._conn.execute(
            "UPDATE runs SET updated_at = '2020-01-01T00:00:00Z' WHERE task_id = ?", (task_id,)
        )
        store._conn.commit()
        shutil.rmtree(workspace)

        assert sweep_expired_preserved_worktrees(store, config) == 0
    finally:
        store.close()


# ---------- F4 (S1): a syntax-only G10 pass is never shown as clean ----------


def _passed_on(store: RunStore, repo: Path, tmp_path: Path, command: str) -> str:
    from skep.supervisor.contracts_io import mint_task

    task = mint_task(workspace=repo, instructions="land me")
    store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
    store.transition(task.task_id, "completed")
    patch = tmp_path / f"{task.task_id}.patch"
    patch.write_text(
        "diff --git a/.gitignore b/.gitignore\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/.gitignore\n@@ -0,0 +1 @@\n+node_modules\n",
        encoding="utf-8",
    )
    store.add_artifact(task.task_id, kind="patch", audit_path=patch, sha256="0" * 64)
    store.record_reverification(
        task.task_id, outcome="passed", worker_outcome="passed", confirmed=True,
        commands=[command], exit_codes=[0],
        detail="ok [command from the project's pinned verify_command]",
    )  # fmt: skip
    return str(task.task_id)


def test_a_syntax_only_pass_on_untouched_files_is_marked_on_the_card(
    config: SupervisorConfig, repo: Path, tmp_path: Path
) -> None:
    from skep.supervisor.serve.actions import reverification_summary
    from skep.supervisor.serve.console import run_evidence

    store = RunStore(config.db_path)
    try:
        weak = _passed_on(store, repo, tmp_path, "node --check farm/index.js")
        [run] = run_evidence(store, "land_run", {"task_id": weak})
        assert "syntax-only" in run["g10_weak"]
        assert "none of the files this patch changes" in run["g10_weak"]
        assert run["supervisor"].startswith("G10 passed (syntax-only): ")
        summary = reverification_summary(store.reverification_for(weak)) or {}
        assert "syntax-only" in summary["syntax_only"]

        real = _passed_on(store, repo, tmp_path, "npm test")
        [run] = run_evidence(store, "land_run", {"task_id": real})
        assert run["g10_weak"] is None and run["supervisor"].startswith("G10 passed: ")
    finally:
        store.close()
