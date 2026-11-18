"""v126-F11: template schedules keep their time, and their ticks carry the
creating chat's taint.

The chat, CLI and REST faces called ``make_template_schedule`` without
``start_at`` (REST accepted it and threw it away) and never threaded
``once``: "every morning at 8, run skill X" fired at creation time and a
one-shot repeated forever. A tick dispatched with no ``chat_taint``, so the
card showed the taint once and every later run looked clean.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from skep.cli import main
from skep.supervisor import SupervisorConfig
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.scheduler import make_template_schedule, run_due
from skep.supervisor.store import RunStore
from skep.supervisor.templates import WorkflowTemplate

from .conftest import serve_client as _client
from .fake_ollama import FakeOllama
from .test_serve_chat import sse_events

EIGHT = "2030-01-01T08:00:00Z"


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


def _template() -> WorkflowTemplate:
    return WorkflowTemplate(name="morning", worker_kind="coding", instructions="tidy the docs")


def _fake_dispatch(seen: list[dict[str, Any]]) -> Any:
    def dispatch(repo: Path, instructions: str, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return SimpleNamespace(record=SimpleNamespace(task_id=f"t{len(seen)}", state="completed"))

    return dispatch


def test_a_template_schedule_for_eight_fires_at_eight_and_once_fires_once(
    tmp_path: Path, config: SupervisorConfig
) -> None:
    store = RunStore(config.db_path)
    try:
        store.add_template(_template())
        store.add_schedule(
            make_template_schedule(
                name="m", template=_template(), params={}, repo=tmp_path,
                interval_seconds=86400, start_at=EIGHT, once=True,
            )
        )  # fmt: skip
        seen: list[dict[str, Any]] = []
        dispatch = _fake_dispatch(seen)
        assert (
            run_due(store=store, config=config, now="2030-01-01T07:59:00Z", dispatch=dispatch) == []
        )
        assert len(run_due(store=store, config=config, now=EIGHT, dispatch=dispatch)) == 1
        later = "2030-01-03T08:00:00Z"
        assert run_due(store=store, config=config, now=later, dispatch=dispatch) == []
        assert len(seen) == 1
        fired = store.get_schedule("m")
        assert fired is not None and fired.once is True and fired.enabled is False
    finally:
        store.close()


def test_a_tick_carries_the_creating_chats_current_taint(
    tmp_path: Path, config: SupervisorConfig
) -> None:
    store = RunStore(config.db_path)
    try:
        store.add_template(_template())
        chat = store.create_chat(title="t", model=None)
        schedule = make_template_schedule(
            name="m", template=_template(), params={}, repo=tmp_path, interval_seconds=60,
            start_at=EIGHT, chat_id=chat.chat_id,
        )  # fmt: skip
        store.add_schedule(schedule)
        seen: list[dict[str, Any]] = []
        dispatch = _fake_dispatch(seen)
        run_due(store=store, config=config, now=EIGHT, dispatch=dispatch)
        assert "chat_taint" not in (seen[0]["dispatch_decision"].constraints or {})
        # The chat read outside content after it created the schedule: the
        # next tick says so, as a live dispatch from that chat would.
        message = store.add_chat_message(chat.chat_id, role="tool", content="x", tool_name="t")
        store.add_chat_taint(chat.chat_id, untrusted=message)
        run_due(store=store, config=config, now="2030-01-01T08:01:00Z", dispatch=dispatch)
        assert seen[1]["dispatch_decision"].constraints["chat_taint"] == ["untrusted"]
    finally:
        store.close()


def test_rest_threads_start_at_and_once_for_a_template_schedule(
    repo: Path, config: SupervisorConfig
) -> None:
    client = _client(config)
    store = RunStore(config.db_path)
    try:
        store.add_template(_template())
    finally:
        store.close()
    made = client.post(
        "/api/schedules",
        json={"name": "m", "repo": str(repo), "template": "morning", "every": "1d",
              "start_at": EIGHT, "once": True},
    )  # fmt: skip
    assert made.status_code in (200, 201), made.text
    assert made.json()["next_run_at"] == EIGHT
    assert made.json()["once"] is True


def test_the_cli_threads_start_at_and_once_for_a_template_schedule(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    store = RunStore(build_config(home, None).db_path)
    try:
        store.add_template(_template())
    finally:
        store.close()
    argv = ["--home", str(home), "schedule", "add", "m", str(repo), "--template", "morning",
            "--every", "1d", "--start-at", EIGHT, "--once"]  # fmt: skip
    assert main(argv) == 0
    assert f"next run: {EIGHT}" in capsys.readouterr().out
    store = RunStore(build_config(home, None).db_path)
    try:
        made = store.get_schedule("m")
    finally:
        store.close()
    assert made is not None and made.once is True and made.next_run_at == EIGHT


def test_chat_threads_start_at_once_and_its_own_id_for_a_template_schedule(
    repo: Path, config: SupervisorConfig, ollama: FakeOllama
) -> None:
    from .test_serve_chat_tools import chat_client

    client, chat_id = chat_client(config, ollama)
    store = RunStore(config.db_path)
    try:
        store.add_template(_template())
    finally:
        store.close()
    ollama.script_tool_call(
        "propose_schedule",
        {"name": "m", "repo": str(repo), "every": "1d", "template": "morning",
         "start_at": EIGHT, "once": True},
    )  # fmt: skip
    sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "at 8"}).text)
    action_id = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]
    ollama.script_reply("scheduled")
    client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm")
    store = RunStore(config.db_path)
    try:
        made = store.get_schedule("m")
    finally:
        store.close()
    assert made is not None
    assert (made.next_run_at, made.once, made.chat_id) == (EIGHT, True, chat_id)
