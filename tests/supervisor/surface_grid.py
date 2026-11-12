"""ES-F43 (ADR 0059 P3-6): the probe grid for the Queen's tool surface.

Six home states x seven kinds of chat (a web chat in each shipped role, a
web chat whose stored role is NULL, a channel chat, an analysis chat, a
hermes-import chat) x every tool: is it offered to the model (full
delivery), and — for every mutating tool — what a call decides
(``mutation_execution_decision``, what a chat turn calls). No network: the
search and every MCP server are fakes. Only grant state ``none``: grants have
their own grid.

A channel, analysis or hermes chat gets the role the code records for one
(``roles.ROLE_LESS_CHAT_ROLE``; before P3-6 there was none, so NULL).
``tests/fixtures/policy_v3/pre_p3_6_surface_verdicts.json`` is this grid
answered BEFORE P3-6 (frozen from es-p4-2 @ ba56b63); ``test_tool_surface_v3.py``
holds the new answers to it. Regenerate only with the pre-P3-6 code, under
the test suite (``build_home`` refuses a real HOME).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from skep.supervisor import RunStore, roles
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.serve.chat import ChatEngine
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import MUTATING_TOOL_NAMES, TOOL_SPECS, mutation_execution_decision

from .net_lane_grid import build_net_home, fakes, show

HOMES = ("legacy", "zero", "builder", "assistant", "operator", "tampered")
_ROLE_LESS = getattr(roles, "ROLE_LESS_CHAT_ROLE", None)
# kind -> (stored role, source)
KINDS: dict[str, tuple[str | None, str]] = {
    "web:builder": ("builder", "web"),
    "web:operator": ("operator", "web"),
    "web:assistant": ("assistant", "web"),
    "web:null": (None, "web"),
    "channel": (_ROLE_LESS, "telegram"),
    "analysis": (_ROLE_LESS, "analysis"),
    "hermes": (_ROLE_LESS, "hermes"),
}
# The tools whose decision reads their arguments; every other one gets {}.
ARGS: dict[str, dict[str, Any]] = {
    "read_file": {"path": "{outside}/notes.txt"},
    "search_files": {"path": "{repo}", "pattern": "hi"},
    "run_shell": {"command": "ls -la", "cwd": "{outside}"},
    "start_process": {"command": "npm run dev", "cwd": "{outside}"},
    "stop_process": {"proc_id": "nope"},
    "read_url": {"url": "https://docs.python.org/3/"},
    "call_mcp_tool": {"server_id": "kb", "tool": "search"},
    "dispatch_run": {"repo": "{auto}", "instructions": "x"},
    "quick_edit": {"repo": "{auto}", "path": "a", "instructions": "x"},
    "run_code": {"repo": "{auto}", "code": "print(1)"},
    "batch_dispatch": {"tasks": [{"repo": "{auto}", "instructions": "x"}]},
    "start_research": {"repo": "{auto}", "question": "q", "source_allowlist": ["example.com"]},
    "forge_tool": {"name": "t", "purpose": "p"},
    "run_schedule_now": {"name": "nope"},
    "remember": {"content": "x"},
}
# A second, gated dispatch probe: a repo whose project does not auto-dispatch.
EXTRA: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("dispatch_run@gated", "dispatch_run", {"repo": "{repo}", "instructions": "x"}),
    ("dispatch_run@role-assistant", "dispatch_run",
     {"repo": "{auto}", "instructions": "x", "role": "assistant"}),
)  # fmt: skip


def _fill(value: Any, fmt: dict[str, str]) -> Any:
    if isinstance(value, str):
        return value.format(**fmt)
    if isinstance(value, list):
        return [_fill(v, fmt) for v in value]
    if isinstance(value, dict):
        return {k: _fill(v, fmt) for k, v in value.items()}
    return value


def rows(root: Path, kind: str) -> Iterator[tuple[str, str]]:
    """Every cell of one home: ``home|chat kind|offered|tool`` -> offered or
    hidden, ``home|chat kind|call|probe`` -> ``verdict decided_by``."""
    paths = build_net_home(root, kind)
    auto = root / "auto-repo"
    (auto / ".git").mkdir(parents=True)  # a git checkout is all the gate reads
    config = build_config(paths["personal"], None)
    fmt = {name: str(path) for name, path in paths.items()} | {"auto": str(auto.resolve())}
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="auto", name="auto", strategy="trusted_local_dev", phase="build",
            policy={"auto_dispatch_allowed": True, "default_execution_mode": "sandbox"},
        )  # fmt: skip
        store.add_project_binding(
            project_id="auto", binding_kind="repo_path", binding_value=str(auto.resolve())
        )
        store.set_setting("llm_tool_delivery", "full")
        chats = {
            name: store.create_chat(title=name, model=None, source=source, role=role).chat_id
            for name, (role, source) in KINDS.items()
        }
    finally:
        store.close()
    os.chdir(paths["outside"])
    probes = [(tool, tool, ARGS.get(tool, {})) for tool in sorted(MUTATING_TOOL_NAMES)]
    with fakes():
        store = RunStore(config.db_path)
        holder = ConfigHolder(config, store)
        engine = ChatEngine(store=store, holder=holder, runner=None, home=config.home)  # type: ignore[arg-type]
        try:
            for name, chat_id in chats.items():
                offered = {
                    t["function"]["name"] for t in engine._advertised_tools(store.get_chat(chat_id))
                }
                for spec in TOOL_SPECS:
                    tool = spec["function"]["name"]
                    shown = "offered" if tool in offered else "hidden"
                    yield f"{kind}|{name}|offered|{tool}", shown
                for probe, tool, raw in [*probes, *EXTRA]:
                    try:
                        shown = show(
                            mutation_execution_decision(
                                tool, _fill(raw, fmt), store=store, holder=holder, chat_id=chat_id
                            )
                        )
                    except Exception as exc:  # the grid records it
                        shown = f"error {type(exc).__name__}"
                    yield f"{kind}|{name}|call|{probe}", shown
        finally:
            store.close()
