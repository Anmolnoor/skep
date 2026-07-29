"""v58-F4: transient provider failures are retried, not surfaced.

A dropped connection or 5xx/429 gets CHAT_STREAM_ATTEMPTS tries before the
turn gives up; 4xx fails fast; a stream that already yielded is never
restarted (re-streaming would duplicate what the user saw).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

import skep.supervisor.serve.chat as chat_module
from skep.supervisor.serve.chat import CHAT_STREAM_ATTEMPTS, chat_stream_with_retry
from skep.supervisor.serve.llm import OllamaError


def _reply(text: str) -> dict[str, Any]:
    return {"message": {"content": text}}


def test_connection_failures_get_three_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def flaky(base_url: str, api_key: str | None, **kwargs: Any) -> Iterator[dict[str, Any]]:
        calls["n"] += 1
        if calls["n"] < CHAT_STREAM_ATTEMPTS:
            raise OllamaError("All connection attempts failed")
        return iter([_reply("pong")])

    monkeypatch.setattr(chat_module, "chat_stream", flaky)
    sleeps: list[float] = []
    out = list(chat_stream_with_retry("http://x", None, sleep=sleeps.append))
    assert [c["message"]["content"] for c in out] == ["pong"]
    assert calls["n"] == CHAT_STREAM_ATTEMPTS
    assert len(sleeps) == CHAT_STREAM_ATTEMPTS - 1


def test_exhausted_attempts_surface_the_error(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def dead(base_url: str, api_key: str | None, **kwargs: Any) -> Iterator[dict[str, Any]]:
        calls["n"] += 1
        raise OllamaError("connection refused")

    monkeypatch.setattr(chat_module, "chat_stream", dead)
    with pytest.raises(OllamaError):
        list(chat_stream_with_retry("http://x", None, sleep=lambda _s: None))
    assert calls["n"] == CHAT_STREAM_ATTEMPTS


def test_permanent_http_errors_fail_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def unauthorized(base_url: str, api_key: str | None, **kwargs: Any) -> Iterator[dict[str, Any]]:
        calls["n"] += 1
        raise OllamaError("401 from http://x")

    monkeypatch.setattr(chat_module, "chat_stream", unauthorized)
    sleeps: list[float] = []
    with pytest.raises(OllamaError):
        list(chat_stream_with_retry("http://x", None, sleep=sleeps.append))
    assert calls["n"] == 1  # a bad key never fixes itself
    assert sleeps == []
    # 5xx and 429 ARE transient — they ride the retry path.
    assert chat_module._transient_provider_error(OllamaError("503 from http://x"))
    assert chat_module._transient_provider_error(OllamaError("429 from http://x"))
    assert not chat_module._transient_provider_error(OllamaError("404 from http://x"))


def test_midstream_failure_is_never_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def cut_off(base_url: str, api_key: str | None, **kwargs: Any) -> Iterator[dict[str, Any]]:
        calls["n"] += 1
        yield _reply("half a rep")
        raise OllamaError("connection reset mid-stream")

    monkeypatch.setattr(chat_module, "chat_stream", cut_off)
    received: list[dict[str, Any]] = []
    with pytest.raises(OllamaError):
        for chunk in chat_stream_with_retry("http://x", None, sleep=lambda _s: None):
            received.append(chunk)
    assert calls["n"] == 1  # restarting would duplicate the half-reply
    assert [c["message"]["content"] for c in received] == ["half a rep"]
