"""ADR 0059 P4-1 (D5, V9): the trifecta guard — two taint bits per chat.

The frozen table (``taint_grid``) holds every Queen lane to its pre-P4-1
answer: with taint ``none`` every cell is byte-identical, and with taint set
the only change is an allow becoming a ``taint/<rule>`` ask, in exactly the
cells the D5 table names. Then the closing tests: f1-07, B26, each D5 row,
the laundering paths, the schema upgrade, the strict-provider replay."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from fastapi import HTTPException

from skep.supervisor import RunStore
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.serve import actions
from skep.supervisor.serve.cards import card_summary
from skep.supervisor.serve.chat import ChatEngine
from skep.supervisor.serve.jobs import Dispatcher
from skep.supervisor.serve.llm import LLM_BASE_URL, LLM_DEFAULT_MODEL
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import mutation_execution_decision
from skep.supervisor.store import ChatTaint

from .fake_ollama import FakeOllama
from .net_lane_grid import build_net_home, fakes
from .taint_grid import HOMES, rows, set_taint

FROZEN: dict[str, str] = json.loads(
    (
        Path(__file__).parents[1] / "fixtures" / "policy_v3" / "pre_p4_1_taint_verdicts.json"
    ).read_text()
)
# The probes that send model-chosen text out (D5 "private only"), every
# egress (D5 "both"), and the consequential actions (D5 "untrusted only").
_MCP_WRITES = {"plain-write", "kb-write", "granted", "bank-write", "mail-send", "browse-act"}
TEXT_EGRESS = {("fetch", p) for p in ("baseline-allow", "template-github", "granted",
                                      "overlay-allow", "unknown", "baseline-deny")}  # fmt: skip
TEXT_EGRESS |= {("search", "query")} | {("mcp", p) for p in _MCP_WRITES}
EGRESS = TEXT_EGRESS | {("mcp", p) for p in ("plain-read", "kb-read", "cal-read", "mail-read",
                                             "browse-read")}  # fmt: skip
ACTIONS = {("mcp", p) for p in _MCP_WRITES} | {("other", "remember"), ("other", "no-lane")}
ACTIONS |= {("shell", p) for p in ("ls", "granted", "curl-post", "secret", "repo", "bg",
                                   "bg-granted")}  # fmt: skip
ACTIONS |= {("dispatch", p) for p in ("auto", "gated", "quick-edit", "run-code", "batch")}
RULE = {"private": "private-egress", "untrusted": "untrusted-action", "both": "trifecta"}
GATED = {"private": TEXT_EGRESS, "untrusted": ACTIONS, "both": EGRESS | ACTIONS}


def classify(key: str, old: str, new: str) -> str:
    """``same`` or ``<taint>:<lane>`` for an allow that became the D5 ask;
    raises on any other change (so a test fails on it)."""
    _home, lane, probe, _role, _grant, taint = key.split("|")
    if old == new:
        return "same"
    assert taint != "none", f"taint none changed: {key}: {old} -> {new}"
    assert old.startswith("allow "), f"only an allow may move: {key}: {old} -> {new}"
    assert (lane, probe) in GATED[taint], f"not a D5 cell: {key}: {old} -> {new}"
    # search_web's read path cannot card: it refuses with the taint label; a
    # live turn turns exactly that refusal into a card (test_f1_07_*).
    verdict = "deny" if lane == "search" else "require_approval"
    assert new == f"{verdict} taint/{RULE[taint]}", f"wrong ask: {key}: {old} -> {new}"
    return f"{taint}:{lane}"


# Every flip against the frozen table, per home (the round's report lists
# them). Identical rows per taint state: shell 8 = the granted run_shell and
# start_process x 4 roles in two grant states; dispatch 36 = the four auto
# lanes x 4 roles x 3 grant states (the gated one never allowed); other 12 =
# remember x 4 roles x 3 grant states.
_SAME = {"dispatch": 36, "other": 12, "shell": 8}
_UNTRUSTED = {f"untrusted:{lane}": n for lane, n in _SAME.items()} | {"untrusted:mcp": 8}
_BOTH = {f"both:{lane}": n for lane, n in _SAME.items()}


def _flips(fetch: int, both_mcp: int, search: int) -> dict[str, int]:
    flips = {**_UNTRUSTED, **_BOTH, "both:fetch": fetch, "both:mcp": both_mcp}
    flips |= {"private:fetch": fetch, "private:mcp": 8}
    if search:
        flips |= {"both:search": search, "private:search": search}
    return flips


EXPECTED_FLIPS: dict[str, dict[str, int]] = {
    "legacy": _flips(fetch=8, both_mcp=29, search=0),
    "zero": _flips(fetch=8, both_mcp=8, search=0),
    "builder": _flips(fetch=11, both_mcp=11, search=12),
    "assistant": _flips(fetch=11, both_mcp=23, search=12),
    "operator": _flips(fetch=17, both_mcp=11, search=12),
    # tampered: nothing allows but remember (an inert proposal, outside decide)
    "tampered": {"untrusted:other": 12, "both:other": 12},
}


@pytest.mark.parametrize("kind", HOMES)
def test_taint_only_turns_allows_into_asks_in_the_d5_cells(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Homes x roles x grant states x taint states x every Queen lane, held
    to the pre-P4-1 answers: zero flips with taint none; with taint set,
    only allow -> taint/<rule> ask, only where D5 says."""
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.chdir(tmp_path)
    seen: Counter[str] = Counter()
    for key, shown in rows(tmp_path / kind, kind):
        frozen = FROZEN[key.rsplit("|", 1)[0] + "|none"]
        seen[classify(key, frozen, shown)] += 1
    del seen["same"]
    assert dict(seen) == EXPECTED_FLIPS[kind]


