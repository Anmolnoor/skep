"""Phase 08 (8.2): operator-reviewed SKILL.md capture through the v4 ladder.

``skep skill capture <task_id|chat_id> --name <slug>`` (and REST, and the
chat's ``capture_skill`` card) drafts a SKILL.md from a finished run or a
chat's last multi-step turn. The draft is a v4 candidate — no new queue —
with zero grants: the hosts and paths it used are declared, never granted
(the current policy decides at run time). It stays inactive until a human
approves it, a tainted one only with the taint acknowledged, and every step
leaves a decision row (I6, I8, I13).
"""

from __future__ import annotations

import dataclasses
import json
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from skep.cli import main
from skep.supervisor import RunStore, SupervisorConfig, skill_cmds
from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task
from skep.supervisor.observe import _draft_from_last_turn
from skep.supervisor.serve import channels
from skep.supervisor.serve.channels import card_notes, render_confirm_card
from skep.supervisor.serve.taint import trust_chat
from skep.supervisor.skill_bundle import skill_grants
from skep.supervisor.skill_cmds import CAPTURED, SkillError, approve, capture_skill
from skep.supervisor.skill_md import parse_skill_md
from skep.supervisor.skills import DRAFT, TESTED, SkillCandidate, candidate_signature
from skep.supervisor.templates import WorkflowTemplate
from skep.worker_contract import CONTRACT_VERSION, Event
from skep.worker_contract.task import AutonomyDecisionPayload, Permissions

from .conftest import serve_client
from .taint_grid import set_taint
from .test_taint_v3 import Live, _action, live  # noqa: F401 (the fixture)

STEPS = (
    ("run_shell", {"command": "make lint"}),
    ("read_url", {"url": "https://docs.example.com/release"}),
    ("read_file", {"path": "/srv/app/CHANGELOG.md"}),
)


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[RunStore]:
    store = RunStore(tmp_path / "supervisor.sqlite3")
    yield store
    store.close()


def _turn(
    store: RunStore,
    chat_id: str,
    ask: str = "run the release check",
    steps: tuple[tuple[str, dict[str, Any]], ...] = STEPS,
    *,
    failed: str | None = None,
) -> None:
    store.add_chat_message(chat_id, role="user", content=ask)
    ids = [f"{chat_id[:4]}{len(ask)}c{i}" for i in range(len(steps))]
    calls: list[dict[str, Any]] = [
        {"id": call_id, "function": {"name": n, "arguments": a}}
        for call_id, (n, a) in zip(ids, steps, strict=True)
    ]
    store.add_chat_message(chat_id, role="assistant", content="", tool_calls=calls)
    for call_id, (name, _args) in zip(ids, steps, strict=True):
        result = {"error": "refused"} if name == failed else {"ok": True}
        store.add_chat_message(
            chat_id, role="tool", content=json.dumps(result), tool_name=name,
            tool_call_id=call_id,
        )  # fmt: skip
    store.add_chat_message(chat_id, role="assistant", content="done, all steps ran")


def _chat(store: RunStore, **kw: Any) -> str:
    chat = store.create_chat(title="ops", model=None).chat_id
    _turn(store, chat, **kw)
    return chat


