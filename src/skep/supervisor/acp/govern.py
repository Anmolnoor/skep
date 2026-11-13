"""B1-3 (ADR 0054 D2/D4/D6, A1/A9/A10): ``skep acp govern`` and ``skep acp doctor``.

The one impure module of the proxy: it opens the operator's store, spawns the
agent, and answers the permission hop from ``policy_decide()``. What it cannot
decide it forwards to the human behind the client; a silent allow is the one
wrong answer, so every handler failure forwards (or, on the hard ring, refuses).
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Literal

from ... import __version__
from ..policy_paths import workspace_value
from ..serve.actions import policy_decide, policy_note
from ..serve.settings import ConfigHolder
from ..store import RunStore
from .mapper import (
    AGENT_PRESETS,
    Decided,
    Session,
    _is_init_result,
    agent_name,
    permission_probe,
    probe,
)
from .proxy import Direction, Forward, Handler, Proxy, Reply

# Terminal statuses only: codex and kiro send `in_progress` BEFORE they ask
# (fixtures codex-acp/live-v2-ask.ndjson:84->86, kiro/...-supervised.ndjson:20->25),
# so only a finished call can be known to have run unasked.
_RAN = frozenset({"completed", "failed"})
_SILENT_MODES = frozenset({"auto", "agent", "agent-full-access", "bypassPermissions"})


def _say(line: str) -> None:
    print(f"skep acp: {line}", file=sys.stderr, flush=True)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


@dataclass
class Governor:
    """One governed agent process: session state plus the handlers that decide."""

    holder: ConfigHolder | None
    store: RunStore | None
    store_error: str | None  # D2: store unreadable -> every decision forwards with this reason
    audit_dir: Path
    agent: str
    actor_flag: str | None = None
    repo_override: str | None = None
    home: str = ""
    client: str = "client"
    session: Session = field(default_factory=Session)

    def __post_init__(self) -> None:
        if self.repo_override:
            self.session.cwd = self.repo_override

    @property
    def actor(self) -> str:
        return self.actor_flag or f"acp:{self.agent}"

    def handlers(self) -> dict[tuple[Direction, str], Handler]:
        return {
            ("a2c", "session/update"): self._observe,
            ("a2c", "session/request_permission"): self._permission,
            ("a2c", "terminal/create"): self._terminal,
            ("a2c", "fs/write_text_file"): self._write,
        }

    def on_frame(self, direction: Direction, msg: dict[str, Any]) -> None:
        method, params = msg.get("method"), _dict(msg.get("params"))
        self.session.feed(direction, msg)
        if self.repo_override:  # --repo binds session/new, load and resume alike
            self.session.cwd = self.repo_override
        if direction == "c2a" and method == "initialize" and "params" in msg:
            info = _dict(params.get("clientInfo")) or _dict(params.get("info"))
            self.client = info["name"] if isinstance(info.get("name"), str) else "client"
        elif direction == "a2c" and "result" in msg and _is_init_result(msg, self.session._init_id):
            self.agent = agent_name(_dict(msg["result"]), ()) or self.agent
            self.session.agent = self.agent
            line = (
                f"governing {self.agent} (ACP v{self.session.protocol_version}) — rings: "
                f"{', '.join(self.session.rings_live())} — home {self.home}"
            )
            if self.store_error:
                line += f"; store unreadable: every decision forwards ({self.store_error})"
            _say(line)

    def _decide(
        self, item: tuple[str, str, str], cwd: str, ring: Literal["advisory", "hard"] = "advisory"
    ) -> dict[str, Any]:
        if self.holder is None or self.store is None:
            raise RuntimeError(self.store_error or "no store")
        scope, action, value = item
        return policy_decide(
            self.holder, self.store, scope=scope, action=action, value=value,
            repo=cwd or None, actor=self.actor, ring=ring,
        )  # fmt: skip

    def _observe(self, msg: dict[str, Any]) -> None:
        # A notification: a raise here passes the frame through (on_error), which is right.
        params = _dict(msg.get("params"))
        update = _dict(params.get("update"))
        tid = str(update.get("toolCallId") or "")
        if update.get("sessionUpdate") not in ("tool_call", "tool_call_update") or not tid:
            return None
        if self.session.replaying:
            return None  # B3-F3: session/load history — cached for joins, it ran in another life
        call = self.session.tool_calls[tid]
        if call.get("status") not in _RAN or tid in self.session.asked:
            return None  # observed means "ran unasked"; an asked call has its decision rows
        pr = probe(call, self.session)
        if isinstance(pr, Decided) and self.session.mark_observed(tid):
            for scope, action, value in pr.items:
                policy_note(
                    self.audit_dir, source="observed", actor=self.actor, repo=pr.cwd,
                    scope=scope, action=action, value=value, agent=self.agent,
                    session_id=params.get("sessionId"), tool_call_id=tid,
                )  # fmt: skip
        return None

    def _permission(self, msg: dict[str, Any]) -> Reply | Forward:
        params = _dict(msg.get("params"))
        tid: str | None = None
        cwd, item = self.session.cwd, None
        by_id: dict[str, Any] = {}
        try:
            options = params.get("options")
            for o in options if isinstance(options, list) else []:
                if isinstance(o, dict) and isinstance(o.get("optionId"), str):
                    by_id[o["optionId"]] = o.get("kind")
            kinds = {kind: oid for oid, kind in by_id.items()}  # by kind, never by position
            tid, pr = permission_probe(params, self.session)
            if tid:
                self.session.asked.add(tid)
            if not isinstance(pr, Decided):
                note = pr.reason
            else:
                cwd, item = pr.cwd, pr.items[0]
                records = [self._decide(i, cwd) for i in pr.items]
                if any(r["verdict"] == "deny" for r in records):
                    if "reject_once" in kinds:
                        return _selected(kinds["reject_once"])
                    return Reply({"outcome": {"outcome": "cancelled"}})
                asking = [r for r in records if r["verdict"] != "allow"]
                if asking:
                    item = (asking[0]["scope"], asking[0]["action"], asking[0]["value"])
                    note = str(asking[0]["rule_id"])
                elif "allow_once" in kinds:
                    return _selected(kinds["allow_once"])
                else:
                    note = (
                        "allow, but the agent offered no allow_once (D6: skep never grants always)"
                    )
        except Exception as exc:
            note = f"skep could not decide: {exc}"

        def answered(response: dict[str, Any] | None) -> None:
            outcome = _dict(_dict(_dict(response).get("result")).get("outcome"))
            chosen = "error" if "error" in _dict(response) else "cancelled"
            if outcome.get("outcome") == "selected":
                oid = outcome.get("optionId")
                chosen = str((by_id.get(oid) if isinstance(oid, str) else None) or oid)
            scope, action, value = item or (None, None, None)
            policy_note(
                self.audit_dir, source="forwarded", actor=f"human@{self.client}",
                repo=cwd or None, scope=scope, action=action, value=value, agent=self.agent,
                session_id=params.get("sessionId"), tool_call_id=tid, request_id=msg.get("id"),
                outcome=chosen, note=note,
            )  # fmt: skip

        return Forward(answered)

    def _terminal(self, msg: dict[str, Any]) -> Reply | None:
        params = _dict(msg.get("params"))
        if not (self.session.protocol_version == 1 and self.session.client_terminal):
            return None  # A1: the hard ring is v1 only
        try:
            command = shlex.join([params["command"], *map(str, params.get("args") or [])])
            return self._ring(("shell", "run", command), params.get("cwd") or self.session.cwd)
        except Exception as exc:
            return _refuse(f"could not decide: {exc}")

    def _write(self, msg: dict[str, Any]) -> Reply | None:
        params = _dict(msg.get("params"))
        if not (self.session.protocol_version == 1 and self.session.client_fs_write):
            return None
        try:
            value = workspace_value(params["path"], self.session.cwd)
            return self._ring(("filesystem", "write", value), self.session.cwd)
        except Exception as exc:
            return _refuse(f"could not decide: {exc}")

    def _ring(self, item: tuple[str, str, str], cwd: str) -> Reply | None:
        # A10: the hard ring has no human hop — only allow passes through.
        record = self._decide(item, cwd, ring="hard")
        if record["verdict"] == "allow":
            return None
        reason = f"{record['verdict']} by {record.get('decided_by') or record['rule_id']}"
        if record["verdict"] != "deny":
            reason += " — this ring has no human to ask, so it refuses"
        return _refuse(reason)


def _selected(option_id: Any) -> Reply:
    return Reply({"outcome": {"outcome": "selected", "optionId": option_id}})


def _refuse(reason: str) -> Reply:
    return Reply(error={"code": -32000, "message": f"skep: {reason}"})


def open_governor(
    home: Path, argv: list[str], *, repo: str | None = None, actor: str | None = None
) -> Governor:
    """The Governor over ``home``'s store; an unreadable store still governs (D2)."""
    from ..cli_cmds import build_config  # local: cli_cmds registers the acp family

    config = build_config(home, None)
    holder, store, error = None, None, None
    try:
        store = RunStore(config.db_path)
        holder = ConfigHolder(config, store)
    except Exception as exc:
        error = str(exc)
    return Governor(
        holder, store, error, config.audit_dir, agent_name({}, argv), actor, repo, str(home)
    )