# --- the closing tests -----------------------------------------------------------------


@dataclass
class Live:
    """A chat engine over a net_lane_grid home and a fake model."""

    paths: dict[str, Path]
    store: RunStore
    holder: ConfigHolder
    engine: ChatEngine
    ollama: FakeOllama
    searched: list[str]

    def chat(self, role: str | None = None) -> str:
        return self.store.create_chat(title="t", model=None, role=role).chat_id

    def _llm(self, chat_id: str) -> dict[str, Any]:
        base_url, api_key, model, protocol = self.engine.resolved_llm(self.store.get_chat(chat_id))
        return {"base_url": base_url, "api_key": api_key, "model": model, "protocol": protocol}

    def turn(self, chat_id: str, text: str, **kw: Any) -> list[tuple[Any, dict[str, Any]]]:
        self.store.add_chat_message(chat_id, role="user", content=text)
        return list(self.engine.turn_events(chat_id, **self._llm(chat_id), **kw))

    def confirm(
        self, chat_id: str, action_id: str, keep: str | None = None
    ) -> list[tuple[Any, dict[str, Any]]]:
        chat = self.store.get_chat(chat_id)
        return list(self.engine.verdict_events(chat_id, chat, action_id, confirm=True, keep=keep))

    def pending(self, chat_id: str) -> list[Any]:
        return self.store.pending_chat_actions(chat_id)


@pytest.fixture()
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], Live]]:
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.chdir(tmp_path)
    made: list[Live] = []
    searched: list[str] = []

    def search(query: str, **_: Any) -> list[dict[str, Any]]:
        searched.append(query)
        return [{"title": "t", "href": "https://result.example/", "body": "r"}]

    with fakes(), mock.patch("skep.supervisor.serve.websearch.search_web", search):

        def make(kind: str) -> Live:
            paths = build_net_home(tmp_path / kind, kind)
            config = build_config(paths["personal"], None)
            store = RunStore(config.db_path)
            holder = ConfigHolder(config, store)
            ollama = FakeOllama(api_key=None).start()
            store.set_setting(LLM_BASE_URL, ollama.base_url)
            store.set_setting(LLM_DEFAULT_MODEL, "qwen3")
            engine = ChatEngine(
                store=store, holder=holder, runner=Dispatcher(holder, store),
                home=holder.current.home,
            )  # fmt: skip
            made.append(Live(paths, store, holder, engine, ollama, searched))
            return made[-1]

        yield make
    for item in made:
        item.ollama.stop()
        item.store.close()


def _action(events: list[tuple[Any, dict[str, Any]]]) -> dict[str, Any]:
    found = [data for event, data in events if event == "action"]
    assert len(found) == 1, events
    return found[0]


