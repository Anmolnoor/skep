"""Phase 04 4.1 (ADR 0058): the ACP engine — the adapter inside the run's
sandbox, the governor outside it, every frame decided and every row blamed.

The adapter is ``fake_acp_agent.py --engine``: it asks before each effect and
does it only on ``allow_once``, so what reached the worktree is what skep
answered. Every run here is a real dispatch under the host sandbox.
"""

from __future__ import annotations

import dataclasses
import io
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import RunStore, SupervisorConfig, engines
from skep.supervisor.acp.govern import EngineBinding, Governor, open_governor
from skep.supervisor.acp.proxy import Proxy
from skep.supervisor.dispatch import run_task
from skep.supervisor.engines import CODING_ENGINES, resolve_engine
from skep.supervisor.ingest import IngestOutcome
from skep.supervisor.policy import external_engine_pin_refusal
from skep.supervisor.policy_schema import POLICY_DOCUMENT_SETTINGS_KEY
from skep.supervisor.serve.settings import CARD_TIMEOUT_SECONDS
from skep.worker_contract.task import ProjectContextPayload

from .conftest import predating_home, serve_client

FAKE = str(Path(__file__).with_name("fake_acp_agent.py"))
ENGINE = "acp:claude-agent-acp"
ACTOR = "acp-engine:claude-agent-acp"
VERIFY = "grep -q acp feature.py"
EDIT = {
    "ask": {"kind": "edit", "title": "Write feature.py", "locations": [{"path": "feature.py"}]},
    "write": ["feature.py", "acp = 1\n"],
}
FROB = {"kind": "execute", "title": "frobnicate", "rawInput": {"command": "frobnicate --all"}}


def _project(config: SupervisorConfig, repo: Path, **policy: Any) -> ProjectContextPayload:
    store = RunStore(predating_home(config.db_path))
    try:
        store.add_project_policy(
            project_id="acp", name="acp", strategy="trusted_local_dev", phase="build",
            policy={"verify_command": VERIFY, "default_execution_mode": "sandbox", **policy},
        )  # fmt: skip
        store.add_project_binding(
            project_id="acp", binding_kind="repo_path", binding_value=str(repo.resolve())
        )
    finally:
        store.close()
    return ProjectContextPayload(
        project_id="acp", name="acp", strategy="trusted_local_dev", phase="build",
        binding_kind="repo_path", binding_value=str(repo.resolve()),
    )  # fmt: skip


def _setting(config: SupervisorConfig, key: str, value: Any) -> None:
    store = RunStore(config.db_path)
    try:
        store.set_setting(key, value)
    finally:
        store.close()


