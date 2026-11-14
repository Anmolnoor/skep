"""Review leftover: two `skep serve` on one home ran two supervisors over one
store — double-resumed follow-ups, workers re-adopted twice. The home is
locked (flock, released by the OS when the holder dies); a second serve
refuses naming the holder."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
import uvicorn

from skep.supervisor.serve.serve_cmds import cmd_serve, take_serve_lock


def _args(home: Path, port: int) -> argparse.Namespace:
    return argparse.Namespace(
        home=home, worker_cmd=None, auto_approve=False, host="127.0.0.1", port=port
    )


def test_a_second_serve_on_one_home_refuses_naming_the_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ran: list[int] = []

    def fake_run(self: uvicorn.Server, sockets: object = None) -> None:
        ran.append(1)
        self.started = True

    monkeypatch.setattr(uvicorn.Server, "run", fake_run)
    held, holder = take_serve_lock(tmp_path / "supervisor", 8765)
    assert held is not None and holder is None
    try:
        assert cmd_serve(_args(tmp_path, 8799)) == 1
        err = capsys.readouterr().err
        assert "another skep serve" in err and "port 8765" in err and "pid " in err
        assert ran == []  # it never started
    finally:
        held.close()
    # the holder gone (the OS releases a dead process's lock): it starts
    assert cmd_serve(_args(tmp_path, 8799)) == 0
    assert ran == [1]
    # and released it on the way out
    again, _ = take_serve_lock(tmp_path / "supervisor", 8799)
    assert again is not None
    again.close()