def test_f1_07_a_read_outside_the_roots_then_search_web_asks_and_says_why(
    live: Callable[[str], Live],
) -> None:
    """f1-07 on its real path: read_file outside the roots (carded, confirmed)
    sets ``private``; the search_web after it — allowed by the builder
    baseline's ``net-search`` AND by a learned grant — cards, and the card
    says why. Nothing is searched until the operator says yes, once."""
    env = live("builder")
    actions.learn_policy_rule(
        env.store, rule_id="network:search:ddgs", action="search", pattern="ddgs",
        scope="network", provenance="allow-always:op",
    )  # fmt: skip
    chat = env.chat()
    env.ollama.script_tool_call("read_file", {"path": str(env.paths["outside"] / "notes.txt")})
    read_card = _action(env.turn(chat, "open notes.txt and search the web for line 2"))
    assert read_card["tool"] == "read_file"
    assert env.store.get_chat_taint(chat) == ChatTaint()  # nothing read yet

    env.ollama.script_tool_call("search_web", {"query": "CANARY-F1L-10"})
    events = env.confirm(chat, read_card["action_id"])
    taint = env.store.get_chat_taint(chat)
    assert taint.private is not None and taint.untrusted is None
    card = _action(events)
    assert card["tool"] == "search_web"
    assert card["decision"]["decided_by"] == "taint/private-egress"
    assert card["card"]["taint"] == (
        "this chat has read private data (it read a file outside your folders), so "
        "sending anything out from it needs your OK, even where a rule or grant allows it"
    )
    assert "keep" not in card["card"] and "tier" not in card["card"]
    assert env.searched == []

    env.ollama.script_reply("found it")
    env.confirm(chat, card["action_id"])
    assert env.searched == ["CANARY-F1L-10"]
    # once only: the next search in this chat asks again
    env.ollama.script_tool_call("search_web", {"query": "again"})
    assert _action(env.turn(chat, "search again"))["decision"]["decided_by"] == (
        "taint/trifecta"  # the search results it read set ``untrusted`` too
    )
    assert env.searched == ["CANARY-F1L-10"]


def test_a_read_inside_the_roots_or_a_refused_read_sets_no_taint(
    live: Callable[[str], Live],
) -> None:
    env = live("builder")
    chat = env.chat()
    env.ollama.script_tool_call("read_file", {"path": str(env.paths["repo"] / "README.md")})
    env.ollama.script_tool_call("read_file", {"path": str(Path.home() / ".ssh" / "id_rsa")})
    env.ollama.script_tool_call("search_web", {"query": "q"})
    env.ollama.script_reply("done")
    env.turn(chat, "read the readme, then the key, then search")
    assert env.store.get_chat_taint(chat) == ChatTaint(untrusted=_last_tool_row(env, chat))
    assert env.searched == ["q"]  # no private bit: the search ran in-turn


def _last_tool_row(env: Live, chat: str) -> int:
    return max(m.id for m in env.store.chat_messages(chat) if m.role == "tool")


def test_run_shell_output_sets_private_and_search_web_then_asks(
    live: Callable[[str], Live],
) -> None:
    """Operator decision #3: ANY run_shell output — even ``echo`` — sets
    ``private``; one search card follows per request."""
    env = live("builder")
    chat = env.chat()
    env.ollama.script_tool_call(
        "run_shell", {"command": "echo hi", "cwd": str(env.paths["outside"])}
    )
    shell_card = _action(env.turn(chat, "echo hi then search for it"))
    env.ollama.script_tool_calls(("search_web", {"query": "a"}), ("search_web", {"query": "b"}))
    events = env.confirm(chat, shell_card["action_id"])
    assert env.store.get_chat_taint(chat).private is not None
    cards = [d for e, d in events if e == "action"]
    assert [c["tool"] for c in cards] == ["search_web"]  # one card, not two
    waiting = [d for e, d in events if e == "tool" and d["tool"] == "search_web"]
    assert "already waiting" in waiting[0]["result"]["error"]
    assert env.searched == []


