"""v135 Wave 2a, review round 3 (ADR 0069 "As built"): each test names the
round-3 item (M1-M6) it pins.
"""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import mcp_client, mcp_sandbox, sandbox
from skep.supervisor.mcp_client import MCPServerConfig
from skep.supervisor.store import RunStore


@pytest.fixture(autouse=True)
def clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    sandbox.availability.cache_clear()
    mcp_client._LISTED.clear()
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    yield
    getattr(sandbox.availability, "cache_clear", lambda: None)()
    mcp_client._LISTED.clear()


@pytest.fixture()
def skep_home(tmp_path: Path) -> Path:
    home = tmp_path / "skep"
    (home / "supervisor").mkdir(parents=True)
    return home


@pytest.fixture()
def store(skep_home: Path) -> Iterator[RunStore]:
    opened = RunStore(skep_home / "supervisor" / "supervisor.sqlite3")
    yield opened
    opened.close()


# -- M1: a captured "no registration" is a capture, never a re-read ------------------


def test_a_registration_that_appears_after_the_capture_is_not_the_cards(
    store: RunStore, tmp_path: Path
) -> None:
    from skep.supervisor.serve.cards import recorded_card
    from skep.supervisor.serve.chat import pending_duplicate_action

    chat_id = store.create_chat(title="t", model=None).chat_id
    args = {"server_id": "late", "tool": "t", "arguments": {}}
    captured = mcp_sandbox.card_incarnation(store, args)
    assert captured is None  # the decision saw no server
    mcp_client.save_mcp_server(
        store, MCPServerConfig("late", "stdio", command=(sys.executable, "x.py"))
    )  # registered between the capture and the card
    store.add_chat_action(chat_id, tool="call_mcp_tool", args=args, card={"incarnation": None})
    _record, identical = pending_duplicate_action(
        store, chat_id, "call_mcp_tool", args, incarnation=None
    ) or (None, None)
    assert identical is True  # the check used the capture, as the card does
    card = recorded_card("call_mcp_tool", args, None, store, incarnation=None)
    assert card is not None and card["incarnation"] is None
    # nothing captured: the card reads the registration now
    read_now = recorded_card("call_mcp_tool", args, None, store)
    assert read_now is not None and read_now["incarnation"]


# -- M2: an http listing holds no registry gate ---------------------------------------


def test_a_slow_http_listing_does_not_block_a_registry_write(
    store: RunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    listing, release = threading.Event(), threading.Event()

    def slow(method: str, params: Any) -> Any:
        listing.set()
        release.wait(10)
        return {"tools": []}

    monkeypatch.setattr(mcp_client, "_transport_runner", lambda config, **_: slow)
    config = MCPServerConfig("web", "http", url="http://127.0.0.1:9/mcp")
    lister = threading.Thread(
        target=lambda: mcp_client.refresh_listed_tools(store, config), daemon=True
    )
    lister.start()
    assert listing.wait(5)
    wrote = threading.Event()

    def write() -> None:
        with mcp_sandbox.REGISTRY_GATE.writing():
            wrote.set()

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    try:
        assert wrote.wait(2), "a registry write waited on an http listing"
    finally:
        release.set()
        lister.join(5)
        writer.join(5)


# -- M5: initialize is validated as sent; only what is shown is redacted ---------------

_INIT_OK = (
    '{"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05", '
    '"capabilities": {}, "serverInfo": {"name": "fx", "version": "1"}}}'
)
_CALL_OK = '{"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": "hi"}]}}'


def _script(tmp_path: Path, init: str, reply: str = _CALL_OK) -> Path:
    script = tmp_path / "srv_m5.py"
    script.write_text(
        "import sys\n"
        "sys.stdin.readline()\n"
        f"print({init!r}, flush=True)\n"
        "sys.stdin.readline(); sys.stdin.readline()\n"
        f"print({reply!r}, flush=True)\n",
        encoding="utf-8",
    )
    return script


@pytest.mark.parametrize("secret", ["serverInfo", "protocolVersion", "capabilities"])
def test_a_secret_equal_to_a_required_field_name_does_not_refuse_the_handshake(
    tmp_path: Path, secret: str
) -> None:
    script = _script(tmp_path, _INIT_OK)
    payload = mcp_client._stdio_payload("tools/call", {"name": "t", "arguments": {}})
    lines = mcp_client._stdio_exchange(
        [sys.executable, str(script)], payload,
        scrub=lambda value: mcp_sandbox.redact(value, {"K": secret}),
    )  # fmt: skip
    assert json.loads(lines[-1])["id"] == 2


def test_an_initialize_error_is_still_shown_redacted(tmp_path: Path) -> None:
    masked = "an-error-value-to-mask-77"  # never `secret = "…"`: the hygiene scan
    init = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": f"bad {masked}"}}
    )
    payload = mcp_client._stdio_payload("tools/list", {})
    with pytest.raises(mcp_client.MCPNotRun) as caught:
        mcp_client._stdio_exchange(
            [sys.executable, str(_script(tmp_path, init))], payload,
            scrub=lambda value: mcp_sandbox.redact(value, {"K": masked}),
        )  # fmt: skip
    assert masked not in str(caught.value) and "[redacted]" in str(caught.value)
