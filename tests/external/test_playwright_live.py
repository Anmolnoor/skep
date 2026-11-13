"""R6 (D25): browser reads against the REAL Playwright MCP server.

Opt-in like the other live tests: the ``external_app`` marker keeps it out
of the gates. It never downloads: npx runs ``--offline``, so a machine
without npx or without ``@playwright/mcp`` in its npm cache skips. The page
it reads is served on loopback from the test's own folder; the skep home is
a throwaway; the browser runs headless and ``--isolated`` (no profile on
disk).
"""

from __future__ import annotations

import functools
import http.server
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor import mcp_client as mc
from skep.supervisor.serve.cards import high_tier
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import execute_mutation

pytestmark = pytest.mark.external_app

COMMAND = ("npx", "--offline", "--yes", "@playwright/mcp@latest", "--headless", "--isolated")


@pytest.fixture
def page_url(tmp_path: Path) -> object:
    site = tmp_path / "site"
    site.mkdir()
    (site / "page.html").write_text(
        "<!doctype html><title>R6 live page</title><h1>Hello from R6</h1>", encoding="utf-8"
    )
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(site))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/page.html"
    server.shutdown()


def _require_playwright() -> None:
    if shutil.which("npx") is None:
        pytest.skip("npx is not on PATH")
    probe = subprocess.run([*COMMAND[:4], "--version"], capture_output=True, text=True, timeout=60)
    if probe.returncode != 0:
        pytest.skip("@playwright/mcp is not in the npm cache (this test never downloads it)")


def test_real_playwright_read_spawns_one_server_and_is_not_a_high_card(
    tmp_path: Path, page_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_playwright()
    monkeypatch.chdir(tmp_path)  # Playwright writes .playwright-mcp/ to its cwd
    spawned: list[str] = []
    real_exchange = mc._stdio_exchange

    def counting(argv: list[str], payload: str) -> list[str]:
        spawned.append(payload.splitlines()[-1])
        return real_exchange(argv, payload)

    monkeypatch.setattr(mc, "_stdio_exchange", counting)
    config = SupervisorConfig(home=tmp_path / "home" / "supervisor", worker_command=("x",))
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        mc.save_mcp_server(
            store,
            mc.MCPServerConfig(
                server_id="browser", transport="stdio", command=COMMAND, scope="browse"
            ),
        )

        def call(tool: str, arguments: dict[str, object]) -> dict[str, object]:
            result = execute_mutation(
                "call_mcp_tool", {"server_id": "browser", "tool": tool, "arguments": arguments},
                store=store, holder=holder,
                runner=None,  # type: ignore[arg-type]  # an MCP call never dispatches
                actor="live-test",
            )  # fmt: skip
            assert isinstance(result, dict)
            return result

        # An act cards HIGH and, confirmed, returns the page it opened (F162).
        navigate = {"server_id": "browser", "tool": "browser_navigate"}
        assert high_tier("call_mcp_tool", navigate, store=store) is not None
        opened = call("browser_navigate", {"url": page_url})
        if not opened["ok"] and "install" in str(opened.get("error")):
            pytest.skip(f"Playwright has no browser here: {opened['error']}")
        assert opened["ok"], opened
        assert "R6 live page" in str(opened["content"])

        # A read is not a HIGH card, and two reads list the tools once (F160).
        spawned.clear()
        snapshot = {"server_id": "browser", "tool": "browser_snapshot"}
        for _ in range(2):
            assert mc.mcp_call_is_read(store, "browser", "browser_snapshot")
            assert high_tier("call_mcp_tool", snapshot, store=store) is None
            assert call("browser_snapshot", {})["ok"]
        methods = [line.split('"method": "')[1].split('"')[0] for line in spawned]
        assert methods == ["tools/list", "tools/call", "tools/call"]
    finally:
        store.close()
