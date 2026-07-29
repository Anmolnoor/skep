"""The Ollama-backed coding worker — the first-party LLM planner pointed at
Ollama, running with the SAME saved assistant credentials by default."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

from skep.supervisor import RunStore
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.dispatch import run_task
from skep.supervisor.serve.llm import (
    LLM_BASE_URL,
    LLM_DEFAULT_MODEL,
    LLM_PROTOCOL,
    store_api_key,
)
from skep.worker_contract import Permissions

from .fake_ollama import FakeOllama

_OLLAMA_WORKER = shlex.join([sys.executable, "-m", "skep.workers.ollama"])

_PLAN = json.dumps(
    {
        "summary": "created ollama-backed generated.py",
        "files": [{"path": "generated.py", "content": "print('ollama')\n"}],
        "verify": {"argv": [sys.executable, "generated.py"], "expected_stdout": "ollama\n"},
    }
)


def test_ollama_worker_uses_the_saved_assistant_ollama_config(repo: Path, tmp_path: Path) -> None:
    """Point worker_command at the Ollama worker; with the assistant configured
    for Ollama it runs against that endpoint, with the same 0600 credential."""
    config = build_config(tmp_path / "home", _OLLAMA_WORKER)
    assert config.worker_command == (sys.executable, "-m", "skep.workers.ollama")

    server = FakeOllama(api_key="sk-fake").start()
    store = RunStore(config.db_path)
    try:
        store.set_setting(LLM_BASE_URL, server.base_url)
        store.set_setting(LLM_DEFAULT_MODEL, "qwen2.5-coder")
        store.set_setting(LLM_PROTOCOL, "ollama")
    finally:
        store.close()
    store_api_key(config.home, "sk-fake")  # the shared daemon secret
    try:
        server.script_reply(_PLAN)
        outcome = run_task(
            repo,
            "Use the saved Ollama model to create generated.py.",
            config=config,
            permissions=Permissions(
                read=["workspace"],
                write=["workspace"],
                network=["127.0.0.1"],
                env_allowlist=[],
            ),
        )
    finally:
        server.stop()

    assert outcome.record.state == "completed"
    assert outcome.record.summary == "created ollama-backed generated.py"
    # It reached the Ollama endpoint, with the saved model + shared credential.
    assert server.chat_bodies()[0]["model"] == "qwen2.5-coder"
    assert server.requests[-1]["headers"]["Authorization"] == "Bearer sk-fake"
    # Landing is still the only commit — no leftover in the source repo.
    assert not (repo / "generated.py").exists()


def test_ollama_worker_honors_a_worker_specific_endpoint_override(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SKEP_OLLAMA_URL/MODEL point the WORKER at a different local Ollama than
    the chat, still authenticating with the shared daemon secret."""
    config = build_config(tmp_path / "home", _OLLAMA_WORKER)

    chat_server = FakeOllama(api_key="sk-fake").start()
    worker_server = FakeOllama(api_key="sk-fake").start()
    store = RunStore(config.db_path)
    try:
        # The assistant/chat is configured against one Ollama...
        store.set_setting(LLM_BASE_URL, chat_server.base_url)
        store.set_setting(LLM_DEFAULT_MODEL, "small-chat-model")
        store.set_setting(LLM_PROTOCOL, "ollama")
    finally:
        store.close()
    store_api_key(config.home, "sk-fake")
    # ...but the worker is pointed at a bigger local coding Ollama.
    monkeypatch.setenv("SKEP_OLLAMA_URL", worker_server.base_url)
    monkeypatch.setenv("SKEP_OLLAMA_MODEL", "qwen2.5-coder:32b")
    try:
        worker_server.script_reply(_PLAN)
        outcome = run_task(
            repo,
            "Create generated.py via the worker Ollama.",
            config=config,
            permissions=Permissions(
                read=["workspace"],
                write=["workspace"],
                network=["127.0.0.1"],
                # SKEP_OLLAMA_* must reach the worker process.
                env_allowlist=["SKEP_OLLAMA_URL", "SKEP_OLLAMA_MODEL"],
            ),
        )
    finally:
        chat_server.stop()
        worker_server.stop()

    assert outcome.record.state == "completed"
    # The WORKER Ollama answered with the worker model; the chat one was untouched.
    assert worker_server.chat_bodies()[0]["model"] == "qwen2.5-coder:32b"
    assert chat_server.chat_bodies() == []


def test_ollama_provider_defaults_to_the_saved_assistant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unit-level: with no override, the Ollama worker resolves the saved
    assistant provider — same endpoint, model, and credential."""
    from skep.workers.ollama.__main__ import _ollama_provider

    home = tmp_path / "home"
    config = build_config(home, None)
    store = RunStore(config.db_path)
    try:
        store.set_setting(LLM_BASE_URL, "http://127.0.0.1:11434")
        store.set_setting(LLM_DEFAULT_MODEL, "qwen2.5-coder")
        store.set_setting(LLM_PROTOCOL, "ollama")
    finally:
        store.close()
    store_api_key(config.home, "shared-secret")
    monkeypatch.setenv("SKEP_HOME", str(home.expanduser().resolve()))
    monkeypatch.delenv("SKEP_OLLAMA_URL", raising=False)

    provider = _ollama_provider()
    assert provider is not None
    assert provider.profile.name == "ollama"
    assert provider.profile.model == "qwen2.5-coder"
    assert provider.profile.endpoint == "http://127.0.0.1:11434"
    assert provider.api_key == "shared-secret"  # same credential as the assistant