def _completed_run(
    store: RunStore,
    audit: Path,
    repo: Path,
    *,
    commands: tuple[tuple[str, int], ...] = (
        ("uv run pytest -q", 0), ("make broken", 1), ("uv run pytest -q", 0),
    ),
    chat_taint: list[str] | None = None,
    resume_of: str | None = None,
    network: tuple[str, ...] = ("pypi.org",),
) -> str:  # fmt: skip
    repo.mkdir(parents=True, exist_ok=True)
    decision = None
    if chat_taint is not None:  # what a tainted chat's dispatch records (P4-1)
        decision = AutonomyDecisionPayload.model_validate(
            {"verdict": "allow", "reason": "dispatch.allowed",
             "constraints": {"chat_taint": chat_taint}}
        )  # fmt: skip
    task = mint_task(
        workspace=repo,
        instructions="Run the test suite and fix what fails.",
        permissions=Permissions(
            read=[str(repo)], write=[str(repo)], network=list(network), env_allowlist=[]
        ),
        budget=DEFAULT_BUDGET,
        dispatch_decision=decision,
    )
    task = task.model_copy(update={"resume_of": resume_of})
    store.create_run(task, repo=repo, ref=None, execution_mode="sandbox")
    (audit / task.task_id).mkdir(parents=True)
    (audit / task.task_id / "task.json").write_text(task.model_dump_json(), encoding="utf-8")
    store.ingest_events(
        [
            Event.model_validate(
                {
                    "contract_version": CONTRACT_VERSION,
                    "event_id": f"{task.task_id}-e{seq}",
                    "seq": seq,
                    "task_id": task.task_id,
                    "trace_id": task.trace_id,
                    "ts": "2026-11-23T00:00:00Z",
                    "type": "command.result",
                    "payload": {
                        "command": command,
                        "exit_code": code,
                        "duration_ms": 1,
                        "stdout_tail": "",
                        "stderr_tail": "",
                    },
                }
            )
            for seq, (command, code) in enumerate(commands, start=1)
        ]
    )
    store.transition(task.task_id, "completed", "done")
    return task.task_id


def test_a_chat_capture_is_an_inactive_zero_grant_skill_md_draft(
    store: RunStore, tmp_path: Path
) -> None:
    chat = _chat(store)
    candidate, text = capture_skill(store, tmp_path / "audit", chat, name="release-check",
                                    actor="op")  # fmt: skip
    assert (candidate.status, candidate.template.provenance) == (DRAFT, CAPTURED)
    assert store.get_template("release-check") is None  # inactive until approved
    assert not skill_grants(candidate.template)["dangerous"]  # never a silent grant
    assert candidate.source_task_ids == (f"chat:{chat}",)
    assert candidate.taint == ()
    for line in (
        "1. run_shell — make lint",
        "2. read_url — https://docs.example.com/release",
        "3. read_file — /srv/app/CHANGELOG.md",
        "hosts: docs.example.com",
        "read paths: /srv/app/CHANGELOG.md",
    ):
        assert line in text, line
    assert "grants nothing" in text
    # the SKILL.md is a real one: it parses back to the draft
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "SKILL.md").write_text(text, encoding="utf-8")
    parsed = parse_skill_md(pack)
    assert parsed.name == "release-check"
    assert parsed.instructions == candidate.template.instructions
    (event,) = [e for e in store.note_task_events() if e.action == "captured"]
    assert (event.kind, event.item_id, event.actor) == ("skill_candidate", "release-check", "op")
    assert event.detail == {"source": f"chat:{chat}", "taint": []}


def test_the_newest_completed_multi_step_turn_is_captured(store: RunStore, tmp_path: Path) -> None:
    """The turn that asks for the capture is not the procedure: it is still
    running (or ran only capture_skill); a refused step is not a step."""
    chat = _chat(store, failed="read_file")
    _turn(store, chat, "save that as a skill", (("capture_skill", {"name": "x"}),))
    store.add_chat_message(chat, role="user", content="and another")
    _candidate, text = capture_skill(store, tmp_path, chat, name="rc", actor="op")
    assert "run the release check" in text
    assert "read_file" not in text and "capture_skill" not in text


def test_a_tainted_chat_drafts_marked_and_approval_must_name_the_taint(
    store: RunStore, tmp_path: Path
) -> None:
    chat = _chat(store)
    set_taint(store, chat, "untrusted")
    candidate, text = capture_skill(store, tmp_path, chat, name="rc", actor="op")
    assert candidate.taint == ("untrusted",)
    assert 'taint: "untrusted"' in text
    with pytest.raises(SkillError) as refused:
        approve(store, "rc", actor="op")
    assert f"chat:{chat}" in str(refused.value)
    assert "outside content" in str(refused.value)
    assert "--accept-taint" in str(refused.value)
    assert store.get_template("rc") is None
    approved, target = approve(store, "rc", actor="op", accept_taint=True)
    assert approved.status == "approved"
    assert store.get_template(target).provenance == CAPTURED  # type: ignore[union-attr]
    (decided,) = [e for e in store.note_task_events() if e.action == "approved"]
    assert "taint accepted: outside content" in decided.detail["note"]


