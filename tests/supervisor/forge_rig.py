"""Stand-ins for the forged-tool trial, shared by the forge tests.

Review R1 (K1): the dispatched half of a trial is the role gate only — it
runs ``forge.GATE_SCRIPT``, never the candidate — and the contract is
judged by ``forge.wall_trial`` behind ``mcp_client.forged_lane``. Contract
tests that must not depend on a sandbox backend swap the lane's runner for
``host_runner`` (the same JSON-RPC exchange, on the host, with a source the
test wrote itself)."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import forge, mcp_client

GATE_PASSED: dict[str, Any] = {
    "task_id": "gate-1",
    "state": "completed",
    "output": forge.GATE_MARKER + "the role chain admitted this trial\n",
}


def host_runner(source: str, python: str, *, skep_home: Path, hosts: tuple[str, ...]) -> Any:
    """``mcp_client.forged_runner``'s shape, run on the host (tests only)."""

    def run(method: str, params: Mapping[str, object]) -> Mapping[str, object]:
        with tempfile.TemporaryDirectory() as tmp:
            tool = Path(tmp) / "tool.py"
            tool.write_text(source, encoding="utf-8")
            payload = mcp_client._stdio_payload(method, params)
            proc = subprocess.run(  # forged_exchange's shape: the whole stdin at once
                [python, str(tool)], input=payload, capture_output=True, text=True,
                timeout=mcp_client.MCP_CALL_DEADLINE_SECONDS, cwd=tmp,
            )  # fmt: skip
        return mcp_client._stdio_reply(proc.stdout.splitlines())

    return run


def contract(source: str, home: Path) -> tuple[bool, str, dict[str, Any]]:
    """The trial's contract over ``source``, on the host."""
    patch = pytest.MonkeyPatch()
    patch.setattr(mcp_client, "forged_runner", host_runner)
    try:
        return forge.wall_trial(home, "forge-x", source, sys.executable, ())
    finally:
        patch.undo()


def gate(monkeypatch: pytest.MonkeyPatch, result: dict[str, Any] | None = None) -> list[Any]:
    """Patch the dispatched gate; returns the kwargs of every dispatch."""
    from skep.supervisor.serve import tools as tools_module

    seen: list[Any] = []

    def trial(*_a: Any, **kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs)
        return GATE_PASSED if result is None else result

    monkeypatch.setattr(tools_module, "_forge_trial", trial)
    return seen


def host_wall(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The wall's runner on the host; returns every source it ran."""
    ran: list[str] = []

    def runner(source: str, python: str, *, skep_home: Path, hosts: tuple[str, ...]) -> Any:
        ran.append(source)
        return host_runner(source, python, skep_home=skep_home, hosts=hosts)

    monkeypatch.setattr(mcp_client, "forged_runner", runner)
    return ran


def failing_wall(monkeypatch: pytest.MonkeyPatch, error: str) -> None:
    """A wall whose self_test fails with ``error``."""

    def runner(source: str, python: str, *, skep_home: Path, hosts: tuple[str, ...]) -> Any:
        def run(method: str, params: Mapping[str, object]) -> Mapping[str, object]:
            if method == "tools/list":
                return {"tools": [{"name": "x"}, {"name": "self_test"}]}
            return {"content": error, "isError": True}

        return run

    monkeypatch.setattr(mcp_client, "forged_runner", runner)


def install(config: Any, plugin_id: str, source: str) -> Path:
    """An installed copy, written the way a past promotion left it."""
    installed = forge.installed_tools_root(config) / f"{plugin_id}.py"
    installed.parent.mkdir(parents=True, exist_ok=True)
    installed.write_text(source, encoding="utf-8")
    return installed
