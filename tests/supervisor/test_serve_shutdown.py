"""ES B119: SIGTERM stops the scheduler first and the drain is bounded.

Lane F3 (f3-18): serve ignored SIGTERM for 139 s while a chat turn hung, and
the ticker kept firing schedules the whole time — `ticker.stop()` sat in the
lifespan shutdown, which uvicorn runs only after draining every request.
"""

from __future__ import annotations

import signal

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.serve.serve_cmds import SHUTDOWN_GRACE_SECONDS, make_server

from .conftest import serve_client


def test_the_first_sigterm_halts_the_ticker_before_the_drain(config: SupervisorConfig) -> None:
    with serve_client(config, start_ticker=True, start_channels=False) as client:
        app = client.app
        ticker = app.state.ticker  # type: ignore[attr-defined]
        assert ticker.is_alive()
        server = make_server(app, host="127.0.0.1", port=0)
        server.handle_exit(signal.SIGTERM, None)
        assert server.should_exit
        ticker.join(5)
        assert not ticker.is_alive(), "the ticker kept running into the drain"


def test_cmd_serve_runs_the_bounded_server(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse

    import uvicorn

    from skep.supervisor.serve.serve_cmds import cmd_serve

    ran: list[float | None] = []

    def _run(self: uvicorn.Server, sockets: object = None) -> None:
        ran.append(self.config.timeout_graceful_shutdown)

    monkeypatch.setattr(uvicorn.Server, "run", _run)
    args = argparse.Namespace(
        home=config.home,
        worker_cmd=None,
        auto_approve=False,
        host="127.0.0.1",
        port=0,
    )
    # Never started (run is stubbed) → uvicorn.run's startup-failure code.
    assert cmd_serve(args) == 3
    assert ran == [SHUTDOWN_GRACE_SECONDS]
    assert 0 < SHUTDOWN_GRACE_SECONDS <= 30
