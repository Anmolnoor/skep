"""The trifecta guard's inputs (ADR 0059 D5, round P4-1).

``policy_v3.decide()`` reads a chat's two taint bits; this module sets them
from what the Queen's own tools brought into the chat, and words them for a
card. The bits live on the chat row (``RunStore.add_chat_taint``) and are
sticky: nothing the model does clears them, compaction never touches them,
and a serve restart reloads them from the store.

``private``: a read_file that resolved OUTSIDE the operator roots, a
search_files with hits outside them, ANY run_shell / start_process /
read_process_log output (operator decision #3), an email read. Not: a read
inside the roots, a refused read (the secret floor, a deny, a missing path —
nothing was read), a tool that failed before producing output.

``untrusted``: what read_url, search_web and call_mcp_tool returned (every MCP
server: no server is flagged trusted yet), and every chat row whose author is
not the operator, the Queen or a tool (``RunStore.add_chat_message`` sets it
in the same write).

Reading another chat (search_chats, get_chat_messages, get_chat_context) or
an analyst's answer (delegate_analysis) carries that chat's bits into this
one — a clean chat cannot launder a tainted one.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..store import TAINT_BITS, ChatTaint, RunStore
from .settings import ConfigHolder

_PRIVATE_OUTPUT = frozenset({"run_shell", "start_process", "read_process_log"})
_UNTRUSTED_OUTPUT = frozenset({"read_url", "search_web", "call_mcp_tool"})
_CHAT_READS = frozenset({"search_chats", "get_chat_messages", "get_chat_context"})
# W21: a run read carries the worker's own words (its summary, its errors).
_RUN_READS = frozenset({"get_run", "await_runs"})
# W21: rows that carry the bits of the chat that wrote them — result key, id
# field, ``RunStore.content_taint`` kind. Approved memory is not here: the
# operator approved it (always a card, the CLI or the memory page), so it is
# operator-trusted content — only the proposal queue still carries bits.
_STORE_READS = {
    "list_notes": ("notes", "note_id", "note"),
    "list_tasks": ("tasks", "task_id", "task"),
    "list_memory_proposals": ("proposals", "proposal_id", "proposal"),
}

# How a card names what set a bit, from the tool (or the row's author).
_SOURCES = {
    "read_file": "read a file outside your folders",
    "search_files": "searched files outside your folders",
    "run_shell": "ran a shell command",
    "start_process": "started a process",
    "read_process_log": "read a process's output",
    "read_url": "fetched a web page",
    "search_web": "read web search results",
    "call_mcp_tool": "read a tool server's reply",
    "search_chats": "read another chat",
    "get_chat_messages": "read another chat",
    "get_chat_context": "read another chat",
    "delegate_analysis": "read an analyst's answer",
    "get_run": "read a worker's report",
    "await_runs": "read a worker's report",
    "list_notes": "read a note a tainted chat wrote",
    "list_tasks": "read a task a tainted chat wrote",
    "list_memory_proposals": "read memory a tainted chat filed",
}
_AUTHORS = {
    "webhook": "received a webhook message",
    "channel": "received a message from outside",
    "schedule": "received a script's output",
    "worker": "received a worker's report",
}


def _outside_roots(store: RunStore, holder: ConfigHolder, path: str) -> bool:
    from .fileio import operator_roots

    try:
        target = Path(path).expanduser().resolve()
    except (OSError, ValueError):
        return True  # cannot tell: fail closed
    return not any(
        target == root or root in target.parents for root in operator_roots(store, holder)
    )


def _home_bits(holder: ConfigHolder, path: str, *, tree: bool = False) -> set[str]:
    """W21: what a read in skep's own home brings in. The store holds every
    transcript (private); the audit directory holds transcripts and what
    workers wrote (private and untrusted); results and worktrees are worker
    output (untrusted). ``tree``: ``path`` is a search root, so an area under
    it counts too."""
    config = holder.current
    try:
        target = Path(path).expanduser().resolve()
        home = config.home.expanduser().resolve()
    except (OSError, ValueError):
        return set()
    areas = {
        home / "audit": {"private", "untrusted"},
        home / "results": {"untrusted"},
        home / "worktrees": {"untrusted"},
    }
    bits: set[str] = set()
    for area, adds in areas.items():
        if target == area or area in target.parents or (tree and target in area.parents):
            bits |= adds
    store_file = config.db_path.name  # and its -wal / -shm siblings
    if target.parent == home and target.name.startswith(store_file):
        bits.add("private")
    if tree and target in (home, *home.parents):
        bits.add("private")
    return bits


def _read_chats(name: str, args: dict[str, Any], result: dict[str, Any]) -> set[str]:
    if name == "search_chats":
        return {str(hit.get("chat_id")) for hit in result.get("hits") or [] if hit.get("chat_id")}
    if name == "delegate_analysis":
        return {str(a.get("chat_id")) for a in result.get("analyses") or [] if a.get("chat_id")}
    return {str(args.get("chat_id") or "")} - {""}


def record_tool_taint(
    store: RunStore,
    holder: ConfigHolder,
    chat_id: str,
    name: str,
    args: dict[str, Any],
    result: Any,
    message_id: int,
) -> ChatTaint:
    """Set the bits one tool result brings into ``chat_id`` (its result row is
    ``message_id``). Call it only for a tool that ran: a refusal or a denied
    card read nothing. A result that is an ``error`` read nothing either."""
    if not isinstance(result, dict) or "error" in result:
        return store.get_chat_taint(chat_id)
    private = untrusted = False
    if name == "read_file":
        path = str(args.get("path") or "")
        home = _home_bits(holder, path)
        private = "private" in home or _outside_roots(store, holder, path)
        untrusted = "untrusted" in home
    elif name == "search_files":
        path = str(args.get("path") or "")
        home = _home_bits(holder, path, tree=True) if result.get("matches") else set()
        private = bool(result.get("matches")) and (
            "private" in home or _outside_roots(store, holder, path)
        )
        untrusted = "untrusted" in home
    elif name in _RUN_READS:
        untrusted = True
    elif name in _STORE_READS:
        key, id_field, kind = _STORE_READS[name]
        rows = result.get(key) or []
        bits = store.content_taint(
            kind, [str(r.get(id_field)) for r in rows if isinstance(r, dict) and r.get(id_field)]
        )
        private, untrusted = "private" in bits, "untrusted" in bits
    elif name in _PRIVATE_OUTPUT:
        private = True
    elif name in _UNTRUSTED_OUTPUT:
        untrusted = True
        if name == "call_mcp_tool":
            from ..mcp_client import MCPTool, mcp_tool_scope_action

            tool = MCPTool(
                server_id=str(args.get("server_id") or ""),
                name=str(args.get("tool") or ""),
                description="",
            )
            # a mail server's reply is the operator's mail: private as well
            private = mcp_tool_scope_action(store, tool)[0] == "email"
    elif name in _CHAT_READS or name == "delegate_analysis":
        for other in _read_chats(name, args, result) - {chat_id}:
            seen = store.get_chat_taint(other)
            private = private or seen.private is not None
            untrusted = untrusted or seen.untrusted is not None
    if not (private or untrusted):
        return store.get_chat_taint(chat_id)
    return store.add_chat_taint(
        chat_id,
        private=message_id if private else None,
        untrusted=message_id if untrusted else None,
    )


def inherit_taint(store: RunStore, parent: str | None, child: str) -> None:
    """A chat the Queen opens for its own work (an analyst) starts with the
    parent chat's bits — a new chat is never a way to shed them."""
    if parent is None:
        return
    taint = store.get_chat_taint(parent)
    if taint.any:
        store.add_chat_taint(child, private=taint.private, untrusted=taint.untrusted)


