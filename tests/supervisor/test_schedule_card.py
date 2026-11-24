"""ADR 0059 P4-2 (D6, F3 lesson 1): the script-schedule card.

A script schedule is standing, unattended host shell as the operator. Its
card is HIGH and says exactly what will happen: the command, the interval,
the env names the script really gets, the scheduler's own next three fire
times, the chain it reads from, what it replaces, and "runs as you, on this
host, unattended, until disabled". Create, replace-by-name and chain alike."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import RunStore
from skep.supervisor.scheduler import (
    make_schedule,
    next_fire_times,
    next_run_after,
    run_schedule_script,
    script_env,
)
from skep.supervisor.serve.cards import SCRIPT_SENTENCE, card_summary, schedule_facts
from skep.supervisor.serve.channels import card_notes

from .card_rig import cards, live_fixture  # noqa: F401  (the `live` fixture)

SCRIPT = {"name": "disk", "caste": "script", "instructions": "df -h /", "every": "1d"}


@pytest.fixture()
def store(tmp_path: Path) -> Any:
    db = RunStore(tmp_path / "db.sqlite3")
    yield db
    db.close()


def _parse(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def test_a_script_schedule_card_says_what_will_run_and_is_high(
    store: RunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SKEP_FAKE_API_TOKEN", "never-shown")  # serve.env-style: never inherited
    card = card_summary("propose_schedule", SCRIPT, "", store=store)
    assert card["tier"] == "high"
    facts = card["schedule"]
    assert facts["command"] == "df -h /"
    assert facts["every"] == "1d"
    assert (
        facts["runs"]
        == SCRIPT_SENTENCE
        == (
            "runs as you, on this host, unattended, until disabled, inside the wall: "
            "besides the system and its toolchains it reads only the files its command "
            "names (the live files, read-only), and it reaches only its listed hosts "
            "(none unless listed)"
        )
    )
    assert facts["env"] == sorted(script_env())
    assert "SKEP_FAKE_API_TOKEN" not in facts["env"]
    first, second, third = (_parse(t) for t in facts["next"])
    assert abs(first - datetime.now(UTC)) < timedelta(minutes=1)  # the next tick
    assert second - first == third - second == timedelta(days=1)
    assert "chain" not in facts and "replaces" not in facts


def test_the_env_on_the_card_is_the_env_the_script_gets(tmp_path: Path) -> None:
    output, ok = run_schedule_script("env | cut -d= -f1 | sort", skep_home=tmp_path / "skep")
    assert ok
    shell_own = {"_", "PWD", "OLDPWD", "SHLVL"}  # /bin/sh sets these itself
    assert sorted(set(output.split()) - shell_own) == sorted(script_env())


def test_next_fire_times_come_from_the_scheduler(store: RunStore) -> None:
    args = {**SCRIPT, "every": "2h", "start_at": "2030-01-01T09:00:00+01:00"}
    facts = schedule_facts(args, store)
    assert facts is not None
    assert facts["next"] == next_fire_times("2030-01-01T09:00:00+01:00", 7200)
    assert facts["next"] == ["2030-01-01T08:00:00Z", "2030-01-01T10:00:00Z", "2030-01-01T12:00:00Z"]
    once = schedule_facts({**args, "once": True}, store)
    assert once is not None and once["next"] == ["2030-01-01T08:00:00Z"]
    bad = schedule_facts({**SCRIPT, "every": "forever"}, store)
    assert bad is not None and "invalid interval" in bad["error"] and "next" not in bad


def test_a_start_at_in_the_past_shows_the_runs_the_scheduler_will_make() -> None:
    """A past ``start_at`` is due at once: the scheduler fires on its next
    tick, then goes back on its slot (v122-F5: next_run_after the stored slot,
    past the tick's moment). The card listed the past start and its past
    successors as "next runs"."""
    before = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    times = next_fire_times("2020-01-01T00:00:00Z", 3600)
    assert times[0] >= before
    assert times[1] == next_run_after("2020-01-01T00:00:00Z", 3600, now=times[0])


def test_replacing_a_schedule_by_name_says_what_it_replaces(store: RunStore) -> None:
    store.add_schedule(
        make_schedule(name="disk", repo="", instructions="date", interval_seconds=60,
                      worker_kind="script")
    )  # fmt: skip
    note = {"name": "disk", "caste": "note", "instructions": "hi", "every": "1d"}
    card = card_summary("propose_schedule", note, "", store=store)
    assert card["tier"] == "high"
    assert card["schedule"]["replaces"] == "your script schedule 'disk'"
    assert "command" not in card["schedule"]  # a note runs nothing


def test_a_chain_card_walks_the_chain(store: RunStore) -> None:
    for name, chain in (("a", None), ("b", "a")):
        store.add_schedule(
            make_schedule(name=name, repo="", instructions="x", interval_seconds=60,
                          worker_kind="note", chain=chain)
        )  # fmt: skip
    args = {"name": "c", "caste": "prompt", "instructions": "sum", "every": "1d", "chain": "b"}
    card = card_summary("propose_schedule", args, "", store=store)
    assert card["tier"] == "high"
    assert card["schedule"]["chain"] == ["b", "a"]
    assert "reads the output of: b <- a" in card_notes(card)


def test_a_plain_note_schedule_has_no_schedule_block(store: RunStore) -> None:
    note = {"name": "n", "caste": "note", "instructions": "hi", "every": "1d"}
    card = card_summary("propose_schedule", note, "", store=store)
    assert "schedule" not in card and "tier" not in card


def test_channels_and_the_cli_list_the_facts(store: RunStore) -> None:
    notes = card_notes(card_summary("propose_schedule", SCRIPT, "", store=store))
    assert notes[0].startswith("HIGH risk: a script schedule runs a command as you")
    assert "command: df -h /" in notes and "every: 1d" in notes
    assert any(n.startswith("next runs: ") for n in notes)
    assert any(n.startswith("environment: ") for n in notes)
    assert notes[-1] == SCRIPT_SENTENCE


def test_the_model_proposed_card_records_the_facts_and_a_resolved_card_shows_them(
    live: Any,
) -> None:
    env = live("builder")
    chat = env.chat()
    env.ollama.script_tool_call("propose_schedule", SCRIPT)
    (card,) = cards(env.turn(chat, "check my disk daily"))
    assert card["card"]["tier"] == "high"
    assert card["card"]["schedule"]["command"] == "df -h /"
    (pending,) = env.store.pending_chat_actions(chat)
    assert pending.card is not None and pending.card["schedule"]["command"] == "df -h /"
    env.store.resolve_chat_action(pending.action_id, status="denied", result={"ok": False})
    resolved = env.store.get_chat_action(pending.action_id)
    assert resolved is not None
    shown = env.engine._card(chat, resolved.tool, resolved.args, action=resolved)
    assert shown["schedule"] == pending.card["schedule"]  # as recorded, not recomputed


def test_a_worker_schedule_card_says_its_role_and_what_the_role_narrows(live: Any) -> None:
    """v123-F1: a worker schedule a builder chat proposes runs as builder;
    the card says so, and exactly what a role narrows (and that asks drop)."""
    from skep.supervisor.serve.actions import SCHEDULE_ROLE_NOTE

    env = live("builder")
    chat = env.chat(role="builder")
    proposal = {"name": "n", "repo": str(env.paths["repo"]), "every": "1d", "instructions": "x"}
    env.ollama.script_tool_call("propose_schedule", proposal)
    (card,) = cards(env.turn(chat, "tidy nightly"))
    runs = f"runs as role builder: {SCHEDULE_ROLE_NOTE}"
    assert card["card"]["schedule"]["runs"] == runs
    (pending,) = env.store.pending_chat_actions(chat)
    assert pending.card is not None and pending.card["schedule"]["runs"] == runs
    assert "dropped from its grants" in runs and "env_allowlist" in runs


def test_the_role_note_names_everything_a_role_leaves_untouched() -> None:
    """v123-F1 review (I8): the note's list of what a role does not touch is
    complete — every Permissions field ``delegate_permissions`` leaves as it
    is — and says truly what happens to what lies beyond the role."""
    from skep.supervisor.serve.actions import SCHEDULE_ROLE_NOTE

    for untouched in ("env_allowlist", "execution mode", "auto-apply", "the budget",
                      "allow_git_mutation", "loopback_ports", "allowed_tools",
                      "allowed_plugin_risks"):  # fmt: skip
        assert untouched in SCHEDULE_ROLE_NOTE, untouched
    assert "asks are dropped on a schedule" not in SCHEDULE_ROLE_NOTE
    assert "dropped from its grants" in SCHEDULE_ROLE_NOTE
    assert "a command beyond it stops at a gate for you" in SCHEDULE_ROLE_NOTE


@pytest.mark.parametrize("role", ["assistant", None])
def test_a_worker_schedule_its_chat_role_cannot_run_is_refused_without_a_card(
    live: Any, role: str | None
) -> None:
    """v123-F1/F2 review (B22/B122): a worker schedule from an assistant (or
    role-less) chat could only fail at confirm — refused now, no card, and
    the refusal teaches what works."""
    from .conftest import git

    env = live("builder")
    git(env.paths["repo"], "init", "-q", "-b", "main")
    chat = env.chat(role=role)
    proposal = {"name": "n", "repo": str(env.paths["repo"]), "every": "1d", "instructions": "x"}
    env.ollama.script_tool_call("propose_schedule", proposal)
    events = env.turn(chat, "tidy nightly")
    assert cards(events) == [] and env.store.pending_chat_actions(chat) == []
    (result,) = [data["result"] for event, data in events if event == "tool"]
    said = json.dumps(result)
    assert "role assistant cannot run a coding: it denies workspace writes" in said
    assert "runs in this chat's role" in said and "/role" in said
    assert env.store.get_schedule("n") is None


def test_propose_schedule_says_a_worker_schedule_runs_in_the_chat_role() -> None:
    from skep.supervisor.serve.tools import MUTATING_TOOL_SPECS

    (spec,) = [s for s in MUTATING_TOOL_SPECS if s["function"]["name"] == "propose_schedule"]
    said = spec["function"]["description"]
    assert "A worker schedule runs in this chat's role" in said
    assert "an assistant chat cannot schedule workers" in said


def test_replacing_a_schedule_says_when_its_role_changes(live: Any) -> None:
    """v123-F2 review (I8): re-proposing a schedule by name never changes its
    role silently — the ``replaces`` line says was → now."""
    env = live("builder")
    chat = env.chat(role="builder")
    repo = str(env.paths["repo"])
    for name, role in (("bare", None), ("ops", "operator"), ("same", "builder")):
        env.store.add_schedule(
            make_schedule(name=name, repo=repo, instructions="x", interval_seconds=60, role=role)
        )
    replaced = {}
    for name in ("bare", "ops", "same"):
        env.ollama.script_tool_call(
            "propose_schedule", {"name": name, "repo": repo, "every": "1d", "instructions": "x"}
        )
        (card,) = cards(env.turn(chat, f"redo {name}"))
        replaced[name] = card["card"]["schedule"]["replaces"]
        env.store.resolve_chat_action(card["action_id"], status="denied", result={"ok": False})
    assert replaced == {
        "bare": "your coding schedule 'bare' (was no role → role builder)",
        "ops": "your coding schedule 'ops' (was role operator → role builder)",
        "same": "your coding schedule 'same'",
    }
    # a repo-less kind stores no role: replacing a role-bound schedule says so
    note = {"name": "ops", "caste": "note", "instructions": "hi", "every": "1d"}
    facts = schedule_facts(note, env.store)
    assert facts is not None
    assert facts["replaces"] == "your coding schedule 'ops' (was role operator → no role)"


def test_a_confirm_after_the_chat_role_changed_refuses(live: Any) -> None:
    """v123-F2 review (I8): the card shows the role at proposal; a confirm
    after the chat's role changed would store another — it refuses, saying
    so, and nothing is stored. An unchanged role confirms as shown."""
    from .conftest import git

    env = live("builder")
    git(env.paths["repo"], "init", "-q", "-b", "main")
    chat = env.chat(role="builder")
    repo = str(env.paths["repo"])
    for name in ("moved", "kept"):
        env.ollama.script_tool_call(
            "propose_schedule", {"name": name, "repo": repo, "every": "1d", "instructions": "x"}
        )
        (card,) = cards(env.turn(chat, f"schedule {name}"))
        if name == "moved":
            env.store.set_chat_role(chat, "operator")  # "kept" is proposed as operator
        env.ollama.script_reply("ok")
        env.verdict(chat, card["action_id"], confirm=True, actor="chat-user", keep="once")
        resolved = env.store.get_chat_action(card["action_id"])
        assert resolved is not None and resolved.result is not None
        if name == "moved":
            assert resolved.result["ok"] is False
            assert resolved.result["error"] == (
                "this chat's role changed since the card was shown (builder → operator); "
                "propose the schedule again"
            )
            assert env.store.get_schedule("moved") is None
        else:
            assert resolved.result["ok"] is True, resolved.result
            stored = env.store.get_schedule("kept")
            assert stored is not None and stored.role == "operator"


def test_propose_schedule_caste_help_names_the_worker_castes_and_the_role_rule() -> None:
    from skep.supervisor.serve.tools import MUTATING_TOOL_SPECS

    (spec,) = [s for s in MUTATING_TOOL_SPECS if s["function"]["name"] == "propose_schedule"]
    help_text = spec["function"]["parameters"]["properties"]["caste"]["description"]
    assert "None of these dispatch a worker" not in help_text
    assert "these four dispatch no worker" in help_text
    assert "'coding', 'audit', 'researcher' and 'document' dispatch a worker" in help_text
    assert "in this chat's role" in help_text


# --- Phase 09 R1: a script schedule runs inside the wall (9D's wall.py) ---------


def test_a_chat_created_script_schedule_keeps_the_hosts_it_declares(tmp_path: Path) -> None:
    """The chat face stores a script schedule's `network` (its only egress
    inside the wall, none by default), as the REST and CLI faces do; other
    repo-less kinds reach nothing and store none."""
    from skep.supervisor import SupervisorConfig
    from skep.supervisor.serve.settings import ConfigHolder
    from skep.supervisor.serve.tools import MUTATING_TOOL_SPECS, execute_mutation

    config = SupervisorConfig(home=tmp_path / "skep-home", worker_command=("true",))
    store = RunStore(config.db_path)
    try:
        chat_id = store.create_chat(title="c", model=None).chat_id
        for args in (
            {**SCRIPT, "name": "wx", "network": ["api.weather.gov"]},
            {**SCRIPT, "name": "quiet"},
            {"name": "memo", "caste": "note", "instructions": "hi", "every": "1d",
             "network": ["x.example"]},
        ):  # fmt: skip
            execute_mutation(
                "propose_schedule",
                args,
                store=store,
                holder=ConfigHolder(config, store),
                runner=None,  # type: ignore[arg-type]
                actor="tester",
                chat_id=chat_id,
            )
        stored = {name: store.get_schedule(name) for name in ("wx", "quiet", "memo")}
    finally:
        store.close()

    assert {name: s.network for name, s in stored.items() if s} == {
        "wx": ["api.weather.gov"], "quiet": [], "memo": []
    }  # fmt: skip
    spec = next(t for t in MUTATING_TOOL_SPECS if t["function"]["name"] == "propose_schedule")
    described = spec["function"]["parameters"]["properties"]["network"]["description"]
    assert "script schedule" in described and "none by default" in described


def test_a_script_card_names_its_hosts_and_the_proxy_env_it_adds(store: RunStore) -> None:
    from skep.supervisor.spawner import proxy_env

    hosts = ["api.weather.gov", "imap.example.com"]
    facts = schedule_facts({**SCRIPT, "network": hosts}, store)
    quiet = schedule_facts(SCRIPT, store)

    assert facts is not None and quiet is not None
    assert facts["hosts"] == hosts and quiet["hosts"] == []
    assert facts["env"] == sorted({*script_env(), *proxy_env(0)})
    assert quiet["env"] == sorted(script_env())
    assert "inside the wall" in facts["runs"] and "the files its command names" in facts["runs"]


def test_the_script_high_line_says_it_runs_inside_the_wall(store: RunStore) -> None:
    card = card_summary("propose_schedule", SCRIPT, "", store=store)
    assert card["tier"] == "high"
    assert "inside the wall" in card["high"] and "hosts" in card["high"]