class _Sink:
    """One output pipe; write and close share a lock (a Reply from the a2c thread
    and a pass-through from the c2a thread can hit the same sink). A closed pipe
    is swallowed: the other side's EOF is what ends the relay."""

    def __init__(self, stream: IO[bytes]) -> None:
        self._stream, self._lock = stream, threading.Lock()

    def write(self, data: bytes) -> None:
        with self._lock, contextlib.suppress(OSError, ValueError):
            self._stream.write(data)
            self._stream.flush()

    def close(self) -> None:
        with self._lock, contextlib.suppress(OSError, ValueError):
            self._stream.close()


def _lines(fd: int) -> Iterator[bytes]:
    """Complete lines from a raw fd. Not ``sys.stdin.buffer``: a thread parked in a
    buffered readline holds its lock, and interpreter shutdown then aborts."""
    pending = b""
    while chunk := os.read(fd, 65536):
        *done, pending = (pending + chunk).split(b"\n")
        yield from (line + b"\n" for line in done)
    if pending:
        yield pending


def run(argv: list[str], *, home: Path, repo: str | None, actor: str | None) -> int:
    """``skep acp govern``: relay stdio between our client and the agent, governed."""
    gov = open_governor(home, argv, repo=repo, actor=actor)
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        # stderr inherits ours: the agent's own diagnostics reach the client's log.
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        except OSError as exc:
            _say(f"cannot start {argv[0]}: {exc}")
            return 127

        def forward(signum: int, _frame: object) -> None:
            with contextlib.suppress(OSError):
                proc.send_signal(signum)

        for sig in saved:
            signal.signal(sig, forward)
        assert proc.stdin is not None and proc.stdout is not None
        agent_in, agent_out = _Sink(proc.stdin), proc.stdout
        proxy = Proxy(
            to_client=_Sink(sys.stdout.buffer).write,
            to_agent=agent_in.write,
            handlers=gov.handlers(),
            on_error=lambda label, exc: _say(f"{label}: {exc}"),
            on_frame=gov.on_frame,
        )

        # B3-F8: SKEP_ACP_TRACE=<file> appends every frame in the fixture envelope
        # (`{"dir","raw","t"}`), so a session inside a client can be replayed by
        # the golden tests exactly as it happened. Set it in the client's
        # provider environment; nothing is written without it.
        trace_path = os.environ.get("SKEP_ACP_TRACE")
        trace = open(trace_path, "ab") if trace_path else None  # noqa: SIM115 — closed in finally
        trace_lock = threading.Lock()

        def feed(direction: Direction, raw: bytes) -> None:
            if trace is not None:
                env = {"dir": direction, "raw": raw.decode("utf-8", "replace").rstrip("\n")}
                line = json.dumps({**env, "t": time.time()}).encode("utf-8") + b"\n"
                with trace_lock, contextlib.suppress(OSError, ValueError):
                    trace.write(line)
                    trace.flush()
            proxy.feed(direction, raw)

        def client_loop() -> None:
            with contextlib.suppress(OSError, ValueError):
                for raw in _lines(sys.stdin.fileno()):
                    feed("c2a", raw)
            agent_in.close()

        threading.Thread(target=client_loop, daemon=True).start()
        try:
            for raw in iter(agent_out.readline, b""):
                feed("a2c", raw)
        finally:
            if trace is not None:
                with trace_lock, contextlib.suppress(OSError, ValueError):
                    trace.close()
        code = proc.wait()
        return 128 - code if code < 0 else code
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)
        if gov.store is not None:
            gov.store.close()


