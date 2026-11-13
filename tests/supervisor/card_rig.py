"""A chat engine over a ``net_lane_grid`` home and a fake model — the P4-2
card tests' rig (the same shape as ``test_taint_v3.Live``). No byte leaves
the machine: the fetch, the search and the MCP servers are fakes."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from skep.supervisor import RunStore
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.serve.chat import ChatEngine
from skep.supervisor.serve.jobs import Dispatcher
from skep.supervisor.serve.llm import LLM_BASE_URL, LLM_DEFAULT_MODEL
from skep.supervisor.serve.settings import ConfigHolder

from .fake_ollama import FakeOllama
from .net_lane_grid import build_net_home, fakes

Event = tuple[Any, dict[str, Any]]


@dataclass
class Live:
    paths: dict[str, Path]
    store: RunStore
    holder: ConfigHolder
    engine: ChatEngine
    ollama: FakeOllama
    fetched: list[dict[str, Any]]
    searched: list[str]

    def chat(self, role: str | None = None) -> str:
        return self.store.create_chat(title="t", model=None, role=role).chat_id

    def _llm(self, chat_id: str) -> dict[str, Any]:
        base_url, api_key, model, protocol = self.engine.resolved_llm(self.store.get_chat(chat_id))
        return {"base_url": base_url, "api_key": api_key, "model": model, "protocol": protocol}

    def turn(self, chat_id: str, text: str, **kw: Any) -> list[Event]:
        self.store.add_chat_message(chat_id, role="user", content=text)
        return list(self.engine.turn_events(chat_id, **self._llm(chat_id), **kw))

    def verdict(self, chat_id: str, action_id: str, *, confirm: bool, **kw: Any) -> list[Event]:
        chat = self.store.get_chat(chat_id)
        return list(self.engine.verdict_events(chat_id, chat, action_id, confirm=confirm, **kw))


def cards(events: list[Event]) -> list[dict[str, Any]]:
    return [data for event, data in events if event == "action"]


@pytest.fixture(name="live")
def live_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[str], Live]]:
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.chdir(tmp_path)
    made: list[Live] = []
    searched: list[str] = []

    def search(query: str, **_: Any) -> list[dict[str, Any]]:
        searched.append(query)
        return [{"title": "t", "href": "https://result.example/", "body": "r"}]

    with fakes() as fetched, mock.patch("skep.supervisor.serve.websearch.search_web", search):

        def make(kind: str) -> Live:
            paths = build_net_home(tmp_path / kind, kind)
            config = build_config(paths["personal"], None)
            store = RunStore(config.db_path)
            holder = ConfigHolder(config, store)
            ollama = FakeOllama(api_key=None).start()
            store.set_setting(LLM_BASE_URL, ollama.base_url)
            store.set_setting(LLM_DEFAULT_MODEL, "qwen3")
            engine = ChatEngine(
                store=store, holder=holder, runner=Dispatcher(holder, store),
                home=holder.current.home,
            )  # fmt: skip
            made.append(Live(paths, store, holder, engine, ollama, fetched, searched))
            return made[-1]

        yield make
    for item in made:
        item.ollama.stop()
        item.store.close()