def _dispatch(
    config: SupervisorConfig,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    steps: list[dict[str, Any]],
    *,
    modes: dict[str, Any] | None = None,
    **policy: Any,
) -> IngestOutcome:
    """One real dispatch of the ACP engine with the fake adapter playing ``steps``."""
    context = _project(config, repo, **policy)
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"steps": steps, **({"modes": modes} if modes else {})}))
    entry = CODING_ENGINES[ENGINE]
    adapter = (sys.executable, FAKE, "--engine", str(scenario))
    monkeypatch.setitem(
        CODING_ENGINES, ENGINE, dataclasses.replace(entry, argv=(*entry.argv[:3], *adapter))
    )
    monkeypatch.setattr(engines, "engine_available", lambda engine: (True, "stub"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")  # an inert name, not a key
    return run_task(
        repo, "add feature.py", config=config, execution_mode="sandbox", coding_engine=ENGINE,
        verify_command=VERIFY, project_context=context,
    )  # fmt: skip


def _rows(config: SupervisorConfig, task_id: str) -> list[dict[str, Any]]:
    path = config.audit_dir / "policy" / "decisions.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return [row for row in rows if row.get("task_id") == task_id]


def _cards(config: SupervisorConfig) -> list[Any]:
    store = RunStore(config.db_path)
    try:
        return [
            card
            for chat in store.list_chats()
            for card in store.chat_actions(chat.chat_id)
            if card.tool == "acp_permission"
        ]
    finally:
        store.close()


def _patch(config: SupervisorConfig, task_id: str) -> str:
    from skep.supervisor.serve.actions import patch_path

    store = RunStore(config.db_path)
    try:
        path = patch_path(store, task_id)
    finally:
        store.close()
    return path.read_text(encoding="utf-8") if path is not None else ""


# --- the registry entries Phase 04 4.2 (Builder 4B) relies on ---------------


@pytest.mark.parametrize(
    ("name", "twin"), [("acp:claude-agent-acp", "claude_code"), ("acp:codex-acp", "codex")]
)
def test_the_acp_engines_are_external_and_declare_their_cli_twins_auth(
    name: str, twin: str
) -> None:
    engine, cli = resolve_engine(name), CODING_ENGINES[twin]
    assert engine.external is True
    assert engine.auth_env == cli.auth_env and engine.auth_env  # 4B's auth preflight reads it
    assert engine.env_vars == cli.env_vars and engine.toolchain_env == cli.toolchain_env
    assert engine.network_host == cli.network_host
    assert engine.binary == name.removeprefix("acp:")
    assert engine.argv[1:3] == ("-m", "skep.workers.acp")
    assert external_engine_pin_refusal(name, "git diff --check") is not None  # I2: a real pin


def test_an_acp_engine_never_runs_outside_the_sandbox(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engines, "engine_available", lambda engine: (True, "stub"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    with pytest.raises(ValueError, match="never runs in 'workspace'"):
        run_task(
            repo, "x", config=config, execution_mode="workspace", coding_engine=ENGINE,
            verify_command=VERIFY,
        )  # fmt: skip


# --- a dispatched run --------------------------------------------------------


def test_a_normal_sandboxed_run_edits_and_tests_with_no_mid_run_cards(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unlocked project: the edit is the run lane's workspace write, the
    command the zero-ask step's (sandboxed=True — the engine's wall, verified
    by dispatch), and nothing cards. Every row names the run (Phase 02)."""
    outcome = _dispatch(config, repo, monkeypatch, tmp_path, [EDIT, {"ask": FROB}], unlocked=True)
    task_id = outcome.record.task_id
    assert outcome.record.state == "completed", outcome.record.summary
    assert "acp = 1" in _patch(config, task_id)
    assert _cards(config) == []
    write, shell = (row for row in _rows(config, task_id) if row["source"] != "observed")
    assert (write["scope"], write["value"], write["verdict"]) == (
        "filesystem",
        "workspace",
        "allow",
    )
    assert (shell["value"], shell["verdict"]) == ("frobnicate --all", "allow")
    assert shell["decided_by"] == "unlocked:acp/zero-ask"
    for row in (write, shell):
        assert row["engine"] == ENGINE and row["caste"] == "coding" and row["actor"] == ACTOR
        assert row["tool_call_id"].startswith("call-") and row["repo"] == str(repo.resolve())
    store = RunStore(config.db_path)
    try:
        reverified = store.reverification_for(task_id)
    finally:
        store.close()
    assert reverified is not None and reverified.confirmed  # G10 on the pinned command


def test_a_tool_call_that_never_asked_is_recorded_observed(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ls = {"kind": "execute", "title": "ls", "rawInput": {"command": "ls"}}
    outcome = _dispatch(config, repo, monkeypatch, tmp_path, [EDIT, {"observe": ls}], unlocked=True)
    (observed,) = [r for r in _rows(config, outcome.record.task_id) if r["source"] == "observed"]
    assert (observed["scope"], observed["value"]) == ("shell", "ls")
    assert "verdict" not in observed  # ran unasked: never claimed as pre-approved
    assert observed["engine"] == ENGINE and observed["caste"] == "coding"
    assert observed["tool_call_id"] == "call-1" and observed["actor"] == ACTOR
    assert observed["repo"] == str(repo.resolve())


def test_an_adapter_claimed_path_outside_the_worktree_is_refused(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The adapter names the operator's own checkout — a path it claims, not
    the run's worktree; a per-call cwd there too. Both are refused before any
    rule could allow them (the sandbox would refuse the write anyway, I9)."""
    target = repo / "existing.py"
    outside = {
        "ask": {"kind": "edit", "title": "edit", "locations": [{"path": str(target)}]},
        "write": [str(target), "pwned\n"],
    }
    there = {
        "ask": {"kind": "execute", "title": "ls", "rawInput": {"command": "ls", "cwd": str(repo)}}
    }
    outcome = _dispatch(config, repo, monkeypatch, tmp_path, [outside, there, EDIT], unlocked=True)
    assert outcome.record.state == "completed", outcome.record.summary
    assert target.read_text() == "value = 0\n"
    refused = [r for r in _rows(config, outcome.record.task_id) if r.get("verdict") == "deny"]
    assert [(r["scope"], r["layer"]) for r in refused] == [
        ("filesystem", "wall"),
        ("shell", "wall"),
    ]
    assert all("outside this run's worktree" in r["note"] for r in refused)
    assert refused[0]["value"] == str(target)


def test_an_undecidable_ask_cards_and_the_card_timeout_denies(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A locked project: the command asks, the governor forwards, the engine
    cards it, nobody answers, the timeout denies (I6) and the row says who."""
    _setting(config, CARD_TIMEOUT_SECONDS, 1)
    frob = {"ask": FROB, "write": ["frobnicated.txt", "yes\n"]}
    outcome = _dispatch(config, repo, monkeypatch, tmp_path, [frob, EDIT], unlocked=False)
    task_id = outcome.record.task_id
    assert outcome.record.state == "completed", outcome.record.summary
    (card,) = _cards(config)
    assert card.source == "gate" and card.status == "denied"
    assert card.args["task_id"] == task_id and card.args["value"] == "frobnicate --all"
    assert card.result is not None and card.result.get("auto") is True
    (forwarded,) = [r for r in _rows(config, task_id) if r["source"] == "forwarded"]
    assert forwarded["outcome"] == "reject_once" and forwarded["actor"] == "auto:card-timeout"
    assert forwarded["engine"] == ENGINE and forwarded["tool_call_id"] == "call-0"
    assert "frobnicated" not in _patch(config, task_id)


def test_a_human_confirm_on_the_card_lets_the_agent_go_on(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The card resolves on the commands faces (ADR 0050: chat + REST)."""
    client = serve_client(config)
    confirmed: list[int] = []

    def operator() -> None:
        for _ in range(600):
            proposed = [card for card in _cards(config) if card.status == "proposed"]
            if proposed:
                card = proposed[0]
                answer = client.post(f"/api/chats/{card.chat_id}/commands/{card.action_id}/confirm")
                confirmed.append(answer.status_code)
                return
            time.sleep(0.05)

    clicker = threading.Thread(target=operator, daemon=True)
    clicker.start()
    frob = {"ask": FROB, "write": ["frobnicated.txt", "yes\n"]}
    outcome = _dispatch(config, repo, monkeypatch, tmp_path, [frob, EDIT], unlocked=False)
    clicker.join(5)
    assert confirmed == [200]
    task_id = outcome.record.task_id
    (forwarded,) = [r for r in _rows(config, task_id) if r["source"] == "forwarded"]
    assert forwarded["outcome"] == "allow_once" and forwarded["actor"] == "human@skep"
    assert "frobnicated" in _patch(config, task_id)
    (card,) = _cards(config)
    assert card.status == "confirmed"


def test_an_approved_undeclared_host_fails_closed_with_an_i9_message(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The operator lane allows docs.example, but this run's proxy was never
    told about it: an allow would be a lie the wall then refuses. The ask is
    refused with the reason, never carded, and the sandbox is not widened."""
    network = {
        "scope": "network",
        "allow": [{"rule_id": "net:docs", "action": "fetch", "pattern": "docs.example"}],
    }
    _setting(config, POLICY_DOCUMENT_SETTINGS_KEY, json.dumps({"scopes": [network]}))
    fetch = {
        "ask": {"kind": "fetch", "title": "fetch", "rawInput": {"url": "https://docs.example/a"}}
    }
    outcome = _dispatch(config, repo, monkeypatch, tmp_path, [fetch, EDIT], unlocked=True)
    (row,) = [r for r in _rows(config, outcome.record.task_id) if r["scope"] == "network"]
    assert (row["verdict"], row["layer"], row["value"]) == ("deny", "wall", "docs.example")
    assert "I9" in row["note"] and "not among this run's network hosts" in row["note"]
    assert _cards(config) == []


def test_run_forensics_shows_the_acp_rows(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    from skep.cli import main

    config = dataclasses.replace(config, home=tmp_path / "home" / "supervisor")  # `--home` layout
    ls = {"kind": "execute", "title": "ls", "rawInput": {"command": "ls"}}
    outcome = _dispatch(config, repo, monkeypatch, tmp_path, [EDIT, {"observe": ls}], unlocked=True)
    capsys.readouterr()
    assert (
        main(["--home", str(config.home.parent), "run", "forensics", outcome.record.task_id]) == 0
    )
    report = capsys.readouterr().out
    assert "observed shell.run 'ls'" in report
    assert "allow filesystem.write 'workspace'" in report


def test_an_adapter_with_only_silent_modes_fails_the_run_stated(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ADR 0058 D4: a mode that never asks would leave skep only observing —
    the run fails with the reason, never runs silent (I9)."""
    silent = {"currentModeId": "bypassPermissions", "availableModes": [{"id": "bypassPermissions"}]}
    outcome = _dispatch(config, repo, monkeypatch, tmp_path, [EDIT], modes=silent, unlocked=True)
    assert outcome.record.state == "failed"
    store = RunStore(config.db_path)
    try:
        detail = store.transitions_for(outcome.record.task_id)[-1][1] or ""
    finally:
        store.close()
    assert "asking mode" in detail and "default" in detail


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("sandbox-exec") is None,
    reason="the real strict Seatbelt wall (macOS)",
)
def test_the_adapter_inside_the_strict_seatbelt_wall_cannot_read_a_home_canary(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "fakehome"
    canary = home / "Downloads" / "canary-downloads.txt"
    canary.parent.mkdir(parents=True)
    canary.write_text("CANARY\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    probe = {"read": str(canary), "into": "canary.txt"}
    outcome = _dispatch(  # ADR 0068's strict read wall
        config, repo, monkeypatch, tmp_path, [probe, EDIT], unlocked=True,
        strict_read_confinement=True,
    )  # fmt: skip
    assert outcome.record.state == "completed", outcome.record.summary
    patch = _patch(config, outcome.record.task_id)
    (seen,) = [line for line in patch.splitlines() if line.endswith("Error")]
    assert seen in ("+PermissionError", "+FileNotFoundError") and "CANARY" not in patch, patch


# --- the card's faces and the standalone face --------------------------------


def test_the_acp_card_denies_on_the_commands_face_without_touching_a_review(
    config: SupervisorConfig,
) -> None:
    client = serve_client(config)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    store = RunStore(config.db_path)
    try:
        action_id = store.add_chat_action(
            chat_id, tool="acp_permission", source="gate",
            args={"task_id": "t", "scope": "shell", "action": "run", "value": "make x"},
        )  # fmt: skip
    finally:
        store.close()
    assert client.post(f"/api/chats/{chat_id}/commands/{action_id}/deny").status_code == 200
    (card,) = _cards(config)
    assert card.status == "denied"


def test_the_card_names_what_the_agent_asked_and_its_boundary() -> None:
    from skep.supervisor.serve.cards import card_summary

    shell = card_summary("acp_permission", {"scope": "shell", "action": "run", "value": "make x"})
    assert shell["headline"] == "acp_permission — shell run make x"
    assert shell["boundary"] == "in-sandbox-locked" and "inside its sandbox" in shell["risk"]
    fetch = card_summary("acp_permission", {"scope": "network", "action": "fetch", "value": "h.io"})
    assert fetch["boundary"] == "network-egress"


def test_standalone_acp_govern_never_decides_sandboxed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Trap 6: an IDE's `skep acp govern` runs on the bare host — on an
    unlocked project the same command still forwards to the human."""
    import skep.supervisor.serve.actions as actions
    from skep.supervisor.cli_cmds import build_config
    from skep.supervisor.policy_v3 import decide as real

    repo, home = tmp_path / "repo", tmp_path / "home"
    repo.mkdir()
    config = build_config(home, None)
    _project(config, repo, unlocked=True)
    seen: list[bool] = []

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("sandboxed", False))
        return real(*args, **kwargs)

    monkeypatch.setattr(actions, "decide_v3", spy)
    gov = open_governor(home, ["claude-agent-acp"])
    assert gov.engine is None
    to_client, to_agent = io.BytesIO(), io.BytesIO()
    proxy = Proxy(to_client.write, to_agent.write, gov.handlers(), on_frame=gov.on_frame)
    ask = {"toolCallId": "t1", **FROB}
    options = [{"optionId": "y", "kind": "allow_once", "name": "y"}]
    for direction, body in [
        ("c2a", {"id": 1, "method": "initialize", "params": {"protocolVersion": 1}}),
        ("a2c", {"id": 1, "result": {"protocolVersion": 1, "agentInfo": {"name": "fx"}}}),
        ("c2a", {"id": 2, "method": "session/new", "params": {"cwd": str(repo)}}),
        ("a2c", {"id": 9, "method": "session/request_permission",
                 "params": {"sessionId": "s", "toolCall": ask, "options": options}}),
    ]:  # fmt: skip
        proxy.feed(direction, json.dumps({"jsonrpc": "2.0", **body}).encode() + b"\n")  # type: ignore[arg-type]
    assert seen and not any(seen)
    forwarded = [json.loads(x) for x in to_client.getvalue().splitlines()]
    assert forwarded[-1]["method"] == "session/request_permission"  # the human answers


def test_the_engine_governor_decides_client_side_tools_and_refuses_them(tmp_path: Path) -> None:
    """The engine's client advertises no fs/terminal: a frame asking it to run
    one anyway would run on the host. It is decided (recorded) and refused."""
    calls: list[tuple[Any, ...]] = []

    def decide(
        item: Any, cwd: str, ring: str, tid: str | None, client_side: bool
    ) -> dict[str, Any]:
        calls.append((item, ring, client_side))
        return {"verdict": "deny", "rule_id": "client-side", "decided_by": "wall/client-side"}

    workspace = tmp_path / "ws"
    workspace.mkdir()
    binding = EngineBinding(decide, "repo", {"task_id": "t", "engine": ENGINE, "caste": "coding"})
    gov = Governor(None, None, None, tmp_path, "claude-agent-acp", ACTOR, str(workspace),
                   engine=binding)  # fmt: skip
    to_client, to_agent = io.BytesIO(), io.BytesIO()
    proxy = Proxy(to_client.write, to_agent.write, gov.handlers(), on_frame=gov.on_frame)
    for body in [
        {"id": 3, "method": "terminal/create", "params": {"command": "make", "args": ["x"]}},
        {"id": 4, "method": "fs/write_text_file", "params": {"path": "a.py", "content": ""}},
    ]:
        proxy.feed("a2c", json.dumps({"jsonrpc": "2.0", **body}).encode() + b"\n")
    assert calls == [
        (("shell", "run", "make x"), "hard", True),
        (("filesystem", "write", "workspace"), "hard", True),
    ]
    replies = [json.loads(x) for x in to_agent.getvalue().splitlines()]
    assert [r["error"]["code"] for r in replies] == [-32000, -32000]
    assert to_client.getvalue() == b""  # never reached the engine's client


# --- the engine's own edges: client + governor, no dispatch -------------------


def _session(tmp_path: Path, scenario: dict[str, Any]) -> tuple[str | None, list[dict[str, Any]]]:
    """One ``AcpEngine`` session on the fake adapter, unwalled: the failure it
    states and the ledger rows it wrote."""
    from skep.supervisor.cli_cmds import build_config
    from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task
    from skep.supervisor.serve.settings import ConfigHolder
    from skep.workers.acp import AcpEngine

    config = build_config(tmp_path / "home", None)
    store = RunStore(predating_home(config.db_path))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(scenario), encoding="utf-8")
    task = mint_task(workspace=workspace, instructions="x", budget=DEFAULT_BUDGET)
    argv = [sys.executable, FAKE, "--engine", str(path)]
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd=workspace)
    try:
        engine = AcpEngine(
            proc, holder=ConfigHolder(config, store), store=store, task=task,
            adapter="claude-agent-acp", walled=False,
        )  # fmt: skip
        failure = engine.finish()
        proc.wait(10)
    finally:
        store.close()
    ledger = config.audit_dir / "policy" / "decisions.jsonl"
    rows = [json.loads(x) for x in ledger.read_text().splitlines()] if ledger.exists() else []
    return failure, rows


@pytest.mark.parametrize(("offered", "failed"), [(["default", "auto"], False), (["auto"], True)])
def test_a_v2_adapter_is_pinned_through_its_mode_config_option(
    tmp_path: Path, offered: list[str], failed: bool
) -> None:
    failure, _ = _session(tmp_path, {"config_modes": offered})
    assert (failure is not None) is failed
    assert not failed or "offers no asking mode 'default'" in str(failure)


@pytest.mark.parametrize(
    ("scenario", "said"),
    [
        ({"stop": "refusal"}, "the agent ended its turn: refusal"),
        ({"steps": [{"exit": 1}]}, "session/prompt: the adapter exited before answering"),
    ],
)
def test_a_session_that_does_not_finish_its_turn_is_a_stated_failure(
    tmp_path: Path, scenario: dict[str, Any], said: str
) -> None:
    failure, _ = _session(tmp_path, scenario)
    assert failure == said


def test_the_client_serves_no_tools_and_ignores_stray_lines(tmp_path: Path) -> None:
    """A stray line is not the protocol; a request the client does not serve
    is answered as such; a terminal it was never offered is decided by the
    governor and refused at the wall (its host would be outside the sandbox)."""
    steps = [
        {"noise": "starting up…"},
        {"noise": "[1, 2]"},
        {"request": {"method": "fs/read_text_file", "params": {"path": "a"}}},
        {"request": {"method": "terminal/create", "params": {"command": "make"}}},
    ]
    failure, rows = _session(tmp_path, {"steps": steps})
    assert failure is None
    (row,) = rows
    assert (row["value"], row["verdict"], row["decided_by"]) == ("make", "deny", "wall/client-side")
    assert "runs no tools" in row["note"] and row["actor"] == ACTOR


def test_a_card_that_cannot_be_filed_answers_cancelled_never_hangs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import skep.supervisor.serve.actions as actions

    def broken(*_: Any, **__: Any) -> str:
        raise OSError("store is gone")

    monkeypatch.setattr(actions, "propose_acp_permission", broken)
    failure, rows = _session(tmp_path, {"steps": [{"ask": FROB}]})
    assert failure is None
    (forwarded,) = [r for r in rows if r["source"] == "forwarded"]
    assert forwarded["outcome"] == "cancelled"


def test_a_timeout_loses_to_a_human_confirm_already_running(config: SupervisorConfig) -> None:
    from skep.supervisor.serve.actions import expire_acp_permission

    store = RunStore(config.db_path)
    try:
        chat = store.create_chat(title="t", model=None)
        action_id = store.add_chat_action(chat.chat_id, tool="acp_permission", args={})
        assert store.claim_chat_action(action_id)  # the human's confirm, mid-execution
        assert expire_acp_permission(store, action_id, "auto-denied: card timed out") is False
    finally:
        store.close()


def test_the_in_sandbox_worker_refuses_to_run_outside_skeps_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ES-F220: the adapter's tools skip the capability layer, so the worker
    starts it only where the spawner marked the launch as sandboxed."""
    from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task, write_task_file
    from skep.workers import acp
    from skep.workers.cli_adapter import SANDBOX_MARKER_ENV

    assert acp.main(["claude-agent-acp"]) == 2  # no --headless: a usage error
    monkeypatch.delenv(SANDBOX_MARKER_ENV, raising=False)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    task = mint_task(workspace=workspace, instructions="x", budget=DEFAULT_BUDGET)
    task_file = write_task_file(task, workspace / ".events" / "task.json")
    out = tmp_path / "result.json"
    argv = [sys.executable, FAKE, "--headless", "--task-file", str(task_file), "--out", str(out)]
    assert acp.main(argv) == 5
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["status"] == "rejected" and "outside skep's sandbox" in result["summary"]
