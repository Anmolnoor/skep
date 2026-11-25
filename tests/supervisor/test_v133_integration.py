"""v133 integration: fixes that cross the builders' packages (Sol round 1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from skep.supervisor import SupervisorConfig

from .conftest import serve_client
from .test_v133_record_landing import _run_with_g10, _store


def test_an_operator_land_card_is_bound_to_the_g10_record_it_was_drawn_on(
    config: SupervisorConfig, repo: Path, tmp_path: Path
) -> None:
    """S3 (J1, I7): a typed /land records the evidence it showed, like the
    Queen's card, so a re-verification that changes before the click is seen."""
    client = serve_client(config)
    store = _store(config)
    try:
        task_id = _run_with_g10(
            store, repo, tmp_path, outcome="failed", confirmed=False, exit_codes=[1]
        )
        chat_id = store.create_chat(title="t", model=None).chat_id
        base = f"/api/chats/{chat_id}/commands"
        made = client.post(base, json={"tool": "land_run", "args": {"task_id": task_id}})
        assert made.status_code == 201, made.text
        action = store.get_chat_action(made.json()["action_id"])
        assert action is not None
        card = action.card or {}
        assert task_id in card["g10_bound"] and card["evidence"]["runs"]
        assert card["action_label"].startswith("Land despite")

        store.record_reverification(
            task_id, outcome="unavailable", worker_outcome="passed", confirmed=False,
            commands=["cd web && node smoke.cjs"], exit_codes=[], detail="npm ci failed",
        )  # fmt: skip
        refused = client.post(f"{base}/{action.action_id}/confirm?surface=web")
        result = refused.json()  # a command reports its refusal in the result
        assert result["ok"] is False, result
        assert "changed since this card was shown" in result["error"]
        assert all(a.status != "approved" for a in store.approvals_for(task_id))
    finally:
        store.close()


def test_the_run_page_and_forensics_say_when_g10_ran_a_syntax_only_check(
    config: SupervisorConfig, repo: Path, tmp_path: Path
) -> None:
    """F-I1 (I2, I8): a re-verification that passed a syntax-only pin says so
    wherever the verdict is read; a real check carries no such line."""
    client = serve_client(config)
    store = _store(config)
    try:
        task_id = _run_with_g10(
            store, repo, tmp_path, outcome="failed", confirmed=False, exit_codes=[1]
        )
        real = client.get(f"/api/runs/{task_id}").json()["reverification"]
        assert real["syntax_only"] is None
        store.record_reverification(
            task_id, outcome="passed", worker_outcome="passed", confirmed=True,
            commands=["node --check index.js"], exit_codes=[0],
            detail="passed [command from the project's pinned verify_command]",
        )  # fmt: skip
        page = client.get(f"/api/runs/{task_id}").json()["reverification"]
        report = client.get(f"/api/runs/{task_id}/forensics").json()["outcome"]["g10"]
        assert "syntax-only" in page["syntax_only"] and "syntax-only" in report["syntax_only"]
    finally:
        store.close()


def test_a_slack_run_summary_takes_its_mark_from_g10(config: SupervisorConfig) -> None:
    """F13 (J2): the rich Slack summary of a run G10 did not confirm is not green."""
    from skep.supervisor import mint_task
    from skep.supervisor.serve.channels import ChannelConfig, store_channel_secret
    from skep.supervisor.serve.channels.outbound import push_to_chat_channel

    store = _store(config)
    try:
        chat = store.create_chat(title="slack C1", model=None)
        store.bind_channel_session(
            session_key="slack:C1", channel="slack", identity_id="C1", chat_id=chat.chat_id
        )
        store.upsert_channel_config(ChannelConfig(channel="slack", enabled=True))
        store_channel_secret(config.home, "slack", "xoxb-fake")
        task = mint_task(workspace=config.home / "ws", instructions="x")
        store.create_run(task, repo=config.home, ref=None, execution_mode="sandbox")
        store.transition(task.task_id, "completed", None)
        store.record_reverification(
            task.task_id, outcome="failed", worker_outcome="passed", confirmed=False,
            commands=["npm test"], exit_codes=[1], detail="boom",
        )  # fmt: skip
        sends: list[list[dict[str, object]]] = []

        def send(
            token: str,
            channel_id: str,
            blocks: list[dict[str, object]],
            thread_ts: str | None = None,
        ) -> bool:
            sends.append(blocks)
            return True

        assert push_to_chat_channel(
            store, config.home, chat.chat_id, "run done", run_ref=task.task_id,
            web_ui_url="http://ui.test/", send_slack=send,
        )  # fmt: skip
        assert "🟢" not in str(sends[0])
    finally:
        store.close()


def test_the_terminal_chat_says_which_surface_answered_a_model_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F37 (J3): skep chat sends the surface hint on a model card's verdict,
    as it already does on a command card's."""
    from contextlib import contextmanager
    from typing import Any, cast

    from skep import cli_chat

    paths: list[str] = []

    class Client:
        @contextmanager
        def stream(self, _method: str, path: str, _body: object = None) -> Any:
            paths.append(path)
            yield object()

    monkeypatch.setattr(cli_chat, "iter_sse", lambda _response: iter(()))
    monkeypatch.setattr(cli_chat, "render_turn", lambda *_a, **_k: ("done", []))
    repl = cli_chat.ChatRepl(cast(Any, Client()), {"chat_id": "c1"}, show_thinking=False)
    repl._apply_card_verb({"action_id": "a1", "source": "assistant"}, "deny")
    assert paths == ["/api/chats/c1/actions/a1/deny?surface=cli"]


def test_diagnose_run_itself_refuses_with_the_sandbox_off(
    config: SupervisorConfig, repo: Path, tmp_path: Path
) -> None:
    """F29 (I12): the action refuses, so the REST face cannot run a command
    on a worker's kept tree unconfined either."""
    import dataclasses

    from skep.supervisor import mint_task
    from skep.supervisor.serve import actions

    off = dataclasses.replace(config, sandbox=False)
    store = _store(off)
    try:
        kept = tmp_path / "kept"
        kept.mkdir()
        task = mint_task(workspace=kept, instructions="x")
        store.create_run(task, repo=repo, ref=None, execution_mode="sandbox")
        with pytest.raises(ValueError, match="sandbox is off"):
            actions.diagnose_run(store, off, task.task_id, command="touch escaped")
        assert not (kept / "escaped").exists()
    finally:
        store.close()
