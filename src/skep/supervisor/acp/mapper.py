"""B1-2 (ADR 0054 D3/D5, A2/A8/A9): ACP session state and the tool-call mapper.

Decides which ``(scope, action, value)`` an ACP tool call exercises in skep's
closed vocabulary, joined on ``toolCallId``; pure, no I/O. It never turns
input it cannot read into an allow: that is ``Unreadable``, forwarded (I9).
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from ..policy_paths import workspace_value

_WRITE_KINDS = frozenset({"edit", "delete", "move"})
_NPX = frozenset({"npx", "npm", "bunx"})


@dataclass
class Session:
    """One ACP session as the proxy sees it."""

    cwd: str = ""  # D3: params.cwd of session/new (a --repo override is applied by the caller)
    protocol_version: int = 1  # from the initialize RESULT
    client_fs_write: bool = False  # v1 initialize PARAMS clientCapabilities.fs.writeTextFile
    client_terminal: bool = False  # v1 initialize PARAMS clientCapabilities.terminal
    agent: str = ""  # normalised agent name (AGENT_PRESETS key, or the real name)
    tool_calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    terminals: dict[str, str] = field(default_factory=dict)  # terminalId -> command (v2)
    observed: set[str] = field(default_factory=set)  # toolCallIds written as observed rows
    asked: set[str] = field(default_factory=set)  # toolCallIds a permission request named
    _init_id: Any = field(default=None, repr=False)

    def feed(self, direction: str, msg: Mapping[str, Any]) -> None:
        """Fold one decoded frame into the session state; other frames are ignored."""
        method = msg.get("method")
        params = _dict(msg.get("params"))
        if direction == "c2a" and method == "initialize" and "result" not in msg:
            self._init_id = msg.get("id")
            caps = _dict(params.get("clientCapabilities"))
            self.client_fs_write = _dict(caps.get("fs")).get("writeTextFile") is True
            self.client_terminal = caps.get("terminal") is True
        elif direction == "c2a" and method == "session/new":
            self.cwd = str(params.get("cwd") or "")
        elif direction == "a2c" and "result" in msg and _is_init_result(msg, self._init_id):
            result = _dict(msg.get("result"))
            version = result.get("protocolVersion")
            if isinstance(version, int):
                self.protocol_version = version
            self.agent = self.agent or agent_name(result, ())
        elif direction == "a2c" and method == "session/update":
            update = _dict(params.get("update"))
            kind = update.get("sessionUpdate")
            if kind in ("tool_call", "tool_call_update") and update.get("toolCallId"):
                tid = str(update["toolCallId"])
                patch = {k: v for k, v in update.items() if k != "sessionUpdate"}
                self.tool_calls[tid] = _merge(self.tool_calls.get(tid, {}), patch)
            elif kind == "terminal_update" and isinstance(update.get("command"), str):
                self.terminals[str(update.get("terminalId"))] = update["command"]

    def rings_live(self) -> tuple[str, ...]:
        """A1: the hard ring exists only on v1 with a client fs-write/terminal capability."""
        hard = self.protocol_version == 1 and (self.client_fs_write or self.client_terminal)
        return ("advisory", "hard") if hard else ("advisory",)

    def mark_observed(self, tool_call_id: str) -> bool:
        """True the first time ``tool_call_id`` is marked, False after."""
        if tool_call_id in self.observed:
            return False
        self.observed.add(tool_call_id)
        return True


@dataclass(frozen=True)
class Decided:
    """One ``decide()`` per item, against ``cwd``."""

    items: tuple[tuple[str, str, str], ...]
    cwd: str


@dataclass(frozen=True)
class NoOpinion:
    """Forward to the human; skep has no rule for this."""

    reason: str


@dataclass(frozen=True)
class Unreadable:
    """A kind skep knows with input it cannot read: forward with the reason (I9); never allow."""

    reason: str


Probe = Decided | NoOpinion | Unreadable


@dataclass(frozen=True)
class Preset:
    """Where one agent puts what the ACP spec leaves optional (D5: data rows, not code)."""

    command_keys: tuple[str, ...] = ("command",)
    args_key: str | None = None
    path_keys: tuple[str, ...] = ("file_path", "path")
    meta_kind: tuple[str, ...] = ()
    meta_command: tuple[str, ...] = ()
    meta_path: tuple[str, ...] = ()


AGENT_PRESETS: Mapping[str, Preset] = {
    "claude-agent-acp": Preset(),
    "codex-acp": Preset(),
    "kiro": Preset(
        meta_command=("_meta", "kiro", "command"),
        meta_path=("_meta", "kiro", "consent", "resource"),
    ),
    "grok": Preset(meta_kind=("_meta", "x.ai/tool", "kind")),
    "fx": Preset(),
    "generic": Preset(),
}


def probe(tool_call: Mapping[str, Any], session: Session) -> Probe:
    """The ``(scope, action, value)`` items a joined tool call exercises (ADR D5)."""
    agent = session.agent or "generic"
    preset = AGENT_PRESETS.get(agent, AGENT_PRESETS["generic"])
    raw = _dict(tool_call.get("rawInput"))
    kind = tool_call.get("kind") or _path(tool_call, preset.meta_kind)
    if not kind:
        return NoOpinion("no kind")
    if kind == "execute":
        command = next((raw[k] for k in preset.command_keys if isinstance(raw.get(k), str)), None)
        args = raw.get(preset.args_key) if preset.args_key else None
        if command is not None and isinstance(args, list):
            command = " ".join([command, *map(str, args)])
        command = command or session.terminals.get(str(tool_call.get("toolCallId")))
        command = command or _path(tool_call, preset.meta_command)
        if not command:
            return Unreadable(f"rawInput.command missing for kind=execute (agent {agent})")
        cwd = raw.get("cwd")  # codex: the per-call cwd D3 decides against
        return Decided(
            (("shell", "run", command),), cwd if isinstance(cwd, str) and cwd else session.cwd
        )
    if kind in _WRITE_KINDS:
        locations = tool_call.get("locations")
        paths = [
            str(loc["path"])
            for loc in (locations if isinstance(locations, list) else [])
            if isinstance(loc, dict) and loc.get("path")
        ]
        if not paths:
            first = next((raw[k] for k in preset.path_keys if isinstance(raw.get(k), str)), None)
            first = first or _path(tool_call, preset.meta_path)
            paths = [first] if first else []
        if not paths:
            return Unreadable(f"no path for kind={kind} (agent {agent})")
        items = tuple(("filesystem", "write", workspace_value(p, session.cwd)) for p in paths)
        return Decided(items, session.cwd)
    if kind == "fetch":
        host = urlsplit(str(raw.get("url") or "")).hostname
        if not host:
            return Unreadable(f"rawInput.url missing for kind=fetch (agent {agent})")
        return Decided((("network", "fetch", host),), session.cwd)
    return NoOpinion(f"kind {kind!r}: no rule in skep's vocabulary")


def permission_probe(params: Mapping[str, Any], session: Session) -> tuple[str | None, Probe]:
    """``(toolCallId, probe)`` for a ``session/request_permission``'s params (v1 or v2, A2)."""
    subject = _dict(params.get("subject"))
    inline = params.get("toolCall")
    if subject.get("type") == "command":
        tid = subject.get("toolCallId")
        command, cwd = subject.get("command"), subject.get("cwd")
        if not isinstance(command, str) or not command:
            return tid, Unreadable("command subject without a command")
        return tid, Decided((("shell", "run", command),), str(cwd or session.cwd))
    if subject.get("type") == "tool_call":
        inline = subject.get("toolCall")
    if not isinstance(inline, dict):
        return None, NoOpinion("permission request carries no tool call")
    tid = str(inline.get("toolCallId") or "")
    cached = session.tool_calls.get(tid)
    if cached is None and not inline.get("kind"):
        return tid, NoOpinion(f"toolCallId {tid} was never announced")
    joined = _merge(cached or {}, inline)
    cached_command = _dict((cached or {}).get("rawInput")).get("command")
    if isinstance(cached_command, str):  # README drift 7: codex's request shell-quotes it
        joined["rawInput"] = {**_dict(joined.get("rawInput")), "command": cached_command}
    if isinstance(params.get("_meta"), dict):  # kiro puts its detail on the request itself
        joined["_meta"] = {**_dict(joined.get("_meta")), **params["_meta"]}
    return tid, probe(joined, session)