@pytest.mark.parametrize(
    ("bits", "probe", "expected"),
    [
        # private only: egress carrying model-chosen text asks; reads do not
        ("private", ("read_url", {"url": "https://docs.python.org/3/"}), "taint/private-egress"),
        ("private", ("call_mcp_tool", {"server_id": "tracker", "tool": "update_issue"}),
         "taint/private-egress"),
        ("private", ("call_mcp_tool", {"server_id": "kb", "tool": "get_doc"}), "allow"),
        ("private", ("run_shell", {"command": "python3 build.py --fast"}), "allow"),
        # untrusted only: consequential actions ask; egress does not
        ("untrusted", ("run_shell", {"command": "python3 build.py --fast"}),
         "taint/untrusted-action"),
        ("untrusted", ("call_mcp_tool", {"server_id": "tracker", "tool": "update_issue"}),
         "taint/untrusted-action"),
        ("untrusted", ("read_url", {"url": "https://docs.python.org/3/"}), "allow"),
        ("untrusted", ("call_mcp_tool", {"server_id": "kb", "tool": "get_doc"}), "allow"),
        # both: every egress and every action
        ("both", ("call_mcp_tool", {"server_id": "kb", "tool": "get_doc"}), "taint/trifecta"),
        ("both", ("read_url", {"url": "https://docs.python.org/3/"}), "taint/trifecta"),
        ("both", ("run_shell", {"command": "python3 build.py --fast"}), "taint/trifecta"),
        ("both", ("read_file", {"path": "{repo}/README.md"}), "allow"),
    ],
)  # fmt: skip
def test_each_row_of_the_d5_table(
    live: Callable[[str], Live], bits: str, probe: tuple[str, dict[str, Any]], expected: str
) -> None:
    env = live("builder")
    chat = env.chat()
    actions.remember_action_for_session(
        env.store, tool="run_shell", args={"command": "python3 build.py --fast"}, actor="op",
        tier="always",
    )  # fmt: skip
    actions.learn_policy_rule(
        env.store, rule_id="mcp:tracker:update_issue", action="call",
        pattern="tracker:update_issue", scope="mcp", provenance="allow-always:op",
    )  # fmt: skip
    set_taint(env.store, chat, bits)
    tool, raw = probe
    args = {k: v.format(repo=env.paths["repo"]) for k, v in raw.items()}
    decision = mutation_execution_decision(
        tool, args, store=env.store, holder=env.holder, chat_id=chat
    )
    assert decision is not None
    shown = decision.verdict if decision.verdict == "allow" else decision.decided_by
    assert shown == expected, decision
    if expected.startswith("taint/"):
        assert decision.verdict == "require_approval"
        assert decision.detail and "your OK" in decision.detail


def test_taint_never_turns_a_deny_into_an_ask_and_a_floor_deny_stays(
    live: Callable[[str], Live],
) -> None:
    env = live("builder")
    chat = env.chat()
    set_taint(env.store, chat, "both")
    for tool, args in (
        ("read_url", {"url": "https://evil.example/x"}),
        ("call_mcp_tool", {"server_id": "bank", "tool": "transfer"}),
        ("run_shell", {"command": "cat ~/.aws/credentials"}),
        ("read_file", {"path": str(Path.home() / ".ssh" / "id_rsa")}),
    ):
        decision = mutation_execution_decision(
            tool, args, store=env.store, holder=env.holder, chat_id=chat
        )
        assert decision is not None and decision.verdict == "deny", (tool, decision)


@pytest.mark.parametrize(
    ("bits", "tool", "args", "keeps"),
    [
        ("none", "read_url", {"url": "https://x.example/a"}, ["once", "session", "always"]),
        ("untrusted", "read_url", {"url": "https://x.example/a"}, ["once", "session"]),
        ("private", "read_url", {"url": "https://x.example/a"}, ["once"]),
        ("both", "read_url", {"url": "https://x.example/a"}, ["once"]),
        ("private", "run_shell", {"command": "ls"}, ["once", "session", "always"]),
        ("untrusted", "run_shell", {"command": "ls"}, ["once", "always"]),
        ("both", "run_shell", {"command": "ls"}, ["once"]),
    ],
)
def test_a_tainted_chat_offers_fewer_keeps_and_the_card_matches_the_server(
    tmp_path: Path, bits: str, tool: str, args: dict[str, Any], keeps: list[str]
) -> None:
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        chat = store.create_chat(title="t", model=None).chat_id
        set_taint(store, chat, bits)
        taint = store.get_chat_taint(chat)
        card = card_summary(tool, args, "", taint=taint)
        assert card["keep"] == keeps == actions.keep_options(tool, args, taint)
        for keep in ("once", "session", "always"):
            refused = actions.keep_refusal(tool, args, keep, taint)
            assert (refused is None) == (keep in keeps), (keep, refused)
        assert (card.get("tier") == "high") == (bits == "both")
    finally:
        store.close()


