#!/usr/bin/env python
"""Tabulate what crosses the ACP wire, or convert a recording into the envelope format.

    python -I scripts/acp_fixture_scan.py scan [<dir>]
    python -I scripts/acp_fixture_scan.py convert <in.ndjson> <out.ndjson> [--cwd DIR]

(<dir> defaults to tests/fixtures/acp.)

``scan`` walks ``<dir>`` for ``*.ndjson`` and prints a markdown table keyed by
(agent, method or ``session/update`` kind) -> every dotted field path seen,
with one example value each. The agent is the file's parent directory name.

``convert`` reads a tap log (``{"dir", "raw", "t"}`` lines) or a T3 Code
transcript (``{"type": "expect_outbound"|"emit_inbound", "frame"}`` lines) and
writes the envelope format, one ``{"dir": "c2a"|"a2c", "raw": "<frame>"}`` per
line, scrubbed: the session cwd (from ``session/new``, ``--cwd``, or T3's
``<workspace>``) -> ``$REPO``, ``/Users/<name>`` and ``/home/<name>`` ->
``<home>``, the local user name -> ``<user>``, emails -> ``<email>``, and
every ``availableCommands`` list (update or ``session/new`` result) -> ``[]``;
``_auth/status_update`` and ``usage_update`` frames are dropped, and agent
message/thought chunk text -> ``"…"``. T3 frames
are kept as T3 recorded them (normalized: no ``jsonrpc``/``id``).
Stdlib only: a dev tool, run with ``python -I``.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
from pathlib import Path
from typing import Any

DEFAULT_DIR = Path("tests/fixtures/acp")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
HOME = re.compile(r"/(?:Users|home)/[^/\s\"'\\]+")
PROSE = ("agent_message_chunk", "agent_thought_chunk")
T3_DIR = {"expect_outbound": "c2a", "emit_inbound": "a2c"}


def frames(path: Path) -> list[tuple[str, dict[str, Any], str]]:
    """(dir, frame, raw text) per JSON-RPC-shaped line, unwrapping envelope or T3 lines."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        direction = obj.get("dir") or T3_DIR.get(obj.get("type", ""), "?")
        raw = line
        if "raw" in obj:
            raw = obj["raw"]
            obj = json.loads(raw)
        elif "frame" in obj:
            obj = obj["frame"]
            raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        if isinstance(obj, dict) and ("jsonrpc" in obj or "method" in obj or "id" in obj):
            out.append((direction, obj, raw))
    return out


def key_of(direction: str, frame: dict[str, Any], pending: dict[Any, str]) -> str:
    method = frame.get("method")
    if method and "id" in frame and ("params" in frame or "result" not in frame):
        pending[(direction, frame["id"])] = method
    if method is None:  # a response travels opposite to its request
        other = "a2c" if direction == "c2a" else "c2a"
        method = pending.get((other, frame.get("id")), "?")
    if frame.get("kind") == "response" or "result" in frame or "error" in frame:
        return f"{method} (error)" if "error" in frame else f"{method} (response)"
    kind = (frame.get("params") or {}).get("update", {}).get("sessionUpdate")
    return f"{method}:{kind}" if kind else method


def walk(value: Any, prefix: str, seen: dict[str, str]) -> None:
    if isinstance(value, dict) and value:
        for k, v in value.items():
            walk(v, f"{prefix}.{k}" if prefix else k, seen)
    elif isinstance(value, list) and value:
        for v in value:
            walk(v, f"{prefix}[]", seen)
    elif prefix not in seen:
        text = json.dumps(value, ensure_ascii=False)
        seen[prefix] = text if len(text) <= 60 else text[:57] + "..."


def scan(root: Path) -> str:
    table: dict[tuple[str, str], dict[str, str]] = {}
    for path in sorted(root.rglob("*.ndjson")):
        pending: dict[Any, str] = {}
        for direction, frame, _raw in frames(path):
            key = key_of(direction, frame, pending)
            body = {k: v for k, v in frame.items() if k not in ("jsonrpc", "kind")}
            walk(body, "", table.setdefault((path.parent.name, key), {}))
    rows = ["| agent | method / kind | field path | example |", "|---|---|---|---|"]
    for (agent, key), seen in sorted(table.items()):
        for field, example in sorted(seen.items()):
            cell = example.replace("|", "\\|")
            rows.append(f"| {agent} | `{key}` | `{field}` | `{cell}` |")
    return "\n".join(rows)


def scrub(text: str, cwds: list[str]) -> str:
    for cwd in sorted(set(cwds), key=len, reverse=True):
        text = text.replace(cwd, "$REPO")
    text = HOME.sub("<home>", EMAIL.sub("<email>", text))
    user = getpass.getuser()
    return re.sub(rf"\b{re.escape(user)}\b", "<user>", text) if len(user) > 3 else text


def convert(src: Path, dst: Path, cwd: str | None) -> int:
    pairs = frames(src)
    cwds = ["<workspace>"] + ([cwd] if cwd else [])
    for _direction, frame, _raw in pairs:
        if frame.get("method") == "session/new" and "params" in frame:
            cwds.append(str(frame["params"].get("cwd", "<workspace>")))
    # /tmp is /private/tmp on macOS: an agent may report either spelling.
    cwds += [os.path.realpath(c) for c in cwds if c.startswith("/")]
    cwds += [c.removeprefix("/private") for c in cwds if c.startswith("/private/")]
    lines = []
    for direction, frame, raw in pairs:
        # The operator's local skill list is personal: v1 sends it as an
        # available_commands_update, v2 also inlines it in the session/new result.
        update = (frame.get("params") or {}).get("update") or {}
        # Account plan labels and cost figures are the operator's; the mapper reads neither.
        if (
            frame.get("method") == "_auth/status_update"
            or update.get("sessionUpdate") == "usage_update"
        ):
            continue
        for holder in (update, frame.get("result")):
            if isinstance(holder, dict) and holder.get("availableCommands"):
                holder["availableCommands"] = []
                raw = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
        # Streamed prose re-spells scrubbed paths across chunks; the mapper never reads it.
        content = update.get("content")
        if update.get("sessionUpdate") in PROSE and isinstance(content, dict) and "text" in content:
            content["text"] = "…"
            raw = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
        lines.append(json.dumps({"dir": direction, "raw": scrub(raw, cwds)}, ensure_ascii=False))
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{dst}: {len(lines)} frames")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="verb", required=True)
    sc = sub.add_parser("scan", help="print the per-agent field table")
    sc.add_argument("root", nargs="?", type=Path, default=DEFAULT_DIR)
    cv = sub.add_parser("convert", help="write a scrubbed envelope-format fixture")
    cv.add_argument("src", type=Path)
    cv.add_argument("dst", type=Path)
    cv.add_argument("--cwd", help="the session cwd to scrub, if no session/new carries it")
    args = parser.parse_args(argv)
    if args.verb == "scan":
        print(scan(args.root))
        return 0
    return convert(args.src, args.dst, args.cwd)


if __name__ == "__main__":
    raise SystemExit(main())
