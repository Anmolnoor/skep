"""v134-F19: a Telegram connection error is one warning line plus a backoff.

The live test's log filled with 40-line tracebacks: every failed long-poll
ran ``logger.exception`` and retried on the fixed 2 s idle. A transport
error now logs one redacted WARNING and doubles the wait (capped at 60 s,
reset by a good poll); anything else still logs its traceback.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import httpx

from skep.supervisor.serve.channels.runtime import TelegramPoller


class _Stop:
    """Records each wait; reports "stopped" once the script runs out."""

    def __init__(self, script: list[Any]) -> None:
        self.script = script
        self.waits: list[float] = []

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        return not self.script

    def set(self) -> None:
        self.script.clear()


def _poller(script: list[Any]) -> tuple[TelegramPoller, _Stop]:
    poller = TelegramPoller(
        SimpleNamespace(store=None, home=None),  # type: ignore[arg-type]
        web_ui_url="http://ui.test/",
        idle_seconds=1.0,
    )
    stop = _Stop(script)

    def poll_once() -> int:
        step = script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return int(step)

    poller._stop_event = stop  # type: ignore[assignment]
    poller.poll_once = poll_once  # type: ignore[method-assign]
    return poller, stop


def _status_error() -> httpx.HTTPStatusError:
    token = "123:" + "b" * 12  # a token-shaped path segment, built at runtime
    request = httpx.Request("GET", f"https://api.telegram.org/bot{token}/getUpdates")
    response = httpx.Response(502, request=request)
    return httpx.HTTPStatusError(
        f"Server error '502 Bad Gateway' for url '{request.url}'",
        request=request,
        response=response,
    )


def test_transport_errors_back_off_and_a_good_poll_resets(caplog: Any) -> None:
    script: list[Any] = [
        httpx.ConnectError("nodename nor servname provided"),
        httpx.ConnectError("nodename nor servname provided"),
        httpx.ReadTimeout("timed out"),
        0,
        _status_error(),
        ValueError("a bug"),
    ]
    poller, stop = _poller(script)
    with caplog.at_level(logging.WARNING, logger="skep.serve"):
        poller.run()
    assert stop.waits == [1.0, 2.0, 4.0, 8.0, 1.0, 2.0, 4.0]
    records = caplog.records
    transport = [r for r in records if r.exc_info is None]
    assert len(transport) == 4  # one line per transport error, no traceback
    assert all(r.levelno == logging.WARNING for r in transport)
    assert "ConnectError" in transport[0].getMessage()
    assert "telegram" in transport[0].getMessage()
    assert "HTTPStatusError" in transport[3].getMessage()
    assert all("b" * 12 not in r.getMessage() for r in records)  # token redacted
    (unexpected,) = [r for r in records if r.exc_info is not None]
    assert unexpected.exc_info[0] is ValueError  # a bug keeps its traceback


def test_backoff_is_capped_at_sixty_seconds() -> None:
    poller, stop = _poller([httpx.ConnectError("down") for _ in range(10)])
    poller.run()
    assert max(stop.waits) == 60.0
    assert stop.waits[-3:] == [60.0, 60.0, 60.0]