def doctor(argv: list[str], *, home: Path) -> int:
    """``skep acp doctor``: initialize + a dry session/new; print the rings. Never prompts."""
    del home  # the doctor reads no policy; the flag is accepted for symmetry
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    except OSError as exc:
        print(f"skep acp doctor: {exc}", file=sys.stderr)
        return 1
    timer = threading.Timer(60, proc.kill)  # a silent child must not hang the doctor
    timer.start()
    try:
        init = _ask(proc, 0, "initialize", {
            "protocolVersion": 1, "clientCapabilities": {},
            "clientInfo": {"name": "skep-acp-doctor", "version": __version__},
        })  # fmt: skip
        new = _ask(proc, 1, "session/new", {"cwd": os.getcwd(), "mcpServers": []})
    except RuntimeError as exc:
        print(f"skep acp doctor: {exc}", file=sys.stderr)
        return 1
    finally:
        timer.cancel()
        # EOF first: codex-acp prints a stack trace when killed mid-handshake
        with contextlib.suppress(OSError):
            if proc.stdin:
                proc.stdin.close()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=2)
        proc.kill()
        proc.wait()
    info = _dict(init.get("agentInfo")) or _dict(init.get("info"))
    name = agent_name(init, argv)
    preset = name if name in AGENT_PRESETS else f"generic (unknown agent: {name})"
    print(f"agent: {info.get('name') or name} {info.get('version') or '?'}")
    print(f"protocol: ACP v{init.get('protocolVersion')}")
    print(f"preset: {preset}")
    modes = _dict(new.get("modes"))
    ids = [m.get("id") for m in modes.get("availableModes") or [] if isinstance(m, dict)]
    current = modes.get("currentModeId")
    if not ids:
        option = next(
            (
                o
                for o in new.get("configOptions") or []
                if isinstance(o, dict) and (o.get("configId") or o.get("id")) == "mode"
            ),
            {},
        )
        ids = [o.get("value") for o in option.get("options") or [] if isinstance(o, dict)]
        current = option.get("currentValue")
    marked = [f"{m} (default)" if m == current else str(m) for m in ids]
    print(f"modes: {', '.join(marked) or 'none advertised'}")
    print(
        "rings: advisory — the hard ring depends on the client's initialize "
        "(v1 fs/terminal capability); with none advertised, advisory only"
    )
    if current in _SILENT_MODES:
        print(
            f"WARNING: default mode {current} never asks — skep only observes "
            "until the client sets an asking mode"
        )
    return 0


