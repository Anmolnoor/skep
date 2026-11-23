"""ADR 0058 (Phase 04 4.1): the ACP engine — skep as the ACP client.

Two halves, one wall between them:

- **inside the sandbox** (``python -m skep.workers.acp <adapter argv…>``, the
  engine's argv, spawned by dispatch like every external engine): the
  contract worker. It runs the adapter (``claude-agent-acp`` / ``codex-acp``)
  with this process's stdin/stdout — pipes to the supervisor — then captures
  the patch exactly as the CLI engines do (``cli_adapter``). It reads no
  policy and opens no store.
- **outside it** (``AcpEngine``, started by dispatch on those pipes): the ACP
  client plus ``acp.govern.Governor`` bound to the run. Every
  ``session/request_permission``, ``terminal/create`` and
  ``fs/write_text_file`` the adapter sends is decided by ``decide()``
  against the run's own repo, role and worktree; what it cannot decide
  becomes an ``acp_permission`` card; what the agent ran without asking is
  recorded ``observed``. ``supervisor.sqlite3`` never enters the sandbox.

The sandbox (strict reads, worktree-only writes, the run's network proxy) and
G10 remain the wall even when an adapter never asks; ACP adds mediation of the
asks it does make and a per-tool record (I8, I12).
"""

from __future__ import annotations

import itertools
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from skep.workers.cli_adapter import (
    EXIT_INVOCATION_ERROR,
    UNATTENDED_PREAMBLE,
    AdapterSpec,
    run_cli_agent_task,
)
from skep.workers.worker_runtime import memory_block

if TYPE_CHECKING:
    from skep.worker_contract import CodingWorkerTask

    from ..supervisor.serve.settings import ConfigHolder
    from ..supervisor.store import RunStore

# D4: the default modes of both adapters never ask, so the engine pins one
# that does; an adapter without it fails the run (I9: fail to ask, not allow).
ASKING_MODES = {"claude-agent-acp": "default", "codex-acp": "read-only"}
_CARD_POLL_SECONDS = 0.2


def main(argv: list[str] | None = None) -> int:
    """Inside the sandbox: ``<adapter argv…> --headless --task-file F --out F``."""
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        cut = args.index("--headless")
        task_file = args[args.index("--task-file") + 1]
        out = args[args.index("--out") + 1]
    except (ValueError, IndexError):
        print("usage: python -m skep.workers.acp <adapter argv…> --headless "
              "--task-file F --out F", file=sys.stderr)  # fmt: skip
        return EXIT_INVOCATION_ERROR
    adapter = tuple(args[:cut])
    spec = AdapterSpec(
        caste="coding",
        worker_version=f"acp:{Path(adapter[0]).name if adapter else '?'}-adapter-1",
        command_env="SKEP_ACP_ADAPTER_CMD",
        default_command=adapter,
        build_argv=lambda base, _prompt: base,  # the prompt goes over the wire
        plan_steps=("drive the ACP adapter", "capture git diff"),
        requires_sandbox=True,  # its tools skip the capability layer (ADR 0047)
        inherit_stdio=True,
    )
    return run_cli_agent_task(Path(task_file), Path(out), spec)