def test_the_server_refuses_a_wider_keep_on_a_doubly_tainted_chat_with_a_400(
    live: Callable[[str], Live],
) -> None:
    env = live("builder")
    chat = env.chat()
    env.ollama.script_tool_call("read_url", {"url": "https://unknown.example/a"})
    card = _action(env.turn(chat, "fetch it"))
    set_taint(env.store, chat, "both")
    with pytest.raises(HTTPException) as refused:
        env.confirm(chat, card["action_id"], keep="session")
    assert refused.value.status_code == 400
    assert "hold once" in str(refused.value.detail)
    env.ollama.script_reply("ok")
    env.confirm(chat, card["action_id"])  # no keep named: once, accepted
    resolved = env.store.get_chat_action(card["action_id"])
    assert resolved is not None and resolved.status == "confirmed"


def test_an_unattended_turn_refuses_a_taint_ask_instead_of_carding(
    live: Callable[[str], Live],
) -> None:
    """A run-finish continuation cards every mutation (ES-F3) — but a taint
    ask there is a refusal, never a waiting card: nobody may be watching."""
    env = live("builder")
    chat = env.chat()
    actions.remember_action_for_session(
        env.store, tool="run_shell", args={"command": "python3 build.py --fast"}, actor="op",
        tier="always",
    )  # fmt: skip
    set_taint(env.store, chat, "untrusted")
    env.ollama.script_tool_call("run_shell", {"command": "python3 build.py --fast"})
    env.ollama.script_reply("done")
    env.turn(chat, "[run finished]", unattended=True)
    assert env.pending(chat) == []
    row = [m for m in env.store.chat_messages(chat) if m.tool_name == "run_shell"][-1]
    assert "nobody is watching" in row.content and "taint/untrusted-action" in row.content


def test_taint_is_sticky_across_a_serve_restart_and_compaction(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    store = RunStore(db)
    chat = store.create_chat(title="t", model=None).chat_id
    first = store.add_chat_message(chat, role="tool", tool_name="run_shell", content="{}")
    store.add_chat_taint(chat, private=first)
    store.add_chat_taint(chat, private=first + 99, untrusted=7)  # the first setter keeps a bit
    store.set_chat_context(chat, summary="folded", compacted_through=first + 1000)
    store.close()
    reopened = RunStore(db)
    try:
        assert reopened.get_chat_taint(chat) == ChatTaint(private=first, untrusted=7)
    finally:
        reopened.close()


@pytest.mark.parametrize("stored", ['"private"', "[1, 2]", '{"private": "x"}', "not json", None])
def test_an_unreadable_taint_or_a_missing_chat_counts_as_both_bits(
    tmp_path: Path, stored: str | None
) -> None:
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        chat = store.create_chat(title="t", model=None).chat_id
        if stored is None:
            chat = "no-such-chat"
        else:
            store._conn.execute("UPDATE chats SET taint = ? WHERE chat_id = ?", (stored, chat))
        taint = store.get_chat_taint(chat)
        assert taint.private is not None and taint.untrusted is not None
    finally:
        store.close()


def test_a_store_from_before_p4_1_opens_upgrades_in_place_and_reads_untainted(
    tmp_path: Path,
) -> None:
    """An old database (no ``chats.taint``, no ``chat_messages.provenance``)
    opens, gains both columns, and reads as before: no chat is tainted, each
    row's author is the one its role implies — nothing retroactively taints."""
    import sqlite3

    db = tmp_path / "old.sqlite3"
    store = RunStore(db)
    chat = store.create_chat(title="old", model=None).chat_id
    store.add_chat_message(chat, role="user", content="hi")
    store.add_chat_message(chat, role="assistant", content="hello")
    store.add_chat_message(chat, role="tool", tool_name="run_shell", content="{}")
    store.close()
    with sqlite3.connect(db) as conn:  # the schema as it stood before P4-1
        conn.execute("ALTER TABLE chats DROP COLUMN taint")
        conn.execute("ALTER TABLE chat_messages DROP COLUMN provenance")
    upgraded = RunStore(db)
    try:
        assert upgraded.get_chat_taint(chat) == ChatTaint()
        rows = upgraded.chat_messages(chat)
        assert [(r.role, r.provenance, r.trusted) for r in rows] == [
            ("user", "operator", True), ("assistant", "queen", True), ("tool", "tool", True),
        ]  # fmt: skip
    finally:
        upgraded.close()
