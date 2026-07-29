"""v83-F2: the run_code fast lane — same walls, no worktree, 10 seconds.

The live tests run only where a native sandbox backend is usable (the
sandbox-suite convention); the fail-closed test runs everywhere — it IS
the point of the lane: no sandbox, no supervisor-side execution, ever.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import sandbox
from skep.supervisor.serve.fastlane import (
    FAST_LANE_FALLBACK_NOTE,
    run_code_fast,
)

_live = pytest.mark.skipif(
    not sandbox.availability().usable,
    reason="no usable native sandbox backend on this host",
)


@_live
def test_fast_lane_runs_a_pure_computation() -> None:
    result = run_code_fast("python", "print(sum(range(101)))")
    assert result is not None
    assert result["exit_code"] == 0
    assert result["output"].strip() == "5050"
    assert result["fast_lane"] is True
    # The evidence rides the result (I8): hash + backend name the execution.
    assert len(result["code_sha256"]) == 64
    assert result["sandbox_backend"] == sandbox.availability().backend


@_live
def test_fast_lane_network_is_dead() -> None:
    result = run_code_fast(
        "python",
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=2)\n"
        "except OSError:\n"
        "    raise SystemExit(7)\n"
        "raise SystemExit(0)\n",
    )
    assert result is not None
    assert result["exit_code"] == 7  # the connect failed inside the walls


@_live
def test_fast_lane_writes_outside_workspace_fail(tmp_path: Path) -> None:
    # pytest's tmp_path lives under tempfile.gettempdir(), which the macOS
    # profile deliberately allows (default_writable_roots — the toolchain's
    # temp roots), so a target there tests the wrong wall. Aim outside every
    # temp root; the directory exists only so the write could succeed if the
    # sandbox ever let it through.
    outside = Path.home() / ".cache" / f"skep-fastlane-escape-{os.getpid()}"
    outside.mkdir(parents=True, exist_ok=True)
    target = outside / "escape.txt"
    try:
        result = run_code_fast(
            "python",
            f"open({str(target)!r}, 'w').write('leak')\n",
        )
        assert result is not None
        assert result["exit_code"] != 0
        assert not target.exists()
    finally:
        shutil.rmtree(outside, ignore_errors=True)


@_live
def test_fast_lane_timeout_teaches_the_slow_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("skep.supervisor.serve.fastlane.FAST_LANE_TIMEOUT_SECS", 1)
    result = run_code_fast("python", "import time; time.sleep(30)")
    assert result is not None
    assert result["timed_out"] is True
    assert "worker lane" in result["error"]  # the error names the alternative (I9)


def test_no_sandbox_means_no_fast_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    """I12: bwrap absence is a fall-through, never a fail-open."""
    monkeypatch.setattr(
        "skep.supervisor.serve.fastlane.availability",
        lambda: sandbox.SandboxAvailability(False, "missing_binary", "no bwrap"),
    )
    assert run_code_fast("python", "print('never runs unsandboxed')") is None


def test_run_code_fast_request_falls_back_to_worker_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """fast=true without a sandbox → a normal worker dispatch, zero
    supervisor-side execution, and the fallback reason named (I9)."""
    from skep.supervisor import RunStore
    from skep.supervisor.serve import actions, tools

    monkeypatch.setattr(
        "skep.supervisor.serve.fastlane.availability",
        lambda: sandbox.SandboxAvailability(False, "missing_binary", "no bwrap"),
    )
    submitted: list[str] = []

    def fake_submit(*a: Any, **kw: Any) -> str:
        submitted.append(kw["caste"])
        return "task-fastlane-test"

    monkeypatch.setattr(actions, "submit_run", fake_submit)
    monkeypatch.setattr(
        tools,
        "_script_run_result",
        lambda store, task_id: {"task_id": task_id, "state": "completed"},
    )
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        result = tools.execute_mutation(
            "run_code",
            {"repo": "r", "code": "print(1)", "fast": True},
            store=store,
            holder=None,  # type: ignore[arg-type]
            runner=None,  # type: ignore[arg-type]
            actor="tester",
        )
    finally:
        store.close()
    assert submitted == ["script"]  # the dispatch happened
    assert result["fast_lane_fallback"] == FAST_LANE_FALLBACK_NOTE
