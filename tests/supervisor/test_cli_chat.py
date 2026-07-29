"""v38: ``skep chat`` — the terminal face over the serve API.

Every test drives the REAL app (FastAPI ``TestClient`` is an ``httpx.Client``
over ASGI, injected into ``ServeClient``); ``FakeOllama`` scripts the model;
stdin is scripted by monkeypatching ``builtins.input``. The REPL process
never opens ``RunStore`` — assertions that need the store open it after the
fact, like every other serve test.
"""

from __future__ import annotations

import io
import re
import socket
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from skep.cli_chat import DAEMON_HINT, ServeApiError, ServeClient, iter_sse, render_turn, run_chat
from skep.supervisor import SupervisorConfig

from .conftest import serve_client
from .fake_ollama import FakeOllama


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


@pytest.fixture()
def client(config: SupervisorConfig, ollama: FakeOllama) -> TestClient:
    client = serve_client(config)
    client.put(
        "/api/llm/config",
        json={"base_url": ollama.base_url, "default_model": "qwen3", "api_key": "sk-fake"},
    )
    return client


def script_input(monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> None:
    remaining = iter(lines)

    def fake_input(prompt: str = "") -> str:
        try:
            return next(remaining)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", fake_input)


class RecordingOut(io.StringIO):
    """A stdout stand-in that records each write — deltas must arrive one by one."""

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[str] = []

    def write(self, text: str) -> int:
        self.writes.append(text)
        return super().write(text)


def test_render_turn_streams_deltas_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    out = RecordingOut()
    monkeypatch.setattr(sys, "stdout", out)
    events: list[tuple[str, dict[str, Any]]] = [
        ("message", {"content": "hello"}),
        ("message", {"content": " from"}),
        ("message", {"content": " the hive"}),
        ("done", {"state": "complete"}),
    ]
    state, actions = render_turn(iter(events))
    assert state == "complete"
    assert actions == []
    # Delta-ordered writes, not one batched string (the Hermes feel).
    assert out.writes[:3] == ["hello", " from", " the hive"]


def test_repl_sends_message_and_streams_reply(
    config: SupervisorConfig,
    client: TestClient,
    ollama: FakeOllama,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ollama.script_reply("hello from the hive")
    script_input(monkeypatch, ["what can you do?", "exit"])
    code = run_chat(home=config.home, url="http://unused", client=client)
    assert code == 0
    assert "hello from the hive" in capsys.readouterr().out
    # The turn is durable in the daemon's store, exactly like a web turn.
    chats = client.get("/api/chats").json()["chats"]
    assert chats[0]["title"].startswith("terminal ")
    assert chats[0]["source"] == "terminal"
    detail = client.get(f"/api/chats/{chats[0]['chat_id']}").json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]


def test_continue_resumes_the_newest_chat(
    config: SupervisorConfig,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client.post("/api/chats", json={"title": "older"})
    time.sleep(1.1)  # updated_at has second resolution; break the ordering tie
    client.post("/api/chats", json={"title": "newer"})
    script_input(monkeypatch, [])
    code = run_chat(home=config.home, url="http://unused", client=client, continue_latest=True)
    assert code == 0
    assert "resuming 'newer'" in capsys.readouterr().out


def test_chat_flag_summarizes_then_replay_shows_the_transcript(
    config: SupervisorConfig,
    client: TestClient,
    ollama: FakeOllama,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """v77-F5 re-pin: resuming prints ONE summary line naming what was
    withheld and the command that shows it; /replay restores the old view."""
    chat_id = client.post("/api/chats", json={"title": "old thread"}).json()["chat_id"]
    ollama.script_reply("earlier reply")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "earlier question"})
    capsys.readouterr()
    script_input(monkeypatch, [])
    code = run_chat(home=config.home, url="http://unused", client=client, chat_id=chat_id)
    assert code == 0
    out = capsys.readouterr().out
    assert "resuming 'old thread'" in out
    assert "2 messages, last " in out
    assert "/replay shows the last 20" in out
    assert "earlier question" not in out  # withheld, not silently dropped

    script_input(monkeypatch, ["/replay"])
    code = run_chat(home=config.home, url="http://unused", client=client, chat_id=chat_id)
    assert code == 0
    out = capsys.readouterr().out
    assert "earlier question" in out
    assert "earlier reply" in out


def test_daemon_down_prints_the_teaching_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # No serve token at all: the daemon never booted.
    code = run_chat(home=tmp_path / "empty-home", url="http://127.0.0.1:1")
    assert code == 1
    assert DAEMON_HINT in capsys.readouterr().err

    # Token exists but nothing listens: connection refused, same teaching line.
    home = tmp_path / "home"
    home.mkdir()
    (home / "serve-token").write_text("tok\n")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    code = run_chat(home=home, url=f"http://127.0.0.1:{port}")
    assert code == 1
    assert DAEMON_HINT in capsys.readouterr().err


def test_iter_sse_roundtrips_every_event_type(
    config: SupervisorConfig, client: TestClient, ollama: FakeOllama
) -> None:
    """The parser handles every event ``_sse()`` emits, from real streams."""
    seen: set[str] = set()

    def collect(chat_id: str, content: str) -> None:
        with client.stream(
            "POST", f"/api/chats/{chat_id}/messages", json={"content": content}
        ) as response:
            for event, data in iter_sse(response):
                assert isinstance(data, dict)
                seen.add(event)

    # thinking + message + done
    chat_a = client.post("/api/chats", json={}).json()["chat_id"]
    ollama.chat_scripts.append(
        [
            {"model": "fake", "message": {"role": "assistant", "content": "", "thinking": "hm"}},
            {"model": "fake", "message": {"role": "assistant", "content": "ok"}},
            {"model": "fake", "message": {"role": "assistant", "content": ""}, "done": True},
        ]
    )
    collect(chat_a, "think about it")

    # tool (a read tool executes inside the turn), then the wrap-up reply
    chat_b = client.post("/api/chats", json={}).json()["chat_id"]
    ollama.script_tool_call("list_runs", {})
    ollama.script_reply("no runs yet")
    collect(chat_b, "any runs?")

    # action (a mutating tool pauses into a card)
    chat_c = client.post("/api/chats", json={}).json()["chat_id"]
    ollama.script_tool_call("set_policy", {"auto_approve": True})
    collect(chat_c, "turn on auto approve")

    # error (the model backend answers 500)
    chat_d = client.post("/api/chats", json={}).json()["chat_id"]
    collect(chat_d, "this turn has no scripted reply")

    # v87-F7 adds turn_status (the wait names itself, on every face).
    assert seen == {"message", "thinking", "tool", "action", "error", "done", "turn_status"}


# ---------- F2: cards inline ----------


def _chat_actions(client: TestClient) -> list[dict[str, Any]]:
    chats = client.get("/api/chats").json()["chats"]
    return list(client.get(f"/api/chats/{chats[0]['chat_id']}").json()["actions"])


def test_card_confirm_executes_and_streams_the_continuation(
    config: SupervisorConfig,
    client: TestClient,
    ollama: FakeOllama,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ollama.script_tool_call("set_policy", {"auto_approve": True})
    ollama.script_reply("auto approve is on now")  # the verdict continuation
    script_input(monkeypatch, ["turn on auto approve", "y"])
    assert run_chat(home=config.home, url="http://unused", client=client) == 0
    out = capsys.readouterr().out
    assert "confirm: set_policy" in out
    assert "auto approve is on now" in out
    assert client.get("/api/policy").json()["auto_approve"] is True
    (action,) = _chat_actions(client)
    assert action["status"] == "confirmed"


def test_card_deny_resolves_without_executing(
    config: SupervisorConfig,
    client: TestClient,
    ollama: FakeOllama,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ollama.script_tool_call("set_policy", {"auto_approve": True})
    ollama.script_reply("okay, leaving it off")  # the model resumes after a deny too
    script_input(monkeypatch, ["turn on auto approve", "n"])
    assert run_chat(home=config.home, url="http://unused", client=client) == 0
    assert client.get("/api/policy").json()["auto_approve"] is False
    (action,) = _chat_actions(client)
    assert action["status"] == "denied"


def test_card_skip_leaves_it_pending_for_the_web_ui(
    config: SupervisorConfig,
    client: TestClient,
    ollama: FakeOllama,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ollama.script_tool_call("set_policy", {"auto_approve": True})
    # Piped stdin with no answer left: EOF at the card prompt means skip.
    script_input(monkeypatch, ["turn on auto approve"])
    assert run_chat(home=config.home, url="http://unused", client=client) == 0
    assert "card left pending" in capsys.readouterr().out
    assert client.get("/api/policy").json()["auto_approve"] is False
    (action,) = _chat_actions(client)
    assert action["status"] == "proposed"


def test_confirm_card_boxes_on_a_tty(
    config: SupervisorConfig,
    client: TestClient,
    ollama: FakeOllama,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """v77-F1: on a TTY the card gets box weight; NO_COLOR keeps the box
    (it governs color, not structure); args ride inside untruncated. Off-TTY
    the plain rendering is pinned by every existing card test."""
    from skep import cli_chat

    monkeypatch.setenv("NO_COLOR", "1")  # escapes must vanish; the box must survive
    monkeypatch.setattr(cli_chat, "_tty", lambda: True)
    ollama.script_tool_call("set_policy", {"auto_approve": True})
    ollama.script_reply("auto approve is on now")
    script_input(monkeypatch, ["turn on auto approve", "y", "exit"])
    assert run_chat(home=config.home, url="http://unused", client=client) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out
    box = [line for line in out.splitlines() if line[:1] in {"┌", "│", "└"}]
    assert box[0].startswith("┌─") and box[-1].startswith("└─")
    assert any("confirm: set_policy" in line for line in box)
    assert any('"auto_approve": true' in line for line in box)
    # The right border aligns: every box line is exactly as wide as the widest.
    assert len({len(line) for line in box}) == 1


def test_confirm_card_box_width_ignores_ansi_escapes(
    config: SupervisorConfig,
    client: TestClient,
    ollama: FakeOllama,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The build-spec snippet's bug, pinned fixed: colored lines measure by
    PLAIN width, so the right border stays aligned when escapes are present."""
    import re as re_mod

    from skep import cli_chat

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(cli_chat, "_tty", lambda: True)
    ollama.script_tool_call("set_policy", {"auto_approve": True})
    ollama.script_reply("done")
    script_input(monkeypatch, ["turn on auto approve", "y", "exit"])
    assert run_chat(home=config.home, url="http://unused", client=client) == 0
    out = capsys.readouterr().out
    assert "\x1b[36m" in out  # the tool line is colored inside the box
    plain = re_mod.sub(r"\x1b\[[0-9;]*m", "", out)
    box = [line for line in plain.splitlines() if line[:1] in {"┌", "│", "└"}]
    assert len({len(line) for line in box}) == 1


def test_pending_card_409_resolves_then_retries_the_message(
    config: SupervisorConfig,
    client: TestClient,
    ollama: FakeOllama,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ollama.script_tool_call("set_policy", {"auto_approve": True})  # turn 1 → card
    ollama.script_reply("flipped it on")  # continuation after the replayed confirm
    ollama.script_reply("and here is your answer")  # the retried message's turn
    script_input(monkeypatch, ["turn on auto approve", "s", "now answer me", "y"])
    assert run_chat(home=config.home, url="http://unused", client=client) == 0
    out = capsys.readouterr().out
    assert "card left pending" in out  # the skip
    assert "flipped it on" in out  # the 409 replay confirmed the card
    assert "and here is your answer" in out  # then the message landed
    (action,) = _chat_actions(client)
    assert action["status"] == "confirmed"
    assert client.get("/api/policy").json()["auto_approve"] is True


# ---------- F3: the command deck in the terminal ----------


def test_python_deck_and_executor_cannot_drift() -> None:
    """Every declared command has an executor branch and vice versa (the
    v25-F3 guard, third rendering)."""
    import inspect

    from skep import cli_chat

    declared = set(cli_chat.COMMANDS)
    source = inspect.getsource(cli_chat.ChatRepl.run_command)
    handled = set(re.findall(r'name == "(\w+)"', source))
    for group in re.findall(r'name in \{([^}]*)\}', source):
        handled.update(re.findall(r'"(\w+)"', group))
    assert declared == handled


def test_python_deck_matches_the_web_deck() -> None:
    """Web↔CLI parity: neither deck may quietly grow a command the other lacks.
    CLI_ONLY commands exist only in the terminal (web has equivalent surfaces:
    /status -> status page + health dot, /model -> composer select, /exit -> close tab)."""
    from skep import cli_chat
    from skep.supervisor.serve.app import STATIC_DIR

    source = (STATIC_DIR / "app.js").read_text()
    start = source.index("const COMMANDS = {")
    table = source[start : source.index("};", start)]
    web = set(re.findall(r"^  (\w+): \{", table, flags=re.MULTILINE))
    # CLI_ONLY commands are terminal-only; the rest must match exactly.
    assert {"status", "model", "exit", "replay"} == cli_chat.CLI_ONLY
    assert set(cli_chat.COMMANDS) - cli_chat.CLI_ONLY == web


def test_help_lists_every_command(
    config: SupervisorConfig,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from skep.cli_chat import COMMANDS

    script_input(monkeypatch, ["/help"])
    assert run_chat(home=config.home, url="http://unused", client=client) == 0
    out = capsys.readouterr().out
    for spec in COMMANDS.values():
        assert spec["usage"] in out


def test_deck_read_command_renders_runs(
    repo: Path,
    config: SupervisorConfig,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from .conftest import wait_terminal

    response = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "workspace",
        },
    )
    task_id = str(response.json()["task_id"])
    wait_terminal(client, task_id)
    script_input(monkeypatch, ["/runs 5"])
    assert run_chat(home=config.home, url="http://unused", client=client) == 0
    out = capsys.readouterr().out
    assert task_id in out
    assert "completed" in out


def test_deck_approve_resolves_a_review_end_to_end(
    repo: Path,
    config: SupervisorConfig,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """/approve proposes and confirms in the same breath (v63-F1: the typed
    review id IS the decision — no second prompt), and the approval resolves
    under actor operator-command — byte-identical audit to the web deck."""
    from skep.supervisor import RunStore

    from .conftest import git, wait_terminal

    response = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "workspace",
        },
    )
    task_id = str(response.json()["task_id"])
    assert wait_terminal(client, task_id)["state"] == "completed"
    review_id = client.post(f"/api/runs/{task_id}/approvals").json()["review_id"]

    script_input(monkeypatch, [f"/approve {review_id}"])
    assert run_chat(home=config.home, url="http://unused", client=client) == 0
    out = capsys.readouterr().out
    assert "confirm: approve_review" in out
    assert "applied" in out
    assert f"skep/{task_id}" in git(repo, "branch", "--list", f"skep/{task_id}").stdout
    store = RunStore(config.db_path)
    try:
        approvals = store.approvals_for(task_id)
    finally:
        store.close()
    assert [a.status for a in approvals] == ["approved"]
    assert approvals[0].resolved_by == "operator-command"


def test_deck_deny_cancel_leaves_nothing_executed(
    config: SupervisorConfig,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script_input(monkeypatch, ["/phase ghost-project maintain", "n"])
    assert run_chat(home=config.home, url="http://unused", client=client) == 0
    assert "canceled" in capsys.readouterr().out
    (action,) = _chat_actions(client)
    assert action["source"] == "operator"
    assert action["status"] == "denied"


def test_unknown_command_points_at_help(
    config: SupervisorConfig,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script_input(monkeypatch, ["/warp 9"])
    assert run_chat(home=config.home, url="http://unused", client=client) == 0
    assert "unknown command: /warp" in capsys.readouterr().out


# ---------- v51-F0: /approve and /deny accept the pending-card id ----------


def _skip_a_card(
    config: SupervisorConfig,
    client: TestClient,
    ollama: FakeOllama,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """One session that leaves a set_policy card pending; returns its row."""
    ollama.script_tool_call("set_policy", {"auto_approve": True})
    script_input(monkeypatch, ["turn on auto approve"])  # EOF at the card = skip
    assert run_chat(home=config.home, url="http://unused", client=client) == 0
    (action,) = _chat_actions(client)
    assert action["status"] == "proposed"
    return dict(action)


def test_deck_approve_accepts_the_pending_card_id(
    config: SupervisorConfig,
    client: TestClient,
    ollama: FakeOllama,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The exact id the pending-card hint prints confirms the card — the
    field-test gap: /approve only spoke review-ids, the hint printed card ids."""
    action = _skip_a_card(config, client, ollama, monkeypatch)
    # v50-F3 + v51-F0: the hint names the command that now accepts the id.
    assert f"/approve {action['action_id']}" in capsys.readouterr().out
    ollama.script_reply("auto approve is on now")  # the verdict continuation
    script_input(monkeypatch, [f"/approve {action['action_id']}"])
    assert (
        run_chat(home=config.home, url="http://unused", client=client, continue_latest=True)
        == 0
    )
    assert "auto approve is on now" in capsys.readouterr().out
    assert client.get("/api/policy").json()["auto_approve"] is True
    (resolved,) = _chat_actions(client)
    assert resolved["status"] == "confirmed"


def test_deck_deny_accepts_the_pending_card_id(
    config: SupervisorConfig,
    client: TestClient,
    ollama: FakeOllama,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    action = _skip_a_card(config, client, ollama, monkeypatch)
    ollama.script_reply("okay, leaving it off")
    script_input(monkeypatch, [f"/deny {action['action_id']}"])
    assert (
        run_chat(home=config.home, url="http://unused", client=client, continue_latest=True)
        == 0
    )
    assert client.get("/api/policy").json()["auto_approve"] is False
    (resolved,) = _chat_actions(client)
    assert resolved["status"] == "denied"


def test_web_deck_approve_resolves_card_ids_too(config: SupervisorConfig) -> None:
    """Lockstep: the web deck grew the same id-resolution branch, from the
    same COMMANDS usage strings (test_python_deck_matches_the_web_deck pins
    the table; this pins the behavior)."""
    from skep.supervisor.serve.app import STATIC_DIR

    source = (STATIC_DIR / "app.js").read_text()
    assert "resolvePendingCardById" in source
    assert 'await resolvePendingCardById(reviewId, "confirm")' in source
    assert 'await resolvePendingCardById(reviewId, "deny")' in source


# ---------- v67-F3 (R12b): /btw asks beside the work ----------


def test_btw_streams_a_read_only_answer_and_cards_nothing(
    config: SupervisorConfig,
    client: TestClient,
    ollama: FakeOllama,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """/btw is a read-only side question: the answer streams, no card exists
    afterwards, and a mutation attempt is refused inside the turn."""
    ollama.script_tool_call("set_policy", {"auto_approve": True})
    ollama.script_reply("a side question cannot change policy")
    script_input(monkeypatch, ["/btw turn on auto approve please", "exit"])
    assert run_chat(home=config.home, url="http://unused", client=client) == 0
    assert "a side question cannot change policy" in capsys.readouterr().out
    assert client.get("/api/policy").json()["auto_approve"] is False
    assert _chat_actions(client) == []


# ---------- v63-F1: /approve works across chats and over oneshot ----------


def test_oneshot_approve_resolves_a_card_from_another_chat(
    config: SupervisorConfig,
    client: TestClient,
    ollama: FakeOllama,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The observed regress (field test 2026-07-19): flagless --oneshot mints
    a fresh chat, so `/approve <card-id>` never found the hint's card and
    minted a NEW approve_review card its EOF-as-skip stdin could not answer —
    forever. The exact id is unambiguous: find it in ITS chat and resolve."""
    action = _skip_a_card(config, client, ollama, monkeypatch)
    ollama.script_reply("auto approve is on now")  # the verdict continuation
    script_input(monkeypatch, [])  # oneshot: no one is at the keyboard
    code = run_chat(
        home=config.home,
        url="http://unused",
        client=client,
        oneshot=f"/approve {action['action_id']}",
    )
    assert code == 0
    assert client.get("/api/policy").json()["auto_approve"] is True
    rows = [
        a
        for chat in client.get("/api/chats").json()["chats"]
        for a in client.get(f"/api/chats/{chat['chat_id']}").json()["actions"]
    ]
    (resolved,) = rows  # no second card was minted anywhere
    assert resolved["action_id"] == action["action_id"]
    assert resolved["status"] == "confirmed"


def test_oneshot_approve_review_id_applies_without_prompt(
    repo: Path,
    config: SupervisorConfig,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Scripted approval end-to-end: the typed review id is the confirmation,
    so a stdin that can answer nothing still lands the patch."""
    from skep.supervisor import RunStore

    from .conftest import git, wait_terminal

    response = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "workspace",
        },
    )
    task_id = str(response.json()["task_id"])
    assert wait_terminal(client, task_id)["state"] == "completed"
    review_id = client.post(f"/api/runs/{task_id}/approvals").json()["review_id"]

    script_input(monkeypatch, [])  # EOF everywhere: cron-shaped stdin
    code = run_chat(
        home=config.home, url="http://unused", client=client, oneshot=f"/approve {review_id}"
    )
    assert code == 0
    assert f"skep/{task_id}" in git(repo, "branch", "--list", f"skep/{task_id}").stdout
    store = RunStore(config.db_path)
    try:
        approvals = store.approvals_for(task_id)
    finally:
        store.close()
    assert [a.status for a in approvals] == ["approved"]
    assert approvals[0].resolved_by == "operator-command"


# ---------- F4: run telemetry inline ----------


def test_confirmed_dispatch_auto_tails_to_the_terminal_state(
    repo: Path,
    config: SupervisorConfig,
    client: TestClient,
    ollama: FakeOllama,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ollama.script_tool_call(
        "dispatch_run",
        {
            "repo": str(repo),
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "workspace",
        },
    )
    ollama.script_reply("dispatched — watching it for you")
    script_input(monkeypatch, ["fix the bug", "y"])
    assert run_chat(home=config.home, url="http://unused", client=client) == 0
    out = capsys.readouterr().out
    assert "confirm: dispatch_run" in out
    assert "watching" in out
    assert "worker started" in out
    assert "verification: passed" in out
    runs = client.get("/api/runs").json()["runs"]
    assert [r["state"] for r in runs] == ["completed"]
    assert f"run {runs[0]['task_id']}: completed" in out


def test_gated_run_prompts_and_approve_tails_the_successor(
    repo: Path,
    config: SupervisorConfig,
    client: TestClient,
    ollama: FakeOllama,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ollama.script_tool_call(
        "dispatch_run",
        {
            "repo": str(repo),
            "instructions": "Commit this. MODE:pending",
            "execution_mode": "workspace",
        },
    )
    ollama.script_reply("dispatched the gated run")
    script_input(monkeypatch, ["do the gated work", "y", "a"])
    assert run_chat(home=config.home, url="http://unused", client=client) == 0
    out = capsys.readouterr().out
    assert "approval needed: git_commit" in out
    assert "resuming as" in out
    # The original is superseded by the resumed successor (v19-F8).
    runs = {r["task_id"]: r["state"] for r in client.get("/api/runs").json()["runs"]}
    assert sorted(runs.values()) == ["completed", "superseded"]
    successor = next(t for t, s in runs.items() if s == "completed")
    assert f"run {successor}: completed" in out


def test_gated_run_deny_returns_to_the_prompt(
    repo: Path,
    config: SupervisorConfig,
    client: TestClient,
    ollama: FakeOllama,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ollama.script_tool_call(
        "dispatch_run",
        {
            "repo": str(repo),
            "instructions": "Commit this. MODE:pending",
            "execution_mode": "workspace",
        },
    )
    ollama.script_reply("dispatched")
    script_input(monkeypatch, ["do the gated work", "y", "d"])
    assert run_chat(home=config.home, url="http://unused", client=client) == 0
    assert "denied: task" in capsys.readouterr().out
    approvals = client.get("/api/approvals").json()["approvals"]
    assert approvals == []  # resolved — nothing left pending


# ---------- F5: entry banner + --oneshot ----------


def test_banner_reports_pending_counts(
    repo: Path,
    config: SupervisorConfig,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from .conftest import wait_terminal

    response = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "workspace",
        },
    )
    task_id = str(response.json()["task_id"])
    wait_terminal(client, task_id)
    client.post(f"/api/runs/{task_id}/approvals")  # 1 approval waiting
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    client.post(  # 1 card pending
        f"/api/chats/{chat_id}/commands",
        json={"tool": "deny_review", "args": {"review_id": "r-1"}},
    )
    script_input(monkeypatch, [])
    assert run_chat(home=config.home, url="http://unused", client=client) == 0
    out = capsys.readouterr().out
    assert "1 approval(s) waiting (/approvals)" in out
    assert "1 card(s) pending" in out
    assert "assistant ready (qwen3)" in out


def test_banner_all_clear(
    config: SupervisorConfig,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script_input(monkeypatch, [])
    assert run_chat(home=config.home, url="http://unused", client=client) == 0
    assert "nothing waiting on you" in capsys.readouterr().out


def test_oneshot_streams_one_reply_and_exits_zero(
    config: SupervisorConfig,
    client: TestClient,
    ollama: FakeOllama,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ollama.script_reply("scripted oneshot answer")
    code = run_chat(
        home=config.home, url="http://unused", client=client, oneshot="what is pending?"
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "scripted oneshot answer" in out
    # The scripting face keeps stdout clean: no banner, no prompt.
    assert "nothing waiting on you" not in out


def test_oneshot_skips_cards_and_reports_them_by_id(
    config: SupervisorConfig,
    client: TestClient,
    ollama: FakeOllama,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without an answering stdin (cron, pipes), EOF reads as skip — never
    act. The old oneshot contract, pinned through the v50-F1 resolve path."""
    ollama.script_tool_call("set_policy", {"auto_approve": True})
    script_input(monkeypatch, [])  # EOF: no one is at the keyboard
    code = run_chat(
        home=config.home, url="http://unused", client=client, oneshot="enable auto approve"
    )
    assert code == 0
    (action,) = _chat_actions(client)
    assert action["status"] == "proposed"  # skipped, never confirmed
    out = capsys.readouterr().out
    assert f"card left pending ({action['action_id']})" in out
    # v50-F3: the hint is actionable — a real address and the exact command.
    assert "confirm at http://unused" in out
    assert "skep chat --continue" in out
    assert client.get("/api/policy").json()["auto_approve"] is False


def test_oneshot_confirms_a_card_when_the_operator_answers(
    config: SupervisorConfig,
    client: TestClient,
    ollama: FakeOllama,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """v50-F1: the card cliff is gone — a oneshot user at a TTY can say yes
    right there, through the exact REPL resolution path."""
    ollama.script_tool_call("set_policy", {"auto_approve": True})
    ollama.script_reply("auto-approve is on")
    script_input(monkeypatch, ["y"])
    code = run_chat(
        home=config.home, url="http://unused", client=client, oneshot="enable auto approve"
    )
    assert code == 0
    assert client.get("/api/policy").json()["auto_approve"] is True
    (action,) = _chat_actions(client)
    assert action["status"] == "confirmed"
    assert "auto-approve is on" in capsys.readouterr().out


def test_oneshot_yes_confirms_cards_without_a_tty(
    config: SupervisorConfig,
    client: TestClient,
    ollama: FakeOllama,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """v81-F12: --yes is the explicit opt-out of skip-and-report — the card
    confirms and the continuation streams, with no one at the keyboard."""
    ollama.script_tool_call("set_policy", {"auto_approve": True})
    ollama.script_reply("auto-approve is on")
    script_input(monkeypatch, [])  # EOF: cron/pipe, nobody answering
    code = run_chat(
        home=config.home,
        url="http://unused",
        client=client,
        oneshot="enable auto approve",
        yes=True,
    )
    assert code == 0
    (action,) = _chat_actions(client)
    assert action["status"] == "confirmed"
    out = capsys.readouterr().out
    assert "--yes: confirming card" in out
    assert "auto-approve is on" in out
    assert client.get("/api/policy").json()["auto_approve"] is True


def test_oneshot_intercepts_slash_commands_before_the_model(
    config: SupervisorConfig,
    client: TestClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """v48-F5: /help in oneshot is the deterministic deck table, not a
    model-invented summary (no FakeOllama here — the model is never asked)."""
    assert run_chat(home=config.home, url="http://unused", client=client, oneshot="/help") == 0
    out = capsys.readouterr().out
    assert "/workon" in out and "/approvals" in out  # the deck table
    (chat,) = client.get("/api/chats").json()["chats"]
    detail = client.get(f"/api/chats/{chat['chat_id']}").json()
    assert detail["messages"] == []  # nothing went to the Queen


def test_oneshot_runs_read_deck_commands_against_the_api(
    config: SupervisorConfig,
    client: TestClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_chat(home=config.home, url="http://unused", client=client, oneshot="/runs") == 0
    assert "(no runs yet)" in capsys.readouterr().out


def test_oneshot_continue_reuses_the_latest_chat(
    config: SupervisorConfig,
    client: TestClient,
    ollama: FakeOllama,
) -> None:
    """v50-F2: --continue --oneshot is real continuity — one transcript,
    so the Queen is not re-asked for context it already has."""
    ollama.script_reply("noted: dark roast")
    run_chat(home=config.home, url="http://unused", client=client, oneshot="I like dark roast")

    ollama.script_reply("a dark roast reminder it is")
    run_chat(
        home=config.home,
        url="http://unused",
        client=client,
        oneshot="set that reminder we discussed",
        continue_latest=True,
    )

    chats = client.get("/api/chats").json()["chats"]
    assert len(chats) == 1  # the second oneshot joined the first chat
    detail = client.get(f"/api/chats/{chats[0]['chat_id']}").json()
    user_messages = [m["content"] for m in detail["messages"] if m["role"] == "user"]
    assert user_messages == ["I like dark roast", "set that reminder we discussed"]
    # The model saw the full transcript on the second turn.
    assert any(
        "dark roast" in str(m.get("content"))
        for m in ollama.chat_bodies()[-1]["messages"]
        if m["role"] == "user"
    )


# ---------- v77-F4: indented agent threads with deterministic summaries ----------


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"error": "boom"}, "✗ boom"),
        ({"ok": False}, "✗ refused"),
        ({"ok": False, "error": "denied by policy: nope"}, "✗ denied by policy: nope"),
        ({"runs": [1, 2, 3]}, "✓ 3 runs"),
        ({"approvals": []}, "✓ 0 approvals"),
        ({"task_id": "t-1", "state": "dispatched"}, "✓ run t-1"),
        ({"resumed_as": "t-2"}, "✓ run t-2"),
        ({"ok": True, "result": {"task_id": "t-3"}}, "✓ run t-3"),
        ({"note": "hi"}, "✓ ok (14 chars)"),  # len('{"note": "hi"}')
        ("plain text", "✓ ok (10 chars)"),
    ],
)
def test_tool_summary_table(result: Any, expected: str) -> None:
    """The pinned shape table: error / ok-false / ok-wrapper / single-list /
    task_id / fallback — deterministic, never model text."""
    from skep.cli_chat import _tool_summary

    assert _tool_summary("any_tool", result) == expected


def test_streamed_tool_calls_render_as_an_indented_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = RecordingOut()
    monkeypatch.setattr(sys, "stdout", out)
    events: list[tuple[str, dict[str, Any]]] = [
        ("tool", {"tool": "list_runs", "result": {"runs": [1, 2]}}),
        ("tool", {"tool": "read_file", "result": {"error": "no such file"}}),
        ("message", {"content": "done"}),
        ("done", {"state": "complete"}),
    ]
    render_turn(iter(events))
    text = out.getvalue()
    assert "  ▸ list_runs\n    ✓ 2 runs\n" in text
    assert "  ▸ read_file\n    ✗ no such file\n" in text


def test_replay_renders_tool_rows_through_the_summary(
    config: SupervisorConfig,
    client: TestClient,
    ollama: FakeOllama,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Live and replay read the same fields: a replayed transcript shows the
    thread shape, not a bare tool name."""
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    ollama.script_tool_call("list_runs", {})
    ollama.script_reply("no runs yet")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "any runs?"})
    capsys.readouterr()
    script_input(monkeypatch, ["/replay"])
    assert run_chat(home=config.home, url="http://unused", client=client, chat_id=chat_id) == 0
    out = capsys.readouterr().out
    assert "▸ list_runs" in out
    assert "✓ 0 runs" in out


# ---------- v77-F5: OSC 8 task-id links ----------


def test_task_ids_link_on_a_tty_and_stay_plain_off(
    repo: Path,
    config: SupervisorConfig,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The OSC 8 byte sequence wraps the FULL run id on a TTY (NO_COLOR does
    not disable links — it governs color); piped output is byte-plain."""
    from skep import cli_chat

    from .conftest import wait_terminal

    response = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "workspace",
        },
    )
    task_id = str(response.json()["task_id"])
    wait_terminal(client, task_id)

    # Off-TTY (the oneshot/script contract): no escape bytes at all.
    script_input(monkeypatch, ["/runs"])
    assert run_chat(home=config.home, url="http://unused", client=client) == 0
    out = capsys.readouterr().out
    assert task_id in out
    assert "\x1b]8" not in out

    # TTY with NO_COLOR: the link survives, wrapping the full id.
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(cli_chat, "_tty", lambda: True)
    script_input(monkeypatch, ["/runs"])
    assert run_chat(home=config.home, url="http://unused", client=client) == 0
    out = capsys.readouterr().out
    assert f"\x1b]8;;http://unused/#/runs/{task_id}\x1b\\{task_id}\x1b]8;;\x1b\\" in out


# ---------- v77-F2: the prompt status line ----------


class StubServe:
    """The two GETs the status prompt makes, with call counting."""

    def __init__(self, percent: int = 10, model: str | None = None, fail: bool = False) -> None:
        self.percent = percent
        self.model = model
        self.fail = fail
        self.calls: list[str] = []

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(path)
        if self.fail:
            raise RuntimeError("daemon went away")
        if path == "/api/llm/config":
            return {"default_model": "qwen3"}
        # The real chat-detail shape: the record nests under "chat".
        return {"chat": {"model": self.model}, "context": {"percent": self.percent}}


def _tty_repl(monkeypatch: pytest.MonkeyPatch, stub: StubServe) -> Any:
    from skep import cli_chat

    monkeypatch.setattr(cli_chat, "_tty", lambda: True)
    monkeypatch.delenv("NO_COLOR", raising=False)
    return cli_chat.ChatRepl(cast(Any, stub), {"chat_id": "c1"}, show_thinking=False)


@pytest.mark.parametrize(
    ("percent", "code"), [(59, "32"), (60, "33"), (79, "33"), (80, "31")]
)
def test_prompt_thresholds_and_readline_markers(
    monkeypatch: pytest.MonkeyPatch, percent: int, code: str
) -> None:
    """The pinned thresholds (green <60, amber 60-79, red >=80) and the
    \\001/\\002 wrapping readline needs — real control bytes, zero-width."""
    from skep.cli_chat import PROMPT

    repl = _tty_repl(monkeypatch, StubServe(percent=percent))
    assert repl._status_prompt() == (
        f"\001\x1b[{code}m\002model: qwen3 · ctx: {percent}% · {PROMPT}\001\x1b[0m\002"
    )


def test_prompt_shows_the_chat_override_model(monkeypatch: pytest.MonkeyPatch) -> None:
    repl = _tty_repl(monkeypatch, StubServe(model="qwen3:32b"))
    assert "model: qwen3:32b" in repl._status_prompt()


def test_prompt_refresh_failure_falls_back_to_the_bare_glyph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skep.cli_chat import CYAN, PROMPT

    repl = _tty_repl(monkeypatch, StubServe(fail=True))
    assert repl._status_prompt() == f"\001\x1b[{CYAN}m\002{PROMPT}\001\x1b[0m\002"


def test_prompt_off_tty_is_the_bare_glyph_and_never_fetches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skep import cli_chat

    monkeypatch.setattr(cli_chat, "_tty", lambda: False)
    stub = StubServe()
    repl = cli_chat.ChatRepl(cast(Any, stub), {"chat_id": "c1"}, show_thinking=False)
    assert repl._status_prompt() == cli_chat.PROMPT
    assert stub.calls == []


def test_prompt_caches_between_deck_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refresh discipline: two prompts with no model turn between them cost
    one fetch; invalidation restores the fetch."""
    stub = StubServe()
    repl = _tty_repl(monkeypatch, stub)
    first = repl._status_prompt()
    assert repl._status_prompt() == first
    assert stub.calls.count("/api/chats/c1") == 1
    repl._prompt_cache = None  # what send()//btw/card verdicts do
    repl._status_prompt()
    assert stub.calls.count("/api/chats/c1") == 2


def test_prompt_line_appears_on_a_tty_session(
    config: SupervisorConfig,
    client: TestClient,
    ollama: FakeOllama,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End to end: the string handed to input() carries model + meter."""
    from skep import cli_chat

    monkeypatch.setattr(cli_chat, "_tty", lambda: True)
    monkeypatch.delenv("NO_COLOR", raising=False)
    prompts: list[str] = []
    lines = iter(["hello", "exit"])

    def fake_input(prompt: str = "") -> str:
        prompts.append(prompt)
        try:
            return next(lines)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", fake_input)
    ollama.script_reply("hi there")
    assert run_chat(home=config.home, url="http://unused", client=client) == 0
    assert prompts and all("model: qwen3" in p and "ctx:" in p for p in prompts)


# ---------- v77-F3: /status, /model, /exit ----------


def test_status_reprints_the_banner_mid_session(
    config: SupervisorConfig,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script_input(monkeypatch, ["/status"])
    assert run_chat(home=config.home, url="http://unused", client=client) == 0
    out = capsys.readouterr().out
    assert out.count("nothing waiting on you") == 2  # entry banner + /status
    assert "daemon: http://unused" in out


def test_model_no_arg_names_the_effective_model_and_meter(
    config: SupervisorConfig,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script_input(monkeypatch, ["/model"])
    assert run_chat(home=config.home, url="http://unused", client=client) == 0
    out = capsys.readouterr().out
    assert "default: qwen3" in out
    assert "context:" in out


def test_model_with_name_cards_and_the_override_shows(
    config: SupervisorConfig,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """/model <name> cards through set_assistant_model (scope chat is the
    default); after confirming, /model reports the override."""
    script_input(monkeypatch, ["/model qwen3:32b", "y", "/model"])
    assert run_chat(home=config.home, url="http://unused", client=client) == 0
    out = capsys.readouterr().out
    assert "confirm: set_assistant_model" in out
    assert "this chat: qwen3:32b (override)" in out
    # scope chat touched one row, never the shared default.
    assert client.get("/api/llm/config").json()["default_model"] == "qwen3"


def test_model_scope_default_reaches_the_saved_config(
    config: SupervisorConfig,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script_input(monkeypatch, ["/model llama4 --scope default", "y"])
    assert run_chat(home=config.home, url="http://unused", client=client) == 0
    assert client.get("/api/llm/config").json()["default_model"] == "llama4"


def test_exit_command_leaves_the_repl(
    config: SupervisorConfig,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The line after /exit must never be read — the loop returned.
    script_input(monkeypatch, ["/exit", "never sent to the model"])
    assert run_chat(home=config.home, url="http://unused", client=client) == 0
    assert "exiting" in capsys.readouterr().out
    (chat,) = client.get("/api/chats").json()["chats"]
    detail = client.get(f"/api/chats/{chat['chat_id']}").json()
    assert detail["messages"] == []


def test_serve_client_raises_with_detail(config: SupervisorConfig) -> None:
    raw = serve_client(config)
    token = (config.home / "serve-token").read_text().strip()
    serve = ServeClient("http://unused", token, client=raw)
    with pytest.raises(ServeApiError) as excinfo:
        serve.get("/api/chats/no-such-chat")
    assert excinfo.value.status == 404
    assert "no chat" in excinfo.value.detail
