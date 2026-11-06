"""A stand-in ACP agent for test_acp_govern (run as a script, never imported).

fake_acp_agent.py <fixture.ndjson> <repo>   print the fixture's a2c lines, drain stdin, exit 3
fake_acp_agent.py --doctor                  answer initialize + session/new like a v1 agent
fake_acp_agent.py --exit-only N             exit N at once, never reading stdin
"""

from __future__ import annotations

import json
import sys


def main(argv: list[str]) -> int:
    if argv[:1] == ["--exit-only"]:
        return int(argv[1])
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
