"""v134-F19: a Telegram connection error is one warning line plus a backoff.

The live test's log filled with 40-line tracebacks: every failed long-poll
ran ``logger.exception`` and retried on the fixed 2 s idle. A transport
error now logs one redacted WARNING and doubles the wait (capped at 60 s,
reset by a good poll); anything else still logs its traceback.
"""

from __future__ import annotations

import logging
import socket
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


_TOKEN = "123:" + "b" * 12  # a token-shaped path segment, built at runtime


def _status_error() -> httpx.HTTPStatusError:
    """A real 502: httpx's own text spans two lines (r1 review)."""
    request = httpx.Request("GET", f"https://api.telegram.org/bot{_TOKEN}/getUpdates")
    try:
        httpx.Response(502, request=request).raise_for_status()
    except httpx.HTTPStatusError as exc:
        assert "\n" in str(exc)  # the case the r1 fix is for
        return exc
    raise AssertionError("a 502 did not raise")


def _connect_error() -> httpx.ConnectError:
    """A real transport error: a loopback port nothing listens on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    try:
        httpx.get(f"http://127.0.0.1:{port}/bot{_TOKEN}/getUpdates", timeout=5.0)
    except httpx.ConnectError as exc:
        return exc
    raise AssertionError("a closed port answered")


def test_transport_errors_back_off_and_a_good_poll_resets(caplog: Any) -> None:
    script: list[Any] = [
        _connect_error(),
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
    assert all("\n" not in r.getMessage() for r in transport)  # one line each
    (unexpected,) = [r for r in records if r.exc_info is not None]
    assert unexpected.exc_info[0] is ValueError  # a bug keeps its traceback


def test_backoff_is_capped_at_sixty_seconds() -> None:
    poller, stop = _poller([httpx.ConnectError("down") for _ in range(10)])
    poller.run()
    assert max(stop.waits) == 60.0
    assert stop.waits[-3:] == [60.0, 60.0, 60.0]