def _source(store: RunStore, chat_id: str, message_id: int | None) -> str | None:
    if not message_id:
        return None
    rows = store.chat_messages_around(chat_id, message_id, before=0, after=0)
    row = next((r for r in rows if r.id == message_id), None)
    if row is None:
        return None
    if row.role == "tool":
        return _SOURCES.get(row.tool_name or "")
    return _AUTHORS.get(row.provenance.split(":", 1)[0])


def taint_line(store: RunStore, chat_id: str, taint: ChatTaint) -> str | None:
    """What a tainted chat's cards say about it, in plain words: what set
    each bit, then what that means. None for a clean chat."""
    if not taint.any:
        return None
    parts = []
    if taint.private is not None:
        how = _source(store, chat_id, taint.private)
        parts.append("read private data" + (f" (it {how})" if how else ""))
    if taint.untrusted is not None:
        how = _source(store, chat_id, taint.untrusted)
        parts.append("taken in outside content" + (f" (it {how})" if how else ""))
    if len(parts) == 2:
        tail = "anything it sends out or changes needs your OK, one approval at a time"
    elif taint.private is not None:
        tail = "sending anything out from it needs your OK, even where a rule or grant allows it"
    else:
        tail = "actions it takes need your OK, even where a rule or grant allows them"
    return f"this chat has {' and has '.join(parts)}, so {tail}"