def _ask(
    proc: subprocess.Popen[bytes], rid: int, method: str, params: dict[str, Any]
) -> dict[str, Any]:
    """Send one request; read lines until its response. An error or EOF raises."""
    assert proc.stdin is not None and proc.stdout is not None
    body = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
    try:
        proc.stdin.write(json.dumps(body).encode() + b"\n")
        proc.stdin.flush()
    except OSError as exc:
        raise RuntimeError(f"{method}: the agent closed its stdin ({exc})") from exc
    for raw in iter(proc.stdout.readline, b""):
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        if isinstance(msg, dict) and msg.get("id") == rid and "method" not in msg:
            if "error" in msg:
                raise RuntimeError(f"{method}: {msg['error']}")
            return _dict(msg.get("result"))
    raise RuntimeError(f"{method}: the agent exited without answering (not an ACP agent?)")


_HINT_ADAPTERS = ("claude-agent-acp", "codex-acp")


def install_hint(home: Path) -> int:
    """``skep acp install-hint``: the rows for T3 Code's Local ACP command dialog.

    T3 launches the executable directly with one literal argument per row (no
    shell, no PATH expansion), so the executable must be an absolute path.
    first-run O5: the first line says which baseline the governor answers from
    — or, with none approved, to run ``skep onboard`` first.
    """
    from ..cli_cmds import build_config  # local: cli_cmds registers the acp family
    from ..policy_baseline import baseline_status, status_line

    supervisor = build_config(home, None).home
    if baseline_status(supervisor)["state"] == "none":
        print("run skep onboard first — the governor answers from an approved baseline")
    else:
        print(status_line(supervisor))
    found = shutil.which("skep")
    if found:
        exe = str(Path(found).resolve())
    elif Path(sys.argv[0]).name == "skep":
        exe = str(Path(sys.argv[0]).resolve())
    else:
        exe = "skep"
        _say("could not resolve an absolute path to skep; put the full path in the executable row")
    for adapter in _HINT_ADAPTERS:
        args = ["acp", "govern", "--", "npx", "-y", f"@agentclientprotocol/{adapter}"]
        print(f"Local ACP command — {adapter}")
        print(f"  executable: {exe}")
        print("  arguments, one row each (B3-F1: a single row is one literal argument):")
        for arg in args:
            print(f"    {arg}")
        print()
    print(
        "or, with the adapter installed globally, the last three args become its "
        "binary name (claude-agent-acp / codex-acp)"
    )
    print("see docs/acp-governor.md")
    return 0