def agent_name(initialize_result: Mapping[str, Any], argv: Sequence[str]) -> str:
    """The agent's name: v1 ``agentInfo.name`` / v2 ``info.name``, else from ``argv``."""
    info = _dict(initialize_result.get("agentInfo")) or _dict(initialize_result.get("info"))
    name = info.get("name")
    if isinstance(name, str) and name:
        return name.rsplit("/", 1)[-1]
    if not argv:
        return ""
    base = os.path.basename(argv[0])
    if base not in _NPX:
        return base
    pkg = next((a for a in argv[1:] if not a.startswith("-")), "")
    return pkg.rsplit("/", 1)[-1].split("@", 1)[0]


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _merge(old: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """Patch semantics: a non-None value replaces (lists whole); None/absent keeps the old."""
    return {**old, **{k: v for k, v in patch.items() if v is not None}}


def _path(obj: Mapping[str, Any], keys: Sequence[str]) -> str | None:
    node: Any = obj
    for key in keys:
        node = _dict(node).get(key)
    return node if keys and isinstance(node, str) and node else None


def _is_init_result(msg: Mapping[str, Any], init_id: Any) -> bool:
    # Live frames pair by id; T3's recordings name the method on the response.
    if msg.get("method") == "initialize":
        return True
    return init_id is not None and msg.get("id") == init_id and msg.get("method") is None
