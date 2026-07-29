"""v72-F5: the first-party mail + calendar MCP servers.

Both speak the forge line protocol (the trial-harness shape drives them as
real subprocesses), pass their zero-argument self_test OFFLINE, and carry
no permission logic — the email scope classifies reads as flowing and
send_message as carding, in the one engine (I5/I6).
"""

from __future__ import annotations

import datetime as dt
import email
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from skep.mcp_servers.calendar import VEvent, occurrences, parse_ics
from skep.mcp_servers.mail import compose, text_body


def _drive(module: str, requests: list[dict[str, Any]], *, env: dict[str, str]) -> list[Any]:
    """Run a server as a subprocess, one JSON-RPC line per request."""
    payload = "".join(json.dumps(r) + "\n" for r in requests)
    proc = subprocess.run(
        [sys.executable, "-m", module],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": env.get("PATH", ""), **env},
    )
    assert proc.returncode == 0, proc.stderr
    return [json.loads(line)["result"] for line in proc.stdout.splitlines() if line.strip()]


def _rpc(method: str, request_id: int, **params: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params:
        body["params"] = params
    return body


BASE_REQUESTS = [
    _rpc("initialize", 1),
    _rpc("tools/list", 2),
    _rpc("tools/call", 3, name="self_test", arguments={}),
]


def test_mail_server_speaks_the_contract_and_self_tests_offline(tmp_path: Path) -> None:
    # NO mail env at all: the forge-trial posture (no network, no config).
    init, listing, self_test = _drive(
        "skep.mcp_servers.mail", BASE_REQUESTS, env={"HOME": str(tmp_path)}
    )
    assert init["protocolVersion"]
    names = {tool["name"] for tool in listing["tools"]}
    assert {"list_recent", "read_message", "send_message", "self_test"} <= names
    assert len(names) >= 2  # the trial's "self_test alone is not a tool server"
    assert not self_test.get("isError"), self_test
    assert "not configured yet" in self_test["content"]  # honest about missing env


def test_mail_reads_refuse_unconfigured_with_the_fix_named(tmp_path: Path) -> None:
    (result,) = _drive(
        "skep.mcp_servers.mail",
        [_rpc("tools/call", 1, name="list_recent", arguments={})],
        env={"HOME": str(tmp_path)},
    )
    assert result["isError"] is True
    assert "SKEP_MAIL_IMAP_HOST" in result["content"]  # the error teaches (I9)


def test_mail_body_extraction_and_compose() -> None:
    fixture = (
        b"From: a@b.test\r\nSubject: multi\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/alternative; boundary="x"\r\n\r\n'
        b"--x\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nplain wins\r\n"
        b"--x\r\nContent-Type: text/html\r\n\r\n<b>html loses</b>\r\n--x--\r\n"
    )
    assert "plain wins" in text_body(email.message_from_bytes(fixture))
    html_only = email.message_from_bytes(
        b"From: a@b.test\r\nContent-Type: text/html\r\n\r\n<b>only html</b>\r\n"
    )
    assert "[no text/plain part" in text_body(html_only)
    message = compose("s@e.test", "r@e.test", "subj", "body")
    assert message["To"] == "r@e.test" and "body" in message.get_content()


def test_calendar_server_self_tests_offline_and_reads_a_real_ics(tmp_path: Path) -> None:
    soon = dt.datetime.now() + dt.timedelta(days=1)
    ics = tmp_path / "cal.ics"
    ics.write_text(
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
        f"DTSTART:{soon:%Y%m%dT%H%M%S}\nSUMMARY:dentist\nEND:VEVENT\nEND:VCALENDAR\n",
        encoding="utf-8",
    )
    init, listing, self_test, upcoming = _drive(
        "skep.mcp_servers.calendar",
        [*BASE_REQUESTS, _rpc("tools/call", 4, name="upcoming_events", arguments={"days": 3})],
        env={"HOME": str(tmp_path), "SKEP_CALENDAR_ICS": str(ics)},
    )
    assert init["protocolVersion"]
    assert {"upcoming_events", "self_test"} <= {t["name"] for t in listing["tools"]}
    assert not self_test.get("isError"), self_test
    assert "dentist" in upcoming["content"]


def test_calendar_unconfigured_error_teaches(tmp_path: Path) -> None:
    (result,) = _drive(
        "skep.mcp_servers.calendar",
        [_rpc("tools/call", 1, name="upcoming_events", arguments={})],
        env={"HOME": str(tmp_path)},
    )
    assert result["isError"] is True and "SKEP_CALENDAR_ICS" in result["content"]


def test_ics_parser_recurrence_and_folding() -> None:
    events = parse_ics(
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART:20260701T100000\n"
        "SUMMARY:weekly\n sync\nRRULE:FREQ=WEEKLY\nEND:VEVENT\n"
        "BEGIN:VEVENT\nDTSTART:20260702T100000\nSUMMARY:monthly\n"
        "RRULE:FREQ=MONTHLY\nEND:VEVENT\nEND:VCALENDAR\n"
    )
    assert events[0].summary == "weeklysync"  # folded continuation line
    weekly = occurrences(events[0], dt.datetime(2026, 7, 10), dt.datetime(2026, 7, 30))
    assert [when.day for when, _ in weekly] == [15, 22, 29]
    # An unsupported RRULE degrades honestly: one line, marked recurring.
    monthly = occurrences(events[1], dt.datetime(2026, 7, 1), dt.datetime(2026, 7, 30))
    assert monthly == [(dt.datetime(2026, 7, 2, 10, 0), "monthly (recurring)")]
    outside = occurrences(
        VEvent(start=dt.datetime(2026, 1, 1), all_day=True, summary="x", rrule_freq=None),
        dt.datetime(2026, 7, 1),
        dt.datetime(2026, 7, 30),
    )
    assert outside == []


def test_email_scope_classifies_reads_flowing_and_send_carding(tmp_path: Path) -> None:
    """The whole point of shipping first-party servers WITHOUT permission
    logic: the existing engine classifies them (I5), and sending always
    cards (I6)."""
    from skep.supervisor import RunStore
    from skep.supervisor.mcp_client import (
        MCPServerConfig,
        MCPTool,
        mcp_scope_decision,
        mcp_tool_scope_action,
        save_mcp_server,
    )

    store = RunStore(tmp_path / "s.sqlite3")
    try:
        save_mcp_server(
            store,
            MCPServerConfig(
                server_id="mail",
                transport="stdio",
                command=(sys.executable, "-m", "skep.mcp_servers.mail"),
                scope="email",
            ),
        )

        def tool(name: str) -> MCPTool:
            return MCPTool(server_id="mail", name=name, description="")

        assert mcp_tool_scope_action(store, tool("list_recent")) == ("email", "read")
        assert mcp_tool_scope_action(store, tool("read_message")) == ("email", "read")
        assert mcp_tool_scope_action(store, tool("send_message")) == ("email", "send")
        send_decision = mcp_scope_decision(store, tool("send_message"))
        assert send_decision.verdict == "require_approval"
    finally:
        store.close()
