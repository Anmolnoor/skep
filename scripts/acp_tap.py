#!/usr/bin/env python
"""Record what crosses the ACP stdio wire: a pass-through logger, or a minimal client.

    python -I scripts/acp_tap.py tap --log FILE -- <agent argv...>
    python -I scripts/acp_tap.py client [--v2] [--cwd DIR] [--log FILE] [--prompt TEXT]
                                        [--model ID] [--mode ID] [--timeout 180] -- <agent argv...>

``tap`` sits between a real client and the agent: our stdin -> agent stdin,
agent stdout -> our stdout, line by line, every frame logged. ``client`` is
the client: initialize, session/new, one session/prompt; it answers each
``session/request_permission`` with the first ``allow_once`` option (else
``cancelled``) and every other agent->client request with -32601 -- it never
advertises ``fs`` or ``terminal``. The log is the envelope format,
``{"dir": "c2a"|"a2c", "raw": "<frame>", "t": <epoch s>}`` per line.
Stdlib only: a dev tool, run with ``python -I``.
"""

from __future__ import annotations

import argparse
import json
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

PROMPT = "run `git status`, then create a file named probe.txt containing the word hello"
_lock = threading.Lock()


def record(log: IO[str] | None, direction: str, raw: str) -> None:
    if log is None:
        return
    with _lock:
        log.write(json.dumps({"dir": direction, "raw": raw, "t": round(time.time(), 3)}) + "\n")
        log.flush()


def tap(argv: list[str], log: IO[str] | None) -> int:
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert proc.stdin and proc.stdout
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda s, _f: proc.send_signal(s))

    def pump_in() -> None:
        assert proc.stdin
        for line in sys.stdin:
            record(log, "c2a", line.rstrip("\n"))
            proc.stdin.write(line)
            proc.stdin.flush()
        proc.stdin.close()

    threading.Thread(target=pump_in, daemon=True).start()
    for line in proc.stdout:
        record(log, "a2c", line.rstrip("\n"))
        sys.stdout.write(line)
        sys.stdout.flush()
    return proc.wait()


def client(argv: list[str], args: argparse.Namespace, log: IO[str] | None) -> int:
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert proc.stdin and proc.stdout
    inbox: queue.Queue[str | None] = queue.Queue()

    def pump_out() -> None:
        assert proc.stdout
        for line in proc.stdout:
            inbox.put(line.rstrip("\n"))
        inbox.put(None)

    threading.Thread(target=pump_out, daemon=True).start()
    deadline = time.monotonic() + args.timeout

    def send(frame: dict[str, Any]) -> None:
        assert proc.stdin
        raw = json.dumps(frame)
        record(log, "c2a", raw)
        proc.stdin.write(raw + "\n")
        proc.stdin.flush()

    def call(rid: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return read_until(lambda m: m.get("id") == rid and "method" not in m, method)

    def read_until(hit: Callable[[dict[str, Any]], bool], method: str) -> dict[str, Any]:
        while True:
            line = inbox.get(timeout=max(0.0, deadline - time.monotonic()))
            if line is None:
                raise EOFError(f"agent closed stdout while waiting for {method}")
            record(log, "a2c", line)
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if hit(msg):
                return dict(msg)
            if "method" in msg and "id" in msg:
                send(answer(msg))

    def done(code: int, note: str) -> int:
        print(note, file=sys.stderr)
        proc.kill()
        return code

    if args.v2:
        init = {
            "protocolVersion": 2,
            "info": {"name": "skep-acp-tap", "version": "0"},
            "capabilities": {},
        }
    else:
        init = {"protocolVersion": 1, "clientCapabilities": {}}
    try:
        hello = call(1, "initialize", init)
        if "error" in hello:
            return done(1, f"initialize failed: {hello['error']}")
        session = call(2, "session/new", {"cwd": str(args.cwd.resolve()), "mcpServers": []})
        if "error" in session:
            auth = (hello.get("result") or {}).get("authMethods")
            return done(1, f"session/new failed: {session['error']} (authMethods: {auth})")
        sid = session["result"]["sessionId"]
        res = session["result"]
        for rid, kind, value in ((4, "model", args.model), (5, "mode", args.mode)):
            if not value:
                continue
            # v1 advertises `models`/`modes` + set_model/set_mode; else a config option.
            listed = res.get(f"{kind}s") or {}
            opt: dict[str, Any] = next(
                (o for o in res.get("configOptions") or [] if o.get("category") == kind), {}
            )
            if listed:
                key = "modelId" if kind == "model" else "id"
                ids = [m[key] for m in listed.get(f"available{kind.title()}s", [])]
                method, params = f"session/set_{kind}", {"sessionId": sid, f"{kind}Id": value}
            else:
                ids = [o["value"] for o in opt.get("options", [])]
                cid = opt.get("configId", opt.get("id", kind))
                method, params = "session/set_config_option", {"sessionId": sid, "configId": cid}
                params |= {"type": "id", "value": value} if args.v2 else {"value": value}
            if value not in ids:
                return done(1, f"{kind} {value!r} not advertised; advertised: {ids}")
            picked = call(rid, method, params)
            if "error" in picked:
                return done(1, f"set {kind} failed: {picked['error']}")
        prompt = [{"type": "text", "text": args.prompt}]
        reply = call(3, "session/prompt", {"sessionId": sid, "prompt": prompt})
        if "result" in reply and "stopReason" not in reply["result"]:
            # v2: the response only accepts the prompt; an idle state_update ends the turn.
            idle = read_until(
                lambda m: (m.get("params") or {}).get("update", {}).get("state") == "idle",
                "state_update",
            )
            reply = {"result": idle["params"]["update"]}
    except (queue.Empty, EOFError) as exc:
        return done(1, f"stopped: {exc or 'timeout'}")
    stop = (reply.get("result") or {}).get("stopReason")
    return done(0 if stop else 1, f"session/prompt: {stop or reply.get('error')}")


def answer(request: dict[str, Any]) -> dict[str, Any]:
    rid = request["id"]
    if request["method"] != "session/request_permission":
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "error": {"code": -32601, "message": "Method not found"},
        }
    options = (request.get("params") or {}).get("options") or []
    allow = next((o["optionId"] for o in options if o.get("kind") == "allow_once"), None)
    outcome = {"outcome": "selected", "optionId": allow} if allow else {"outcome": "cancelled"}
    return {"jsonrpc": "2.0", "id": rid, "result": {"outcome": outcome}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="verb", required=True)
    tp = sub.add_parser("tap", help="stdio pass-through logger")
    cl = sub.add_parser("client", help="minimal ACP client")
    for p in (tp, cl):
        p.add_argument("--log", type=Path)
        p.add_argument("agent", nargs=argparse.REMAINDER, help="-- <agent argv...>")
    cl.add_argument("--v2", action="store_true", help="initialize with the v2 preview shape")
    cl.add_argument("--cwd", type=Path, default=Path.cwd())
    cl.add_argument("--prompt", default=PROMPT)
    cl.add_argument("--model", help="set this model on the session before the prompt")
    cl.add_argument("--mode", help="set this session mode before the prompt")
    cl.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args(argv)
    agent = args.agent[1:] if args.agent[:1] == ["--"] else args.agent
    if not agent:
        parser.error("missing -- <agent argv...>")
    log = args.log.open("a", encoding="utf-8") if args.log else None
    try:
        return tap(agent, log) if args.verb == "tap" else client(agent, args, log)
    finally:
        if log:
            log.close()


if __name__ == "__main__":
    raise SystemExit(main())
