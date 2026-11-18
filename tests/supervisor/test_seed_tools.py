"""v83-F14: the shipped yt_transcript seed tool + the seed lane in the forge.

The seed tool is held to the SAME contract as a forged one: the real trial
harness runs it (self_test is offline by contract), promote_tool gates it
behind the same trial + confirmed card, and only the source location
differs (the versioned package file instead of a landed branch)."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.forge import (
    ForgedPlugin,
    load_plugins,
    save_plugin,
    seed_tool_source,
    seed_tools_root,
    sync_seed_tools,
    trial_script,
    trial_verdict,
)
from skep.supervisor.mcp_client import load_mcp_servers, runner_for_config
from skep.supervisor.serve import tools as tools_module
from skep.supervisor.serve.jobs import Dispatcher
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import execute_mutation

from .card_rig import cards, live_fixture  # noqa: F401  (the `live` fixture)


def _run_harness(source: str, workdir: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, "-c", trial_script(source)],
        capture_output=True,
        text=True,
        cwd=workdir,
        timeout=120,
    )
    return {"state": "completed", "output": proc.stdout, "stderr": proc.stderr}


def _seed_module() -> Any:
    path = seed_tools_root() / "yt_transcript.py"
    spec = importlib.util.spec_from_file_location("yt_transcript_seed", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_yt_transcript_passes_the_real_trial_harness(tmp_path: Path) -> None:
    """The seed is held to the forge contract for real — offline self_test,
    JSON-RPC over stdio, no crash on bad calls."""
    ok, reason, evidence = trial_verdict(
        _run_harness(seed_tool_source("yt_transcript.py"), tmp_path)
    )
    assert ok, reason
    assert evidence["tools"] == ["get_transcript", "self_test"]
    assert "offline" in str(evidence["self_test"])


def test_yt_transcript_parsing_and_guards() -> None:
    module = _seed_module()
    # id extraction across the forms users paste
    for form in (
        "dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
    ):
        assert module.video_id(form) == "dQw4w9WgXcQ"
    with pytest.raises(ValueError, match="video id"):
        module.video_id("https://vimeo.com/12345")
    # the tool refuses non-youtube hosts ITSELF, before any fetch
    with pytest.raises(ValueError, match="non-youtube"):
        module._fetch("https://evil.example/watch?v=dQw4w9WgXcQ")
    # v122-F1: a host that merely ends in "youtube.com" is not youtube
    with pytest.raises(ValueError, match="non-youtube"):
        module._fetch("https://evilyoutube.com/watch?v=dQw4w9WgXcQ")
    with pytest.raises(ValueError, match="video id"):
        module.video_id("https://evilyoutube.com/watch?v=dQw4w9WgXcQ")
    assert module.video_id("https://m.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    # upstream drift teaches re-forging instead of mystifying (review item 7)
    assert module.caption_track_url("<html>no captions marker</html>") is None
    assert "re-forging" in module.UPSTREAM_DRIFT


def test_sync_seed_tools_registers_draft_once_and_never_resurrects(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        added = sync_seed_tools(store)
        assert "yt-transcript" in added
        record = load_plugins(store)["yt-transcript"]
        assert record.state == "draft" and record.provenance == "seed"
        assert sync_seed_tools(store) == []  # idempotent
        # A rolled-back record is the operator's decision — never resurrected.
        save_plugin(
            store,
            ForgedPlugin(
                plugin_id="yt-transcript",
                name="yt_transcript",
                purpose="x",
                state="rolled_back",
                repo="",
                rel_path="yt_transcript.py",
                task_id="",
                server_id="forge-yt-transcript",
                provenance="seed",
            ),
        )
        assert sync_seed_tools(store) == []
        assert load_plugins(store)["yt-transcript"].state == "rolled_back"
    finally:
        store.close()


def test_promote_seed_tool_trials_installs_and_registers(
    config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seed lane through promote_tool: same trial, same card, same
    activation — the source just comes from the package."""
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        runner = cast(Dispatcher, None)
        sync_seed_tools(store)

        def real_trial(
            store_: Any,
            holder_: Any,
            runner_: Any,
            *,
            source: str,
            repo: str,
            decision: Any,
            delegation_chain: Any = (),
        ) -> dict[str, Any]:
            assert repo.endswith("forge")  # the trial runs in the forge repo
            return _run_harness(source, tmp_path)

        monkeypatch.setattr(tools_module, "_forge_trial", real_trial)
        promoted = execute_mutation(
            "promote_tool",
            {"plugin_id": "yt-transcript"},
            store=store,
            holder=holder,
            runner=runner,
            actor="test",
        )
        assert promoted["state"] == "active"
        installed = config.home.parent / "tools" / "yt-transcript.py"
        assert installed.read_text(encoding="utf-8") == seed_tool_source("yt_transcript.py")
        server = load_mcp_servers(store)["forge-yt-transcript"]
        assert server.transport == "stdio"
        assert server.command == (sys.executable, str(installed))
    finally:
        store.close()


def test_get_transcript_is_listed_read_only_and_classifies_read(tmp_path: Path) -> None:
    """v122-F1: the real seed, spoken to over stdio, lists ``get_transcript``
    with ``readOnlyHint`` — a read verb plus the hint is what B09 needs, so
    the call is a read and a phone call is not a HIGH card."""
    from skep.supervisor.mcp_client import (
        MCPClient,
        MCPServerConfig,
        classify_mcp_risk,
    )

    config = MCPServerConfig(
        server_id="yt",
        transport="stdio",
        command=(sys.executable, str(seed_tools_root() / "yt_transcript.py")),
    )
    tools = {t.name: t for t in MCPClient(config, runner=runner_for_config(config)).list_tools()}
    assert tools["get_transcript"].read_only is True
    assert classify_mcp_risk(tools["get_transcript"]) == "read"


def test_an_allowed_get_transcript_call_runs_in_turn_with_no_card(
    live: Any, tmp_path: Path
) -> None:
    """v122-F1 acceptance shape: with one allow rule (a glob, so not the
    operator's by-name trust mark) a chat call to the seed runs in-turn. The
    real seed answers over stdio; a non-video argument keeps it offline."""
    from unittest import mock

    from skep.supervisor import mcp_client
    from skep.supervisor.mcp_client import MCPServerConfig, save_mcp_server
    from skep.supervisor.serve import actions

    env = live("builder")
    save_mcp_server(
        env.store,
        MCPServerConfig(
            server_id="yt",
            transport="stdio",
            command=(sys.executable, str(seed_tools_root() / "yt_transcript.py")),
        ),
    )
    actions.learn_policy_rule(
        env.store, rule_id="mcp:yt:*", action="call",
        pattern="yt:*", scope="mcp", provenance="allow-always:op",
    )  # fmt: skip
    chat = env.chat()
    env.ollama.script_tool_call(
        "call_mcp_tool",
        {"server_id": "yt", "tool": "get_transcript", "arguments": {"video": "not a video"}},
    )
    env.ollama.script_reply("done")
    with mock.patch.object(mcp_client, "runner_for_config", runner_for_config):  # the real one
        events = env.turn(chat, "summarize this video")
    assert cards(events) == []
    row = next(r for r in env.store.chat_messages(chat) if r.tool_name == "call_mcp_tool")
    assert "could not extract a video id" in row.content
