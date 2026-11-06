"""B1-2 (ADR 0054 D3/D5, A2/A8/A9): the ACP session state and mapper against the wire table."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor.acp.mapper import (
    AGENT_PRESETS,
    Decided,
    NoOpinion,
    Probe,
    Session,
    Unreadable,
    agent_name,
    permission_probe,
    probe,
)
from skep.supervisor.policy_schema import SCOPE_ACTIONS

from ..fixtures.acp import ACP_DIR, ALL_FIXTURES, envelope_frames

EXPECTED = {
    (path.parent.name, entry["fixture"], entry["line"]): entry
    for path in sorted(ACP_DIR.glob("*/expected_probes.json"))
    for entry in json.loads(path.read_text(encoding="utf-8"))
}


def _as_json(p: Probe) -> dict[str, Any]:
    if isinstance(p, Decided):
        return {"kind": "decided", "items": [list(i) for i in p.items], "cwd": p.cwd}
    return {"kind": "no_opinion" if isinstance(p, NoOpinion) else "unreadable", "reason": p.reason}


@pytest.mark.parametrize("fixture", ALL_FIXTURES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_every_permission_request_maps_as_the_wire_table_says(
    fixture: Path, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    session, seen = Session(), set()
    for line, (direction, raw) in enumerate(envelope_frames(fixture, str(repo)), 1):
        msg = json.loads(raw)
        if msg.get("method") == "session/request_permission" and "params" in msg:
            # The caller's argv fallback when initialize named no agent (kiro, grok).
            session.agent = session.agent or agent_name({}, [fixture.parent.name])
            key = (fixture.parent.name, fixture.name, line)
            want = json.loads(json.dumps(EXPECTED[key]).replace("$REPO", str(repo)))
            tid, got = permission_probe(msg["params"], session)
            assert (tid, _as_json(got)) == (want["tool_call_id"], want["probe"])
            seen.add(key)
        session.feed(direction, msg)
    assert seen == {k for k in EXPECTED if k[:2] == (fixture.parent.name, fixture.name)}


def test_expected_items_stay_inside_the_closed_vocabulary() -> None:
    items = [
        i for e in EXPECTED.values() if e["probe"]["kind"] == "decided" for i in e["probe"]["items"]
    ]
    assert items and {(s, a) for s, a, _ in items} <= {
        (s, a) for s, acts in SCOPE_ACTIONS.items() for a in acts
    }
    assert {(s, a) for s, a, _ in items} <= {("shell", "run"), ("filesystem", "write")}


def _update(update: str, **fields: Any) -> dict[str, Any]:
    return {"method": "session/update", "params": {"update": {"sessionUpdate": update, **fields}}}


def test_tool_call_updates_merge_as_patches() -> None:
    s = Session()
    s.feed("a2c", _update("tool_call", toolCallId="t", kind="edit", rawInput={}, locations=[]))
    s.feed("a2c", _update("tool_call_update", toolCallId="t", kind=None, locations=[{"path": "a"}]))
    s.feed("a2c", _update("tool_call_update", toolCallId="t", locations=[{"path": "b"}]))
    assert s.tool_calls["t"] == {
        "toolCallId": "t",
        "kind": "edit",
        "rawInput": {},
        "locations": [{"path": "b"}],
    }
    s.feed("a2c", _update("terminal_update", terminalId="t", command="ls"))
    assert s.terminals == {"t": "ls"}
    assert probe({"toolCallId": "t", "kind": "execute", "rawInput": {}}, s) == Decided(
        (("shell", "run", "ls"),), ""
    )


def test_codex_cached_command_beats_the_quoted_request(tmp_path: Path) -> None:
    s = Session(cwd=str(tmp_path), agent="codex-acp")
    s.feed(
        "a2c",
        _update(
            "tool_call_update", toolCallId="x", kind="execute", rawInput={"command": "printf a > b"}
        ),
    )
    params = {
        "subject": {
            "type": "tool_call",
            "toolCall": {
                "toolCallId": "x",
                "kind": "execute",
                "rawInput": {"command": '"printf a > b"', "cwd": "/w"},
            },
        }
    }
    assert permission_probe(params, s) == ("x", Decided((("shell", "run", "printf a > b"),), "/w"))


def test_request_shapes_without_a_readable_operation_are_never_decided(tmp_path: Path) -> None:
    s = Session(cwd=str(tmp_path))
    assert permission_probe({"toolCall": {"toolCallId": "k", "kind": "execute"}}, s) == (
        "k",
        Unreadable("rawInput.command missing for kind=execute (agent generic)"),
    )
    assert permission_probe({"toolCall": {"toolCallId": "e", "kind": "edit"}}, s)[1] == Unreadable(
        "no path for kind=edit (agent generic)"
    )
    assert permission_probe({"subject": {"type": "_ext"}}, s) == (
        None,
        NoOpinion("permission request carries no tool call"),
    )
    assert permission_probe(
        {"subject": {"type": "command", "command": "ls", "cwd": "/c", "toolCallId": "c"}}, s
    ) == (
        "c",
        Decided((("shell", "run", "ls"),), "/c"),
    )
    assert probe({"kind": "read"}, s) == NoOpinion("kind 'read': no rule in skep's vocabulary")
    assert probe({}, s) == NoOpinion("no kind")
    assert probe({"kind": "fetch", "rawInput": {"url": "https://docs.example/x"}}, s) == Decided(
        (("network", "fetch", "docs.example"),), str(tmp_path)
    )


def test_agent_name_from_the_result_or_argv() -> None:
    assert (
        agent_name({"agentInfo": {"name": "@agentclientprotocol/claude-agent-acp"}}, ())
        == "claude-agent-acp"
    )
    assert agent_name({"info": {"name": "@zed-industries/codex-acp"}}, ()) == "codex-acp"
    assert (
        agent_name({}, ["npx", "-y", "@agentclientprotocol/claude-agent-acp@0.101.0"])
        == "claude-agent-acp"
    )
    assert agent_name({}, ["/usr/local/bin/kiro", "acp"]) == "kiro"
    assert agent_name({"agentInfo": {"name": "grok-replay"}}, ()) == "grok-replay"
    assert "grok-replay" not in AGENT_PRESETS  # falls back to generic, keeps its name


def test_rings_live_names_the_hard_ring_only_on_v1_with_client_caps() -> None:
    def rings(version: int, caps: dict[str, Any]) -> tuple[str, ...]:
        s = Session()
        s.feed("c2a", {"id": 0, "method": "initialize", "params": {"clientCapabilities": caps}})
        s.feed("a2c", {"id": 0, "result": {"protocolVersion": version}})
        return s.rings_live()

    assert rings(1, {"terminal": True}) == ("advisory", "hard")
    assert rings(1, {"fs": {"writeTextFile": True}}) == ("advisory", "hard")
    assert rings(1, {}) == ("advisory",)
    assert rings(2, {"terminal": True}) == ("advisory",)


def test_mark_observed_is_true_the_first_time_only() -> None:
    s = Session()
    assert s.mark_observed("t") and not s.mark_observed("t")