class TrustRefused(ValueError):
    """W20: why /trust did not clear anything."""


def trust_chat(
    store: RunStore, audit_dir: Path, chat_id: str, bits: Iterable[str], *, actor: str
) -> dict[str, Any]:
    """W20: the operator trusts one chat again — clears ``bits`` (both when
    empty). The one way a bit is cleared, and an operator face only: no model
    tool, no channel and no unattended turn reaches it (ADR 0050 faces: the
    command deck's /trust, ``POST /api/chats/{id}/trust``, ``skep trust``).

    It records who cleared which bit, when and the message that had set it, in
    ``<audit>/policy/taint-cleared.jsonl`` and as a visible line in the chat.
    Rows already in the chat stay wrapped as outside content; new outside
    content sets the bit again."""
    from ..policy_v3 import UNATTENDED_CHATS

    if store.get_chat(chat_id) is None:
        raise TrustRefused(f"no chat {chat_id!r}")
    wanted = set(bits) or set(TAINT_BITS)
    unknown = sorted(wanted - set(TAINT_BITS))
    if unknown:
        raise TrustRefused(f"unknown taint bit(s) {unknown} — name private, untrusted or neither")
    if chat_id in UNATTENDED_CHATS:
        raise TrustRefused("an unattended turn is running in this chat — /trust after it ends")
    taint = store.get_chat_taint(chat_id)
    cleared = {bit: getattr(taint, bit) for bit in TAINT_BITS if bit in wanted}
    cleared = {bit: origin for bit, origin in cleared.items() if origin is not None}
    if not cleared:
        return {"chat_id": chat_id, "cleared": {}, "taint": taint.bits(),
                "note": "this chat carries none of those bits — nothing to clear"}  # fmt: skip
    said = ", ".join(
        f"{bit} (set by message {origin})" if origin else f"{bit} (origin unknown)"
        for bit, origin in cleared.items()
    )
    line = store.add_chat_message(
        chat_id,
        role="system",
        content=f"[{actor} trusted this chat again: cleared {said}. Rows already here stay "
        "marked as outside content; new outside content sets the bit again.]",
    )
    after = store.clear_chat_taint(chat_id, list(cleared), through=line)
    record = {
        "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "actor": actor,
        "chat_id": chat_id,
        "cleared": cleared,
        "line": line,
    }
    path = audit_dir / "policy" / "taint-cleared.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    return {"chat_id": chat_id, "cleared": cleared, "line_id": line, "taint": after.bits()}
