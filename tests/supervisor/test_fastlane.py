"""v83-F2: the run_code fast lane — same walls, no worktree, 10 seconds.

The live tests run only where a native sandbox backend is usable (the
sandbox-suite convention); the fail-closed test runs everywhere — it IS
the point of the lane: no sandbox, no supervisor-side execution, ever.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from types import SimpleNamespace
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
    result = run_code_fast(
        "python", "print(sum(range(101)))", skep_home=Path("/nonexistent/skep-home")
    )
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
        skep_home=Path("/nonexistent/skep-home"),
    )
    assert result is not None
    assert result["exit_code"] == 7  # the connect failed inside the walls


@_live
def test_fast_lane_writes_outside_workspace_fail(tmp_path: Path) -> None:
    # Aim outside every temp root (bwrap's /tmp is a private tmpfs, so a
    # target there tests the wrong wall); the directory exists only so the
    # write could succeed if the sandbox ever let it through.
    outside = Path.home() / ".cache" / f"skep-fastlane-escape-{os.getpid()}"
    outside.mkdir(parents=True, exist_ok=True)
    target = outside / "escape.txt"
    try:
        result = run_code_fast(
            "python",
            f"open({str(target)!r}, 'w').write('leak')\n",
            skep_home=tmp_path / "skep-home",
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
    result = run_code_fast(
        "python", "import time; time.sleep(30)", skep_home=Path("/nonexistent/skep-home")
    )
    assert result is not None
    assert result["timed_out"] is True
    assert "worker lane" in result["error"]  # the error names the alternative (I9)


def test_no_sandbox_means_no_fast_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    """I12: bwrap absence is a fall-through, never a fail-open."""
    monkeypatch.setattr(
        "skep.supervisor.serve.fastlane.availability",
        lambda: sandbox.SandboxAvailability(False, "missing_binary", "no bwrap"),
    )
    assert (
        run_code_fast(
            "python", "print('never runs unsandboxed')", skep_home=Path("/nonexistent/skep-home")
        )
        is None
    )


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
        lambda store, task_id, **kw: {"task_id": task_id, "state": "completed"},
    )
    store = RunStore(tmp_path / "s.sqlite3")
    holder = SimpleNamespace(current=SimpleNamespace(home=tmp_path / "home" / "supervisor"))
    try:
        result = tools.execute_mutation(
            "run_code",
            {"repo": "r", "code": "print(1)", "fast": True},
            store=store,
            holder=holder,  # type: ignore[arg-type]
            runner=None,  # type: ignore[arg-type]
            actor="tester",
        )
    finally:
        store.close()
    assert submitted == ["script"]  # the dispatch happened
    assert result["fast_lane_fallback"] == FAST_LANE_FALLBACK_NOTE


def test_fast_lane_profile_uses_serve_home_not_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Mac finding A: `skep serve --home X` does not export SKEP_HOME, so a
    profile that fell back to $SKEP_HOME/~/.skep hid the WRONG serve-token —
    fast=true could read X's. The tool hands the profile serve's own home."""
    import subprocess

    from skep.supervisor import RunStore
    from skep.supervisor.serve import fastlane, tools

    seen: dict[str, object] = {}
    monkeypatch.setenv("SKEP_HOME", str(tmp_path / "decoy"))
    monkeypatch.setattr(
        fastlane, "availability", lambda: sandbox.SandboxAvailability(True, backend="bubblewrap")
    )
    monkeypatch.setattr(fastlane, "write_profile", lambda path, **kw: seen.update(kw) or path)
    monkeypatch.setattr(fastlane, "wrap_command", lambda argv, profile: argv)
    monkeypatch.setattr(
        subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", "")
    )
    home = tmp_path / "opshome"
    holder = SimpleNamespace(current=SimpleNamespace(home=home / "supervisor"))
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        tools.execute_mutation(
            "run_code",
            {"repo": "r", "code": "print(1)", "fast": True},
            store=store,
            holder=holder,  # type: ignore[arg-type]
            runner=None,  # type: ignore[arg-type]
            actor="tester",
        )
    finally:
        store.close()
    assert seen["skep_home"] == home
