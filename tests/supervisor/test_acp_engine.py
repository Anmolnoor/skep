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


def _project(
    config: SupervisorConfig, repo: Path, *, binding: tuple[str, str] | None = None, **policy: Any
) -> ProjectContextPayload:
    kind, value = binding or ("repo_path", str(repo.resolve()))
    store = RunStore(predating_home(config.db_path))
    try:
        store.add_project_policy(
            project_id="acp", name="acp", strategy="trusted_local_dev", phase="build",
            policy={
                "verify_command": VERIFY, "default_execution_mode": "sandbox",
                "strict_read_confinement": True, **policy,
            },
        )  # fmt: skip
        store.add_project_binding(project_id="acp", binding_kind=kind, binding_value=value)
    finally:
        store.close()
    return ProjectContextPayload(
        project_id="acp", name="acp", strategy="trusted_local_dev", phase="build",
        binding_kind=kind, binding_value=value,
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
    binding: tuple[str, str] | None = None,
    chat_id: str | None = None,
    stop: str | None = None,
    **policy: Any,
) -> IngestOutcome:
    """One real dispatch of the ACP engine with the fake adapter playing ``steps``."""
    context = _project(config, repo, binding=binding, **policy)
    scenario = tmp_path / "scenario.json"
    extra = {**({"modes": modes} if modes else {}), **({"stop": stop} if stop else {})}
    scenario.write_text(json.dumps({"steps": steps, **extra}))
    entry = CODING_ENGINES[ENGINE]
    adapter = (sys.executable, FAKE, "--engine", str(scenario))
    monkeypatch.setitem(
        CODING_ENGINES, ENGINE, dataclasses.replace(entry, argv=(*entry.argv[:3], *adapter))
    )
    monkeypatch.setattr(engines, "engine_available", lambda engine: (True, "stub"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")  # an inert name, not a key
    return run_task(
        repo, "add feature.py", config=config, execution_mode="sandbox", coding_engine=ENGINE,
        verify_command=VERIFY, project_context=context, chat_id=chat_id,
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


def test_a_session_failure_keeps_the_workers_claim_beside_the_supervisors_verdict(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A9 (I8): the agent edited, then refused its turn — the worker inside
    saw a clean exit and claims completed. That claim is kept as made, in
    the store and the audit; the supervisor's verdict (failed, why) is a
    separate record, and the run's state is the supervisor's."""
    outcome = _dispatch(config, repo, monkeypatch, tmp_path, [EDIT], stop="refusal",
                        unlocked=True)  # fmt: skip
    task_id = outcome.record.task_id
    assert outcome.record.state == "failed"
    store = RunStore(config.db_path)
    try:
        transitions = [state for state, _, _ in store.transitions_for(task_id)]
        record = store.get_run(task_id)
    finally:
        store.close()
    claimed = json.loads((config.audit_dir / task_id / "result.json").read_text())
    assert claimed["status"] == "completed"
    assert transitions[-2:] == ["completed", "failed"]
    assert record is not None and "refusal" not in (record.summary or "")
    verdict = json.loads((config.audit_dir / task_id / "supervisor_verdict.json").read_text())
    assert verdict["status"] == "failed" and verdict["worker_status"] == "completed"
    assert verdict["reason"] == "the agent ended its turn: refusal"


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
        item: Any, cwd: str, ring: str, tid: str | None, client_side: bool, url: str | None
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


def _session(
    tmp_path: Path,
    scenario: dict[str, Any],
    *,
    card_timeout: int | None = None,
    argv: list[str] | None = None,
    finish: float = 30.0,
) -> tuple[str | None, list[dict[str, Any]], list[Any]]:
    """One ``AcpEngine`` session on the fake adapter, unwalled: the failure it
    states, the ledger rows it wrote and its cards — read the moment
    ``finish`` returned."""
    from skep.supervisor.cli_cmds import build_config
    from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task
    from skep.supervisor.serve.settings import ConfigHolder
    from skep.workers.acp import AcpEngine

    config = build_config(tmp_path / "home", None)
    store = RunStore(predating_home(config.db_path))
    if card_timeout is not None:
        store.set_setting(CARD_TIMEOUT_SECONDS, card_timeout)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(scenario), encoding="utf-8")
    task = mint_task(workspace=workspace, instructions="x", budget=DEFAULT_BUDGET)
    argv = argv or [sys.executable, FAKE, "--engine", str(path)]
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd=workspace)
    try:
        engine = AcpEngine(
            proc, holder=ConfigHolder(config, store), store=store, task=task,
            adapter="claude-agent-acp", walled=False,
        )  # fmt: skip
        failure = engine.finish(finish)
        cards = [
            card
            for chat in store.list_chats()
            for card in store.chat_actions(chat.chat_id)
            if card.tool == "acp_permission"
        ]
        assert proc.stdin is not None
        proc.stdin.close()  # a stuck session's adapter ends with its EOF
        proc.wait(10)
    finally:
        store.close()
    ledger = config.audit_dir / "policy" / "decisions.jsonl"
    rows = [json.loads(x) for x in ledger.read_text().splitlines()] if ledger.exists() else []
    return failure, rows, cards


@pytest.mark.parametrize(("offered", "failed"), [(["default", "auto"], False), (["auto"], True)])
def test_a_v2_adapter_is_pinned_through_its_mode_config_option(
    tmp_path: Path, offered: list[str], failed: bool
) -> None:
    failure, _, _ = _session(tmp_path, {"config_modes": offered})
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
    failure, _, _ = _session(tmp_path, scenario)
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
    failure, rows, _ = _session(tmp_path, {"steps": steps})
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
    failure, rows, _ = _session(tmp_path, {"steps": [{"ask": FROB}]})
    assert failure is None
    (forwarded,) = [r for r in rows if r["source"] == "forwarded"]
    assert forwarded["outcome"] == "cancelled"
    assert forwarded["actor"] == "auto:engine-error"  # A13: no human answered it


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


# --- Phase 04 review R1 (4C) -------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "agent"),
    [
        (["python", "-m", "skep.workers.acp", "codex-acp"], "acp:codex-acp"),
        (["python", "-m", "skep.workers.acp", "claude-agent-acp", "--headless"],
         "acp:claude-agent-acp"),
        (["python", "-m", "skep.workers.acp", "npx", "-y", "@zed-industries/codex-acp"], "acp"),
        (["python", "-m", "skep.workers.acp", "/opt/adapters/my-acp"], "acp"),
        (["python", "/src/skep/workers/acp.py", "codex-acp"], "acp:codex-acp"),
        (["uv", "run", "python", "-m", "skep.workers.claude_code"], "claude_code"),
        (["my-wrapper", "codex-acp"], None),  # the adapter's name alone is not the engine
        (["codex-acp"], None),
    ],
)  # fmt: skip
def test_a_worker_command_names_its_acp_engine_by_the_module_it_runs(
    argv: list[str], agent: str | None
) -> None:
    """A1: keyed by the ``-m`` module, never by an argv's last token."""
    assert engines.external_agent_for_command(argv) == agent


def test_a_worker_command_running_an_unregistered_adapter_is_governed(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A1: `--worker-cmd python -m skep.workers.acp <any adapter>` gets the
    pipes, the governor and the wall like the registered engines."""
    context = _project(config, repo, unlocked=True)
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"steps": [EDIT, {"ask": FROB}]}))
    command = (sys.executable, "-m", "skep.workers.acp", sys.executable, FAKE, "--engine")
    config = dataclasses.replace(config, worker_command=(*command, str(scenario)))
    outcome = run_task(
        repo, "add feature.py", config=config, execution_mode="sandbox", verify_command=VERIFY,
        project_context=context,
    )  # fmt: skip
    assert outcome.record.state == "completed", outcome.record.summary
    rows = [r for r in _rows(config, outcome.record.task_id) if r["source"] != "observed"]
    assert [r["verdict"] for r in rows] == ["allow", "allow"]
    assert all(r["engine"].startswith("acp:") and r["actor"].startswith("acp-engine:")
               for r in rows)  # fmt: skip


@pytest.mark.parametrize("why", ["strict reads off", "sandbox off"])
def test_an_acp_run_never_starts_without_the_strict_read_wall(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    why: str,
) -> None:  # fmt: skip
    """A2 (I9, I12): the adapter's reads are the wall's, not the project's
    optional setting — refused before any run record exists."""
    if why == "sandbox off":
        config = dataclasses.replace(config, sandbox=False)
    strict = why != "strict reads off"
    with pytest.raises(ValueError, match="--strict-reads" if not strict else "confines reads"):
        _dispatch(config, repo, monkeypatch, tmp_path, [EDIT], strict_read_confinement=strict)
    store = RunStore(config.db_path)
    try:
        assert store.recent_runs() == []
    finally:
        store.close()


def test_a_template_bound_run_is_judged_by_its_project_in_its_worktree(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A3: the stack comes from the dispatched binding (a template name the
    repo path never resolves to) and the floor's cwd is the worktree."""
    import skep.supervisor.serve.actions as actions
    from skep.supervisor.policy_v3 import decide as real

    seen: list[tuple[Any, Any]] = []

    def spy(req: Any, *, stack: Any, **kwargs: Any) -> Any:
        seen.append((stack.cwd, (stack.project or {}).get("project_id")))
        return real(req, stack=stack, **kwargs)

    monkeypatch.setattr(actions, "decide_v3", spy)
    outcome = _dispatch(
        config, repo, monkeypatch, tmp_path, [EDIT, {"ask": FROB}], unlocked=True,
        binding=("template_name", "acp-tmpl"),
    )  # fmt: skip
    assert outcome.record.state == "completed", outcome.record.summary
    assert _cards(config) == []  # its unlocked project's zero-ask answered
    workspace = str(config.worktrees_root / outcome.record.task_id)
    assert seen and all(cwd == workspace and project == "acp" for cwd, project in seen)


def _wall_task(tmp_path: Path, **permissions: Any) -> Any:
    from skep.supervisor.contracts_io import DEFAULT_BUDGET, DEFAULT_PERMISSIONS, mint_task

    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    return mint_task(
        workspace=workspace, instructions="x", budget=DEFAULT_BUDGET,
        permissions=DEFAULT_PERMISSIONS.model_copy(update=permissions),
    )  # fmt: skip


@pytest.mark.parametrize(
    ("network", "loopback", "url", "walled"),
    [
        (["docs.example:443"], [], "http://docs.example:8080/a", True),  # proxy: 8080 denied
        (["docs.example:8443"], [], "https://docs.example:8443/a", False),  # declared port
        (["docs.example"], [], "https://docs.example/a", False),
        (["pypi.org"], [3000], "http://localhost:3000/", False),  # an opened loopback port
        (["pypi.org"], [], "http://localhost:3000/", True),
        (["*"], [], "http://anything.example:9/", False),
    ],
)  # fmt: skip
def test_the_network_wall_check_is_the_proxys_port_aware_admission(
    tmp_path: Path, network: list[str], loopback: list[int], url: str, walled: bool
) -> None:
    """A4: the requested authority, judged as the run's proxy would judge it."""
    from urllib.parse import urlsplit

    from skep.supervisor.serve.actions import _outside_wall

    task = _wall_task(tmp_path, network=network, loopback_ports=loopback)
    host = str(urlsplit(url).hostname)
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        reason = _outside_wall(
            store, task, ("network", "fetch", host), "", False, url=url, home=tmp_path / "h"
        )
    finally:
        store.close()
    assert (reason is not None) is walled, reason
    assert reason is None or "I9" in reason


def test_a_read_outside_the_worktree_is_judged_by_the_recorded_read_wall(
    tmp_path: Path,
) -> None:
    """A12: a read is worded as a read and checked against the run's
    recorded read allowlist, not called a write."""
    from skep.supervisor.serve.actions import _outside_wall

    task = _wall_task(tmp_path)
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        store.create_run(task, repo=tmp_path, ref="main", execution_mode="sandbox")
        detail = json.dumps({"read_allowlist": ["/opt/datasets"]})
        store.transition(task.task_id, "dispatched", detail)

        def wall(action: str, value: str) -> str | None:
            item = ("filesystem", action, value)
            return _outside_wall(store, task, item, "", False, url=None, home=tmp_path)

        assert wall("read", "/opt/datasets/a.csv") is None
        refused = wall("read", "/Users/someone/notes.txt")
        assert refused is not None and "read" in refused and "write" not in refused
        assert "/opt/datasets" in refused
        written = wall("write", "/opt/datasets/a.csv")
        assert written is not None and "write" in written
    finally:
        store.close()


def test_a_confirm_after_the_cards_deadline_is_refused_and_denies(
    config: SupervisorConfig,
) -> None:
    """A5 (I6): expiry is enforced at the claim — a late confirm loses."""
    from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task
    from skep.supervisor.serve.actions import propose_acp_permission

    client = serve_client(config)
    store = RunStore(config.db_path)
    try:
        task = mint_task(workspace=config.home, instructions="x", budget=DEFAULT_BUDGET)
        ask = {"scope": "shell", "action": "run", "value": "make x"}
        chat_id, action_id = propose_acp_permission(
            store, task, ask, chat_id=None, expires_at=time.time() - 1
        )
    finally:
        store.close()
    answer = client.post(f"/api/chats/{chat_id}/commands/{action_id}/confirm")
    assert answer.status_code == 409 and "timed out" in answer.json()["detail"]
    (card,) = _cards(config)
    assert card.status == "denied" and card.result is not None and card.result["auto"] is True


def test_a_card_stuck_executing_is_denied_once_the_deadline_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A6: a confirm that crashed mid-claim never answers; past the deadline
    the engine stops losing the claim forever and denies the ask."""
    import skep.supervisor.serve.actions as actions

    real = actions.propose_acp_permission

    def stuck(store: RunStore, *args: Any, **kwargs: Any) -> tuple[str, str]:
        chat_id, action_id = real(store, *args, **kwargs)
        assert store.claim_chat_action(action_id)  # the confirm that never finished
        return chat_id, action_id

    monkeypatch.setattr(actions, "propose_acp_permission", stuck)
    failure, rows, _ = _session(tmp_path, {"steps": [{"ask": FROB}]}, card_timeout=1, finish=20)
    assert failure is None
    (forwarded,) = [r for r in rows if r["source"] == "forwarded"]
    assert (forwarded["outcome"], forwarded["actor"]) == ("reject_once", "auto:card-timeout")


def test_an_ask_open_when_the_adapter_dies_is_denied_before_finish_returns(
    tmp_path: Path,
) -> None:
    """A7: the session's end wakes the waiting ask, and finish() joins it —
    the card is resolved by the time the store may close."""
    started = time.monotonic()
    failure, _, cards = _session(tmp_path, {"steps": [{"ask": FROB, "then_exit": True}]})
    assert failure is not None and "exited" in failure
    (card,) = cards
    assert card.status == "denied" and "the run ended" in card.result["note"]
    assert time.monotonic() - started < 15


def test_an_unmapped_tool_call_that_ran_unasked_is_recorded_observed(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A8 (I8): an ordinary read has no policy mapping, and is still a row."""
    read = {"kind": "read", "title": "Read existing.py", "locations": [{"path": "existing.py"}]}
    outcome = _dispatch(config, repo, monkeypatch, tmp_path, [EDIT, {"observe": read}],
                        unlocked=True)  # fmt: skip
    (observed,) = [r for r in _rows(config, outcome.record.task_id) if r["source"] == "observed"]
    assert (observed["kind"], observed["value"]) == ("read", "Read existing.py")
    assert observed["scope"] is None and "verdict" not in observed
    assert observed["engine"] == ENGINE and observed["tool_call_id"] == "call-1"
    assert observed["task_id"] == outcome.record.task_id and observed["actor"] == ACTOR


def test_the_cards_of_a_run_land_in_its_dispatching_chat_else_one_chat(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A10: the dispatch names the chat (a schedule's); without one, every
    ask of the run shares ONE fallback chat."""
    _setting(config, CARD_TIMEOUT_SECONDS, 1)
    two = [{"ask": FROB}, {"ask": {**FROB, "rawInput": {"command": "frob --twice"}}}]
    _dispatch(config, repo, monkeypatch, tmp_path, two, unlocked=False)
    assert len({card.chat_id for card in _cards(config)}) == 1
    store = RunStore(config.db_path)
    try:
        chat_id = store.create_chat(title="schedule chat", model=None).chat_id
    finally:
        store.close()
    before = {card.action_id for card in _cards(config)}
    shutil.rmtree(config.worktrees_root, ignore_errors=True)
    _dispatch(config, repo, monkeypatch, tmp_path, [{"ask": FROB}], chat_id=chat_id)
    (card,) = [card for card in _cards(config) if card.action_id not in before]
    assert card.chat_id == chat_id


def test_an_acp_card_whose_run_ended_is_superseded_by_the_ticker(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    """A14: its engine died with the old serve — nobody can answer it."""
    from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task
    from skep.supervisor.serve.actions import propose_acp_permission
    from skep.supervisor.serve.settings import ConfigHolder
    from skep.supervisor.serve.ticker import Ticker

    store = RunStore(config.db_path)
    try:
        ask = {"scope": "shell", "action": "run", "value": "make x"}
        tasks = []
        for _ in range(2):
            task = mint_task(workspace=tmp_path, instructions="x", budget=DEFAULT_BUDGET)
            store.create_run(task, repo=tmp_path, ref="main", execution_mode="sandbox")
            tasks.append(propose_acp_permission(store, task, ask, chat_id=None,
                                                expires_at=time.time() + 600)[1])  # fmt: skip
            store.transition(task.task_id, "running")
        store.transition(store.get_chat_action(tasks[0]).args["task_id"], "failed")  # type: ignore[union-attr]
        Ticker(ConfigHolder(config, store), store)._supersede_orphan_acp_cards()
        ended, live = (store.get_chat_action(action_id) for action_id in tasks)
    finally:
        store.close()
    assert ended is not None and ended.status == "superseded"
    assert "failed" in (ended.result or {})["note"]
    assert live is not None and live.status == "proposed"


def test_finish_states_a_session_that_did_not_end(tmp_path: Path) -> None:
    """A15: a drive thread still running is a stated failure, never None."""
    silent = [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"]
    failure, _, _ = _session(tmp_path, {}, argv=silent, finish=0.5)
    assert failure is not None and "did not end" in failure
