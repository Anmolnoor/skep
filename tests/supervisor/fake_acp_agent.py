"""A stand-in ACP agent for test_acp_govern / test_acp_engine (run as a script, never imported).

fake_acp_agent.py <fixture.ndjson> <repo>   print the fixture's a2c lines, drain stdin, exit 3
fake_acp_agent.py --doctor                  answer initialize + session/new like a v1 agent
fake_acp_agent.py --exit-only N             exit N at once, never reading stdin
fake_acp_agent.py --engine <scenario.json>  a v1 adapter for the ACP engine (ADR 0058): answers
                                            the handshake, then plays the scenario's ``steps``
                                            inside session/prompt, asking before each effect
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_AGENT = {"name": "@agentclientprotocol/claude-agent-acp", "version": "9.9.9"}
_MODES = {"currentModeId": "auto", "availableModes": [{"id": "default"}, {"id": "auto"}]}


def _send(**body: Any) -> None:
    print(json.dumps({"jsonrpc": "2.0", **body}), flush=True)


def _update(tid: str, **fields: Any) -> None:
    update = {"sessionUpdate": "tool_call", "toolCallId": tid, **fields}
    _send(method="session/update", params={"sessionId": "s1", "update": update})


def _answer(rid: int) -> dict[str, Any]:
    """The client's response to our request ``rid`` (nothing else arrives meanwhile)."""
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("id") == rid and "method" not in msg:
            return dict(msg)
    return {}


def _step(n: int, step: dict[str, Any]) -> None:
    tid = f"call-{n}"
    if "exit" in step:  # the adapter dies mid-prompt
        raise SystemExit(step["exit"])
    if "noise" in step:  # a line that is not the protocol
        print(step["noise"], flush=True)
        return
    if "request" in step:  # an agent->client request, e.g. terminal/create
        _send(id=700 + n, **step["request"])
        _answer(700 + n)
        return
    if "observe" in step:  # ran without asking: only a finished tool_call is reported
        _update(tid, status="completed", **step["observe"])
        return
    if "read" in step:  # reads a path itself and leaves what happened in the worktree
        try:
            Path(step["read"]).read_bytes()
            seen = "read"
        except OSError as exc:
            seen = type(exc).__name__
        Path(step["into"]).write_text(seen + "\n", encoding="utf-8")
        return
    call = {"toolCallId": tid, **step["ask"]}
    _update(tid, status="pending", **step["ask"])
    options = [
        {"optionId": "yes", "name": "Allow", "kind": "allow_once"},
        {"optionId": "no", "name": "Reject", "kind": "reject_once"},
    ]
    rid = 500 + n
    _send(id=rid, method="session/request_permission",
          params={"sessionId": "s1", "toolCall": call, "options": options})  # fmt: skip
    if step.get("then_exit"):  # the adapter dies with its ask still open
        raise SystemExit(0)
    outcome = _answer(rid).get("result", {}).get("outcome", {})
    if outcome.get("optionId") != "yes":
        _update(tid, status="failed")
        return
    if "write" in step:
        path, text = step["write"]
        Path(path).write_text(text, encoding="utf-8")
    _update(tid, status="completed")


def engine(scenario_path: str) -> int:
    scenario = json.loads(Path(scenario_path).read_text(encoding="utf-8"))
    for line in sys.stdin:
        msg = json.loads(line)
        method, rid = msg.get("method"), msg.get("id")
        params = msg.get("params") or {}
        if method == "initialize":
            _send(id=rid, result={"protocolVersion": 1, "agentInfo": _AGENT, "authMethods": []})
        elif method == "session/new" and "config_modes" in scenario:  # v2's spelling
            values = [{"value": v} for v in scenario["config_modes"]]
            option = {"configId": "mode", "currentValue": "auto", "options": values}
            _send(id=rid, result={"sessionId": "s1", "configOptions": [option]})
        elif method == "session/new":
            modes = scenario.get("modes", _MODES)
            _send(id=rid, result={"sessionId": "s1", "modes": modes})
        elif method == "session/set_config_option":
            _send(id=rid, result={})
        elif method == "session/set_mode":
            modes = scenario.get("modes", _MODES)
            known = {m["id"] for m in modes["availableModes"]}
            if params.get("modeId") in known:
                _send(id=rid, result={})
            else:
                _send(id=rid, error={"code": -32602, "message": "unknown mode"})
        elif method == "session/prompt":
            for n, step in enumerate(scenario.get("steps", [])):
                _step(n, step)
            _send(id=rid, result={"stopReason": scenario.get("stop", "end_turn")})
        elif rid is not None:
            _send(id=rid, error={"code": -32601, "message": f"no {method}"})
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["--exit-only"]:
        return int(argv[1])
    if argv[:1] == ["--engine"]:
        return engine(argv[1])
    if argv == ["--doctor"]:
        results = {
            "initialize": {
                "protocolVersion": 1,
                "agentInfo": {"name": "@agentclientprotocol/claude-agent-acp", "version": "9.9.9"},
                "authMethods": [],
            },
            "session/new": {
                "sessionId": "s",
                "modes": {
                    "currentModeId": "auto",
                    "availableModes": [{"id": "default"}, {"id": "auto"}],
                },
            },
        }
        for line in sys.stdin:
            msg = json.loads(line)
            if msg.get("method") in results:
                reply = {"jsonrpc": "2.0", "id": msg["id"], "result": results[msg["method"]]}
                print(json.dumps(reply), flush=True)
        return 0
    fixture, repo = argv
    with open(fixture, encoding="utf-8") as frames:
        for line in frames:
            envelope = json.loads(line)
            if envelope["dir"] == "a2c":
                print(envelope["raw"].replace("$REPO", repo), flush=True)
    sys.stdin.buffer.read()
    return 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
