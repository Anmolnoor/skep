"""v83-F11: setup_browser — the one-card Playwright registration."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_setup_browser_registers_playwright_under_browse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v83-F11: one card from zero to a browsing Queen — the documented npx
    incantation registered under the browse scope; a dead handshake reports
    honestly instead of leaving a silently dead entry (I8)."""
    from skep.supervisor import mcp_client as mc
    from skep.supervisor.serve.tools import (
        COMMAND_TOOL_NAMES,
        MUTATING_TOOL_NAMES,
        execute_mutation,
    )
    from skep.supervisor.store import RunStore

    assert "setup_browser" in MUTATING_TOOL_NAMES
    assert "setup_browser" in COMMAND_TOOL_NAMES  # the /browser deck verb

    class _DeadClient:
        def __init__(self, *a: object, **kw: object) -> None:
            pass

        def list_tools(self) -> list[object]:
            raise mc.MCPError("npx not found")

    monkeypatch.setattr(mc, "MCPClient", _DeadClient)
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        result = execute_mutation(
            "setup_browser",
            {},
            store=store,
            holder=None,  # type: ignore[arg-type]
            runner=None,  # type: ignore[arg-type]
            actor="tester",
        )
        assert result["registered"] == "browser"
        assert "npx" in result["handshake_failed"]
        saved = mc.load_mcp_servers(store)["browser"]
        assert saved.scope == "browse"
        assert saved.command == ("npx", "@playwright/mcp@latest")

        class _LiveClient(_DeadClient):
            def list_tools(self) -> list[object]:
                return [
                    mc.MCPTool(server_id="browser", name="browser_snapshot", description="")
                ]

        monkeypatch.setattr(mc, "MCPClient", _LiveClient)
        live = execute_mutation(
            "setup_browser",
            {},
            store=store,
            holder=None,  # type: ignore[arg-type]
            runner=None,  # type: ignore[arg-type]
            actor="tester",
        )
        assert live["tools"] == ["browser_snapshot"]
    finally:
        store.close()