def test_a_run_capture_lists_what_ran_and_widens_nothing(store: RunStore, tmp_path: Path) -> None:
    audit = tmp_path / "audit"
    task_id = _completed_run(store, audit, tmp_path / "repo")
    candidate, text = capture_skill(store, audit, task_id, name="fix-tests", actor="op")
    assert candidate.template.network == () and candidate.template.shell_allowlist == ()
    assert "Run the test suite and fix what fails." in text
    assert "1. uv run pytest -q" in text and "make broken" not in text
    assert text.count("uv run pytest -q") == 1  # each command once, in first-run order
    assert "hosts: pypi.org" in text
    assert candidate.source_task_ids == (task_id,)
    assert candidate.taint == ("untrusted",)  # the commands are the worker's words (D15)


def test_capture_refuses_what_it_cannot_honestly_draft(store: RunStore, tmp_path: Path) -> None:
    chat = _chat(store)
    quiet = store.create_chat(title="q", model=None).chat_id
    store.add_chat_message(quiet, role="user", content="hi")
    store.add_chat_message(quiet, role="assistant", content="hello")
    braces = _chat(store, steps=(("run_shell", {"command": "echo {{x}}"}), STEPS[0]))
    single = _chat(store, steps=STEPS[:1])
    task = mint_task(workspace=tmp_path, instructions="x", budget=DEFAULT_BUDGET)
    store.create_run(task, repo=tmp_path, ref=None, execution_mode="sandbox")
    store.transition(task.task_id, "failed", "boom")
    capture_skill(store, tmp_path, chat, name="taken", actor="op")
    for source, name, why in (
        ("nope", "a", "no run or chat"),
        (task.task_id, "a", "only a completed run"),
        (quiet, "a", "no finished multi-step turn"),
        (
            single,
            "a",
            "no finished multi-step turn",
        ),  # Review R1 (Q14): one step is not a procedure
        (chat, "taken", "already exists"),
        (chat, "Bad Name!", "lowercase slug"),
        (braces, "a", "parameter"),
    ):
        with pytest.raises(SkillError, match=why):
            capture_skill(store, tmp_path, source, name=name, actor="op")


def test_the_ladder_still_holds_for_a_draft(store: RunStore, tmp_path: Path) -> None:
    """Direct approval from draft is the zero-grant lane only: a captured
    draft that somehow carries a grant must take the test gate."""
    chat = _chat(store)
    candidate, _text = capture_skill(store, tmp_path, chat, name="rc", actor="op")
    widened = dataclasses.replace(
        candidate, template=dataclasses.replace(candidate.template, network=("evil.example",))
    )
    store.add_candidate(widened)
    with pytest.raises(SkillError, match="test it first"):
        approve(store, "rc", actor="op")


def _cli(home: Path, *args: str) -> int:
    return main(["--home", str(home), *args])