class AcpEngine:
    """Outside the sandbox: drive one session over the worker's pipes."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        *,
        holder: ConfigHolder,
        store: RunStore,
        task: CodingWorkerTask,
        adapter: str,
        walled: bool,
    ) -> None:
        from ..supervisor.acp.govern import EngineBinding, Governor, _Sink
        from ..supervisor.acp.proxy import Proxy
        from ..supervisor.serve.actions import acp_engine_decide
        from ..supervisor.serve.settings import (
            CARD_TIMEOUT_SECONDS,
            DEFAULT_CARD_TIMEOUT,
            _stored_int,
        )

        self._process, self._store, self._task, self._adapter = process, store, task, adapter
        actor = f"acp-engine:{adapter}"

        def decide(
            item: tuple[str, str, str], cwd: str, ring: Any, tid: str | None, client_side: bool
        ) -> dict[str, Any]:
            return acp_engine_decide(
                holder, store, task, item, actor=actor, cwd=cwd, ring=ring, tool_call_id=tid,
                client_side=client_side, walled=walled,
            )  # fmt: skip

        run = store.get_run(task.task_id)
        blame = {"task_id": task.task_id, "engine": f"acp:{adapter}", "caste": task.worker_kind}
        self.governor = Governor(
            holder, store, None, holder.current.audit_dir, adapter, actor, task.workspace,
            str(holder.current.home), engine=EngineBinding(
                decide, str(run.repo) if run else task.workspace, blame
            ),
        )  # fmt: skip
        assert process.stdin is not None and process.stdout is not None
        self._agent_in = _Sink(process.stdin)
        self._proxy = Proxy(
            to_client=self._inbox,
            to_agent=self._agent_in.write,
            handlers=self.governor.handlers(),
            on_error=lambda label, exc: self._say(f"{label}: {exc}"),
            on_frame=self.governor.on_frame,
        )
        configured = _stored_int(
            store.get_setting(CARD_TIMEOUT_SECONDS), DEFAULT_CARD_TIMEOUT, minimum=0
        )
        budget = float(task.budget.wall_clock_seconds)
        self._card_timeout = min(configured, budget) if configured else budget  # I6
        self._ids = itertools.count(1)
        self._responses: dict[Any, dict[str, Any]] = {}
        self._cond = threading.Condition()
        self._closed = False
        self.failure: str | None = None
        self._thread = threading.Thread(target=self._drive, name="acp-engine", daemon=True)
        self._thread.start()

    def finish(self, timeout: float = 30.0) -> str | None:
        """Wait for the session to end; the failure the run must state, or None."""
        self._thread.join(timeout)
        return self.failure

    # --- the wire -------------------------------------------------------

    def _say(self, line: str) -> None:
        print(f"skep acp engine: {line}", file=sys.stderr, flush=True)

    def _send(self, body: dict[str, Any]) -> None:
        line = json.dumps({"jsonrpc": "2.0", **body}).encode("utf-8") + b"\n"
        self._proxy.feed("c2a", line)  # through the governor, as any client's frame

    def _read(self) -> None:
        assert self._process.stdout is not None
        try:
            for raw in iter(self._process.stdout.readline, b""):
                self._proxy.feed("a2c", raw)
        finally:
            with self._cond:
                self._closed = True
                self._cond.notify_all()

    def _inbox(self, raw: bytes) -> None:
        """What the governor let through to the client."""
        try:
            msg = json.loads(raw)
        except ValueError:
            return  # the worker's own stray line, not the protocol
        if not isinstance(msg, dict):
            return
        method, rid = msg.get("method"), msg.get("id")
        if method is None and rid is not None:
            with self._cond:
                self._responses[rid] = msg
                self._cond.notify_all()
        elif method == "session/request_permission" and rid is not None:
            threading.Thread(target=self._forwarded, args=(msg,), daemon=True).start()
        elif rid is not None:
            error = {"code": -32601, "message": f"skep's ACP engine does not serve {method}"}
            self._send({"id": rid, "error": error})

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        rid = next(self._ids)
        self._send({"id": rid, "method": method, "params": params})
        with self._cond:
            while rid not in self._responses and not self._closed:
                self._cond.wait()
            msg = self._responses.pop(rid, None)
        if msg is None:
            raise RuntimeError(f"{method}: the adapter exited before answering")
        if "error" in msg:
            raise RuntimeError(f"{method}: the adapter answered {msg['error']}")
        result = msg.get("result")
        return result if isinstance(result, dict) else {}

    # --- the session ----------------------------------------------------

    def _drive(self) -> None:
        from .. import __version__

        threading.Thread(target=self._read, name="acp-engine-read", daemon=True).start()
        try:
            self._request("initialize", {
                "protocolVersion": 1,
                # no fs/terminal: a client-side tool would run on the host (I12)
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False,
                },
                "clientInfo": {"name": "skep", "version": __version__},
            })  # fmt: skip
            new = self._request("session/new", {"cwd": self._task.workspace, "mcpServers": []})
            session_id = new.get("sessionId")
            self._pin_mode(session_id, new)
            prompt = UNATTENDED_PREAMBLE + self._task.instructions + memory_block(self._task.memory)
            result = self._request("session/prompt", {
                "sessionId": session_id, "prompt": [{"type": "text", "text": prompt}],
            })  # fmt: skip
            if result.get("stopReason") in ("refusal", "cancelled"):
                self.failure = f"the agent ended its turn: {result['stopReason']}"
        except RuntimeError as exc:
            self.failure = str(exc)
        finally:
            self._agent_in.close()  # EOF: the adapter exits, the worker takes the patch
        if self.failure:
            self._say(self.failure)

    def _pin_mode(self, session_id: Any, new: dict[str, Any]) -> None:
        from ..supervisor.acp.govern import session_modes

        want = ASKING_MODES.get(self._adapter, "default")
        ids, current, via_config = session_modes(new)
        if want not in ids:
            offered = ", ".join(map(str, ids)) or "none"
            raise RuntimeError(
                f"{self._adapter} offers no asking mode {want!r} (modes: {offered}) — skep "
                "would only observe, so the run stops (ADR 0058 D4, I9)"
            )
        if current == want:
            return
        if via_config:
            self._request("session/set_config_option",
                          {"sessionId": session_id, "configId": "mode", "value": want})  # fmt: skip
        else:
            self._request("session/set_mode", {"sessionId": session_id, "modeId": want})

    def _forwarded(self, msg: dict[str, Any]) -> None:
        try:
            self._ask_human(msg)
        except Exception as exc:  # never leave the agent waiting on an ask
            self._say(f"forwarded ask: {exc}; answered cancelled")
            self._send({"id": msg.get("id"), "result": {"outcome": {"outcome": "cancelled"}}})

    def _ask_human(self, msg: dict[str, Any]) -> None:
        """D3: a forwarded ask becomes the run's card; the answer goes back
        through the governor, whose ``forwarded`` row names who gave it."""
        from ..supervisor.acp.mapper import Decided, permission_probe
        from ..supervisor.serve.actions import (
            ACP_TIMEOUT_ACTOR,
            expire_acp_permission,
            propose_acp_permission,
        )

        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        assert isinstance(params, dict)
        kinds = {
            o.get("kind"): o.get("optionId")
            for o in params.get("options") or []
            if isinstance(o, dict)
        }
        tid, probe = permission_probe(params, self.governor.session)
        call = self.governor.session.tool_calls.get(str(tid), {})
        scope, action, value = probe.items[0] if isinstance(probe, Decided) else (None,) * 3
        ask = {
            "scope": scope, "action": action, "value": value, "tool_call_id": tid,
            "title": call.get("title"), "kind": call.get("kind"),
            "reason": None if isinstance(probe, Decided) else getattr(probe, "reason", None),
        }  # fmt: skip
        chat_id = self._store.chat_for_task(self._task.task_id)
        action_id = propose_acp_permission(self._store, self._task, ask, chat_id=chat_id)
        deadline = time.monotonic() + self._card_timeout
        timed_out = False
        while True:
            card = self._store.get_chat_action(action_id)
            if card is not None and card.status not in ("proposed", "executing"):
                break
            if self._closed or time.monotonic() > deadline:
                note = "the run ended before an answer" if self._closed else "card timed out"
                timed_out = expire_acp_permission(self._store, action_id, f"auto-denied: {note}")
                if timed_out:
                    break
            time.sleep(_CARD_POLL_SECONDS)  # ponytail: a poll — any process may answer
        allowed = not timed_out and card is not None and card.status == "confirmed"
        kind = "allow_once" if allowed else "reject_once"
        outcome: dict[str, Any] = {"outcome": "cancelled"}
        if kinds.get(kind) is not None:
            outcome = {"outcome": "selected", "optionId": kinds[kind]}
        if timed_out and self.governor.engine is not None:
            self.governor.engine.answered_by[json.dumps(msg.get("id"))] = ACP_TIMEOUT_ACTOR
        self._send({"id": msg.get("id"), "result": {"outcome": outcome}})


if __name__ == "__main__":
    raise SystemExit(main())
