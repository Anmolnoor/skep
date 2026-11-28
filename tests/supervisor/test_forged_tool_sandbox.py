"""ES (B115, lane F3 f3-37): an activated forged or seed tool ran as a plain
host stdio MCP server — unsandboxed, as the operator, with serve's whole env.
Only the trial was sandboxed, so a tool that behaved only under the trial
passed. Activated forged tools now run inside the sandbox on every call
(writes confined to a throwaway workspace, a scrubbed env), and refuse to
run at all when no sandbox backend is usable (fail closed). Since Phase 07
they run from their bound plugin record (test_forge_runtime_confinement).
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import RunStore, sandbox
from skep.supervisor.forge import ForgedPlugin, save_plugin
from skep.supervisor.mcp_client import (
    MCPError,
    MCPServerConfig,
    runner_for_config,
    save_mcp_server,
)

_TOOL = """
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("id") == 2:
        try:
            open({target!r}, "w").write("escaped")
            wrote = "wrote"
        except OSError:
            wrote = "blocked"
        print(json.dumps({{"jsonrpc": "2.0", "id": 2, "result": {{"content": wrote}}}}))
"""


def _forged(tmp_path: Path, target: Path) -> MCPServerConfig:
    script = tmp_path / "tool.py"
    script.write_text(_TOOL.format(target=str(target)))
    return MCPServerConfig("forge-probe", "stdio", command=(sys.executable, str(script)))


def _bound_store(home: Path, target: Path) -> RunStore:
    """The probe active and bound, as promote_tool leaves it."""
    source = _TOOL.format(target=str(target))
    (home / "tools").mkdir(parents=True)
    (home / "tools" / "probe.py").write_text(source, encoding="utf-8")
    (home / "supervisor").mkdir()
    store = RunStore(home / "supervisor" / "supervisor.sqlite3")
    save_plugin(
        store,
        ForgedPlugin(
            "probe", "probe", "p", "active", "", "probe.py", "", "forge-probe",
            source_sha256=hashlib.sha256(source.encode()).hexdigest(),
        ),
    )  # fmt: skip
    return store


def test_forged_tool_call_is_wrapped_in_the_sandbox_with_a_scrubbed_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    class FakePopen:  # forged_lane's bounded run (ADR 0069 review r2 N13)
        pid, returncode = 0, 0

        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            seen["argv"], seen["env"] = argv, kwargs.get("env")

        def communicate(self, payload: Any = None, timeout: Any = None) -> tuple[str, str]:
            return '{"jsonrpc": "2.0", "id": 2, "result": {"content": "ok"}}\n', ""

    monkeypatch.setattr(
        sandbox, "availability", lambda backend=None: sandbox.SandboxAvailability(True)
    )
    monkeypatch.setattr(
        sandbox, "wrap_command", lambda argv, profile, backend=None: ["SANDBOX", *argv]
    )
    monkeypatch.setattr(sandbox, "write_profile", lambda path, **_: path)
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.setenv("SKEP_FAKE_SECRET", "CANARY")
    home = tmp_path / "home"
    store = _bound_store(home, tmp_path / "x")
    config = save_mcp_server(store, _forged(tmp_path, tmp_path / "x"))  # the one in force
    try:
        run = runner_for_config(config, skep_home=home, store=store)
        assert run("tools/call", {"name": "t"}) == {"content": "ok"}
    finally:
        store.close()
    assert seen["argv"][0] == "SANDBOX"
    assert "SKEP_FAKE_SECRET" not in seen["env"]

    # A plain (operator-registered) server is not wrapped — B115 is forged tools.
    def fake_exchange(argv: list[str], payload: str, **_: object) -> list[str]:
        seen["argv"] = argv
        return ['{"jsonrpc": "2.0", "id": 2, "result": {}}']

    monkeypatch.setattr("skep.supervisor.mcp_client._stdio_exchange", fake_exchange)
    plain = MCPServerConfig("plain", "stdio", command=(sys.executable, "x.py"))
    runner_for_config(plain)("tools/call", {"name": "t"})
    assert seen["argv"][0] == sys.executable


def test_forged_tool_refuses_to_run_without_a_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sandbox,
        "availability",
        lambda backend=None: sandbox.SandboxAvailability(False, reason="missing"),
    )
    with pytest.raises(MCPError, match="sandbox"):
        runner_for_config(_forged(tmp_path, tmp_path / "x"), skep_home=tmp_path / "home")(
            "tools/call", {"name": "t"}
        )


@pytest.mark.skipif(not sandbox.available(), reason="needs a usable native sandbox backend")
def test_forged_tool_cannot_write_outside_its_workspace(tmp_path: Path) -> None:
    target = tmp_path / "escape.txt"
    store = _bound_store(tmp_path / "home", target)
    try:
        config = save_mcp_server(store, _forged(tmp_path, target))  # the one in force
        result = runner_for_config(config, skep_home=tmp_path / "home", store=store)(
            "tools/call", {"name": "t"}
        )
    finally:
        store.close()
    assert result == {"content": "blocked"}
    assert not target.exists()


def test_forged_tool_refuses_without_its_skep_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # B149: the profile hides THIS home's secrets — no home, no run.
    monkeypatch.setattr(
        sandbox, "availability", lambda backend=None: sandbox.SandboxAvailability(True)
    )
    with pytest.raises(MCPError, match="skep home"):
        runner_for_config(_forged(tmp_path, tmp_path / "x"))("tools/call", {"name": "t"})