def test_the_cli_shows_the_full_skill_md_and_gates_a_tainted_approval(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    store = RunStore(home / "supervisor" / "supervisor.sqlite3")
    chat = _chat(store)
    set_taint(store, chat, "untrusted")
    store.close()
    assert _cli(home, "skill", "capture", chat, "--name", "rc") == 0
    out = capsys.readouterr().out
    assert '---\nname: "rc"' in out and "1. run_shell — make lint" in out
    assert f"captured from chat:{chat}" in out and "outside content" in out
    assert "inactive" in out and "skep skill approve rc --accept-taint" in out
    assert _cli(home, "skill", "show", "rc") == 0
    assert "outside content" in capsys.readouterr().out
    assert _cli(home, "skill", "approve", "rc") != 0
    assert "--accept-taint" in capsys.readouterr().err
    assert _cli(home, "skill", "approve", "rc", "--accept-taint") == 0
    assert _cli(home, "skill", "capture", "nope", "--name", "x") != 0


def test_rest_captures_and_the_approve_route_names_the_taint(config: SupervisorConfig) -> None:
    client = serve_client(config)
    store = RunStore(config.db_path)
    try:
        chat = _chat(store)
        set_taint(store, chat, "private")
    finally:
        store.close()
    made = client.post("/api/skills/capture", json={"source": chat, "name": "rc"})
    assert made.status_code == 201, made.text
    assert "1. run_shell — make lint" in made.json()["skill_md"]
    assert made.json()["candidate"]["taint"] == ["private"]
    again = client.post("/api/skills/capture", json={"source": chat, "name": "rc"})
    assert again.status_code == 409
    refused = client.post("/api/skills/rc/approve", json={})
    assert refused.status_code == 409 and "private data" in refused.json()["detail"]
    assert client.post("/api/skills/rc/approve", json={"accept_taint": True}).status_code == 200


@pytest.fixture()
def env(live: Callable[[str], Live]) -> Live:  # noqa: F811
    return live("builder")


def test_the_chat_card_shows_the_full_skill_md_and_binds_it(env: Live) -> None:
    chat = env.chat()
    _turn(env.store, chat)
    env.ollama.script_tool_call("capture_skill", {"name": "rc"})
    card = _action(env.turn(chat, "save that as a skill"))
    assert card["tool"] == "capture_skill"
    assert card["args"]["source"] == chat  # this chat, pinned when proposed
    assert "1. run_shell — make lint" in card["card"]["skill_md"]
    assert "inactive until you approve it" in card["card"]["risk"]
    assert env.store.list_candidates() == []  # nothing before the OK
    env.ollama.script_reply("drafted")
    env.confirm(chat, card["action_id"])
    (candidate,) = env.store.list_candidates()
    assert candidate.status == DRAFT and env.store.get_template("rc") is None

    # the confirm drafts what the card showed, or nothing
    env.ollama.script_tool_call("capture_skill", {"name": "rc2"})
    card = _action(env.turn(chat, "save that too"))
    _turn(
        env.store,
        chat,
        "a newer procedure",
        (("run_shell", {"command": "make dist"}), ("run_shell", {"command": "make upload"})),
    )
    env.ollama.script_reply("it changed")
    env.confirm(chat, card["action_id"])
    assert env.store.get_candidate("rc2") is None
    (result,) = [
        m.content
        for m in env.store.chat_messages(chat)
        if m.role == "tool" and m.tool_name == "capture_skill" and "changed" in m.content
    ]
    assert "propose capture_skill again" in result


def test_a_tainted_source_badges_the_capture_card(env: Live, tmp_path: Path) -> None:
    """A clean chat capturing a run: the card's taint badge names the run."""
    audit = env.holder.current.audit_dir
    task_id = _completed_run(env.store, audit, tmp_path / "repo")
    chat = env.chat()
    env.ollama.script_tool_call("capture_skill", {"name": "fix", "source": task_id})
    card = _action(env.turn(chat, "save run as a skill"))
    assert card["card"]["taint"].startswith("⚠ tainted")
    assert f"captured from {task_id}" in card["card"]["taint"]


def test_string_arguments_and_plain_results_still_read(store: RunStore, tmp_path: Path) -> None:
    """Providers send arguments as a JSON string too, and a tool may answer
    in plain text; a garbled argument string drafts the bare tool name."""
    chat = store.create_chat(title="ops", model=None).chat_id
    store.add_chat_message(chat, role="user", content="lint it")
    store.add_chat_message(chat, role="assistant", content="", tool_calls=[
        {"id": "a", "function": {"name": "run_shell", "arguments": '{"command": "make lint"}'}},
        {"id": "b", "function": {"name": "run_shell", "arguments": "{not json"}},
    ])  # fmt: skip
    for call_id in ("a", "b"):
        store.add_chat_message(chat, role="tool", content="lint ok", tool_name="run_shell",
                               tool_call_id=call_id)  # fmt: skip
    store.add_chat_message(chat, role="assistant", content="done")
    _candidate, text = capture_skill(store, tmp_path, chat, name="lint", actor="op")
    assert "1. run_shell — make lint\n2. run_shell\n" in text


def test_a_run_capture_needs_its_task_and_a_command_that_ran(
    store: RunStore, tmp_path: Path
) -> None:
    audit = tmp_path / "audit"
    task = mint_task(workspace=tmp_path, instructions="x", budget=DEFAULT_BUDGET)
    store.create_run(task, repo=tmp_path, ref=None, execution_mode="sandbox")
    store.transition(task.task_id, "completed", "done")
    with pytest.raises(SkillError, match="no readable task"):
        capture_skill(store, audit, task.task_id, name="a", actor="op")
    (audit / task.task_id).mkdir(parents=True)
    (audit / task.task_id / "task.json").write_text(task.model_dump_json(), encoding="utf-8")
    with pytest.raises(SkillError, match="ran no command that succeeded"):
        capture_skill(store, audit, task.task_id, name="a", actor="op")


def test_a_card_that_cannot_draft_says_why_and_its_confirm_drafts_nothing(env: Live) -> None:
    chat = env.chat()
    env.ollama.script_tool_call("capture_skill", {"name": "x", "source": "nope"})
    card = _action(env.turn(chat, "save it"))
    assert card["card"]["risk"] == "it cannot be drafted as shown: no run or chat 'nope'"
    assert "skill_md" not in card["card"]
    env.ollama.script_reply("no")
    env.confirm(chat, card["action_id"])
    assert env.store.list_candidates() == []
    assert any(
        "recorded no SKILL.md" in m.content
        for m in env.store.chat_messages(chat)
        if m.role == "tool" and m.tool_name == "capture_skill"
    )


def test_the_cli_capture_needs_a_store(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli(tmp_path / "empty", "skill", "capture", "x", "--name", "a") != 0
    assert "nothing to capture" in capsys.readouterr().err


# -- review round 1 ------------------------------------------------------------

USER, WORD, TOKEN, VALUE = "usr9", "pw7x", "tk8y", "k3yv"  # fakes, never real
SECRET_STEPS = (
    ("run_shell", {"command": f"git clone https://{USER}:{WORD}@git.example/r"}),
    ("read_url", {"url": f"https://{TOKEN}@api.example/x?q=1"}),
)


def _no_secret(text: str) -> None:
    for secret in (USER, WORD, TOKEN, VALUE):
        assert secret not in text, (secret, text)


def test_review_r1_a_capture_carries_no_secret(store: RunStore, tmp_path: Path) -> None:
    """Review R1 (Q3): a captured SKILL.md is written to be shared — every
    command, URL, query and ask passes the redactor, URL userinfo stripped."""
    ask = "deploy with SKEP_" + "TOKEN=" + VALUE + " now"
    chat = _chat(store, ask=ask, steps=SECRET_STEPS)
    candidate, text = capture_skill(store, tmp_path, chat, name="dep", actor="op")
    for each in (text, candidate.template.instructions, candidate.template.description):
        _no_secret(each)
    assert "git.example/r" in text and "api.example/x" in text
    audit = tmp_path / "audit"
    run = _completed_run(
        store, audit, tmp_path / "repo",
        commands=((f"curl https://{TOKEN}@pypi.org/simple", 0), ("uv run pytest", 0)),
    )  # fmt: skip
    _candidate, text = capture_skill(store, audit, run, name="run", actor="op")
    _no_secret(text)
    assert "pypi.org/simple" in text


def test_review_r1_a_run_capture_keeps_the_dispatching_chats_taint(
    store: RunStore, tmp_path: Path
) -> None:
    """Review R1 (Q8): a run dispatched from a private chat drafts private,
    and so does its resume (the decision on a resume carries no taint; the
    nearest ancestor's holds)."""
    audit = tmp_path / "audit"
    parent = _completed_run(store, audit, tmp_path / "repo", chat_taint=["private"])
    candidate, _text = capture_skill(store, audit, parent, name="p", actor="op")
    assert candidate.taint == ("private", "untrusted")
    resumed = _completed_run(store, audit, tmp_path / "repo", resume_of=parent)
    candidate, _text = capture_skill(store, audit, resumed, name="r", actor="op")
    assert candidate.taint == ("private", "untrusted")


def test_review_r1_a_turn_from_before_trust_keeps_its_taint(
    store: RunStore, tmp_path: Path
) -> None:
    """Review R1 (Q9): /trust clears the chat's bits, not what an earlier
    turn ran under — a turn from before it is tainted (its taint then is not
    recoverable: both bits, acknowledged at approve). A turn after is clean."""
    chat = store.create_chat(title="ops", model=None).chat_id
    store.add_chat_message(chat, role="user", content="deploy now", provenance="webhook:ci")
    _turn(store, chat)  # the procedure, steered by outside content
    _turn(store, chat, "thanks", ())  # no steps: not a procedure
    trust_chat(store, tmp_path, chat, [], actor="op")
    assert store.get_chat_taint(chat).bits() == []
    candidate, text = capture_skill(store, tmp_path, chat, name="old", actor="op")
    assert "run the release check" in text
    assert candidate.taint == ("private", "untrusted")
    _turn(store, chat, "run it again")
    candidate, _text = capture_skill(store, tmp_path, chat, name="new", actor="op")
    assert candidate.taint == ()


def test_review_r1_a_conversation_draft_from_before_taint_needs_acknowledgment(
    tmp_path: Path,
) -> None:
    """Review R1 (Q10): drafts the old observer made (it drafted from
    untrusted chats too) carry no taint record; the upgrade marks them
    unknown — both bits — so approving one names it."""
    db = tmp_path / "supervisor.sqlite3"
    store = RunStore(db)
    for name, provenance in (("conv-x", "conversation"), ("learned-x", "learned")):
        template = WorkflowTemplate(
            name=name, instructions="observed steps", description="d", provenance=provenance
        )
        store.add_candidate(
            SkillCandidate(
                name=name, signature=candidate_signature(template), status=DRAFT,
                template=template, source_task_ids=("chat:abc",), occurrences=1,
            )
        )  # fmt: skip
    store.close()
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("ALTER TABLE skill_candidates DROP COLUMN taint")
        conn.commit()
    store = RunStore(db)
    try:
        assert store.get_candidate("conv-x").taint == ("private", "untrusted")  # type: ignore[union-attr]
        assert store.get_candidate("learned-x").taint == ()  # type: ignore[union-attr]
        with pytest.raises(SkillError, match="--accept-taint"):
            approve(store, "conv-x", actor="op")
        approve(store, "conv-x", actor="op", accept_taint=True)
    finally:
        store.close()


def test_review_r1_a_channel_chat_turn_is_outside_content(store: RunStore, tmp_path: Path) -> None:
    """Review R1 (Q11): a channel chat stores its members' words as the
    operator's — the observer never drafts from one, and a capture of one is
    untrusted."""
    web = _chat(store)
    assert _draft_from_last_turn(store, web) is not None
    channel = store.create_chat(title="tg", model=None, source="telegram").chat_id
    _turn(store, channel, "ship the release")
    assert _draft_from_last_turn(store, channel) is None
    candidate, _text = capture_skill(store, tmp_path, channel, name="tg", actor="op")
    assert candidate.taint == ("untrusted",)


def test_review_r1_two_captures_of_one_name_never_both_succeed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review R1 (Q12): both pass the name check; the name is reserved with
    the audit row in one transaction, so exactly one wins and is recorded."""
    first, second = RunStore(tmp_path / "s.sqlite3"), RunStore(tmp_path / "s.sqlite3")
    try:
        mine, theirs = _chat(first), _chat(first, ask="another procedure")
        real = skill_cmds.draft_capture

        def racing(store: RunStore, *args: Any, **kw: Any) -> Any:
            drafted = real(store, *args, **kw)
            if store is second:  # it passed the name check: the other capture lands now
                monkeypatch.setattr(skill_cmds, "draft_capture", real)
                capture_skill(first, tmp_path, mine, name="rc", actor="a")
            return drafted

        monkeypatch.setattr(skill_cmds, "draft_capture", racing)
        with pytest.raises(SkillError, match="already exists"):
            capture_skill(second, tmp_path, theirs, name="rc", actor="b")
        assert second.get_candidate("rc").source_task_ids == (f"chat:{mine}",)  # type: ignore[union-attr]
        events = [e for e in second.note_task_events() if e.action == "captured"]
        assert [(e.actor, e.detail["source"]) for e in events] == [("a", f"chat:{mine}")]
    finally:
        first.close()
        second.close()


def test_review_r1_every_confirming_face_shows_the_bound_skill_md(
    env: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review R1 (Q13): the web shows what the confirm binds (the server
    sanitises the draft before binding it, so masking changes nothing); the
    CLI card shows it whole; no messenger can show it, so none confirms it."""
    chat = env.chat()
    _turn(env.store, chat, "deploy it", SECRET_STEPS)
    env.ollama.script_tool_call("capture_skill", {"name": "rc"})
    card = _action(env.turn(chat, "save that as a skill"))
    (pending,) = env.pending(chat)
    bound = pending.card["capture"]["skill_md"]
    assert card["card"]["skill_md"] == bound
    _no_secret(bound)
    notes = "\n".join(card_notes(card["card"], messenger=False))
    assert bound.strip() in notes
    view = render_confirm_card("plugin", (), card["card"], admin=True)
    assert view.web_ui_only and view.too_long
    assert not any("admin can confirm" in note for note in view.notes)
    monkeypatch.setattr(channels, "channel_admin_actor", lambda *_: True)
    with pytest.raises(Exception) as refused:
        list(env.engine.verdict_events(
            chat, env.store.get_chat(chat), card["action_id"], confirm=True,
            actor="channel:telegram:1:admin:1",
        ))  # fmt: skip
    assert "web UI or the CLI" in str(getattr(refused.value, "detail", refused.value))
    assert env.store.list_candidates() == []


def test_review_r1_a_failed_shell_step_is_not_a_step(store: RunStore, tmp_path: Path) -> None:
    """Review R1 (Q14): the tool answered, the command inside it failed —
    that is not a step of the procedure; one step is not a multi-step turn."""
    chat = store.create_chat(title="ops", model=None).chat_id
    store.add_chat_message(chat, role="user", content="lint and read")
    store.add_chat_message(chat, role="assistant", content="", tool_calls=[
        {"id": "a", "function": {"name": "run_shell", "arguments": {"command": "make lint"}}},
        {"id": "b", "function": {"name": "read_url", "arguments": {"url": "https://x.example"}}},
    ])  # fmt: skip
    failed = {"ok": True, "result": {"command": "make lint", "exit_code": 2, "output": ""}}
    store.add_chat_message(chat, role="tool", content=json.dumps(failed), tool_name="run_shell",
                           tool_call_id="a")  # fmt: skip
    store.add_chat_message(chat, role="tool", content=json.dumps({"ok": True}),
                           tool_name="read_url", tool_call_id="b")  # fmt: skip
    store.add_chat_message(chat, role="assistant", content="done")
    with pytest.raises(SkillError, match="no finished multi-step turn"):
        capture_skill(store, tmp_path, chat, name="lint", actor="op")


def test_review_r1_frontmatter_values_are_quoted_scalars(store: RunStore, tmp_path: Path) -> None:
    """Review R1 (Q15): `description: captured procedure: run ...` is not
    YAML; every value is a double-quoted scalar (JSON's escapes are YAML's)."""
    ask = 'deploy: the "blue" stack # now \\ then'
    chat = _chat(store, ask=ask)
    _candidate, text = capture_skill(store, tmp_path, chat, name="blue", actor="op")
    front = text.split("---\n")[1].splitlines()
    assert front and all(re.fullmatch(r'[a-z_]+: "(?:[^"\\]|\\.)*"', x) for x in front), front
    values = {k: json.loads(v) for k, _, v in (x.partition(": ") for x in front)}
    assert values["description"] == f"captured procedure: {ask}"
    assert values["name"] == "blue"


def test_review_r1_a_draft_approved_without_a_test_says_so(store: RunStore, tmp_path: Path) -> None:
    """Review R1 (Q16): no test ran — the evidence never says "test None"."""
    capture_skill(store, tmp_path, _chat(store), name="rc", actor="op")
    approve(store, "rc", actor="op")
    (decided,) = [e for e in store.note_task_events() if e.action == "approved"]
    assert "approved without a test run" in decided.detail["reason"]
    assert "None" not in decided.detail["reason"]


def test_review_r1_skill_show_names_the_approve_that_will_work(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review R1 (Q17): a tainted TESTED candidate and a zero-grant
    conversation draft both approve directly — with --accept-taint when tainted."""
    home = tmp_path / "home"
    store = RunStore(home / "supervisor" / "supervisor.sqlite3")
    for name, provenance, status, taint in (
        ("conv-a", "conversation", DRAFT, ("untrusted",)),
        ("conv-b", "conversation", DRAFT, ()),
        ("tested-c", "learned", TESTED, ("private",)),
    ):
        template = WorkflowTemplate(
            name=name, instructions="steps", description="d", provenance=provenance
        )
        store.add_candidate(
            SkillCandidate(
                name=name, signature=candidate_signature(template), status=status,
                template=template, source_task_ids=("chat:abc",), occurrences=1, taint=taint,
            )
        )  # fmt: skip
    store.close()
    for name, hint in (
        ("conv-a", "skep skill approve conv-a --accept-taint"),
        ("conv-b", "skep skill approve conv-b   (or reject)"),
        ("tested-c", "skep skill approve tested-c --accept-taint"),
    ):
        assert _cli(home, "skill", "show", name) == 0
        assert hint in capsys.readouterr().out, name


# -- review round 2 ------------------------------------------------------------


def test_review_r2_a_run_capture_strips_userinfo_from_its_declared_hosts(
    store: RunStore, tmp_path: Path
) -> None:
    """Review R2 (F3): the declared hosts pass the export redactor like the
    ask, steps and paths — user:password and a username-only token alike."""
    audit = tmp_path / "audit"
    hosts = (f"https://{USER}:{WORD}@git.example", f"https://{TOKEN}@api.example")
    run = _completed_run(store, audit, tmp_path / "repo", network=hosts)
    candidate, text = capture_skill(store, audit, run, name="hosts", actor="op")
    for each in (text, candidate.template.instructions):
        _no_secret(each)
    assert "https://***@git.example, https://***@api.example" in text


def test_review_r2_a_refused_tool_call_is_not_a_step(store: RunStore, tmp_path: Path) -> None:
    """Review R2 (F5): the engine skipped a repeated call (``{"refused":
    "asked_and_answered"}``) — it never ran, so it is not a step."""
    chat = store.create_chat(title="ops", model=None).chat_id
    store.add_chat_message(chat, role="user", content="lint and read")
    url = {"url": "https://x.example"}
    store.add_chat_message(chat, role="assistant", content="", tool_calls=[
        {"id": "a", "function": {"name": "run_shell", "arguments": {"command": "make lint"}}},
        {"id": "b", "function": {"name": "read_url", "arguments": url}},
        {"id": "c", "function": {"name": "read_url", "arguments": url}},
    ])  # fmt: skip
    skipped = {"refused": "asked_and_answered", "nudge": "this exact call already ran"}
    for call_id, name, result in (
        ("a", "run_shell", {"ok": True}),
        ("b", "read_url", skipped),
        ("c", "read_url", {"refused": True}),
    ):
        store.add_chat_message(chat, role="tool", content=json.dumps(result), tool_name=name,
                               tool_call_id=call_id)  # fmt: skip
    store.add_chat_message(chat, role="assistant", content="done")
    with pytest.raises(SkillError, match="no finished multi-step turn"):
        capture_skill(store, tmp_path, chat, name="lint", actor="op")
