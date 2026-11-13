"""ADR 0059 P3-3: read_url, search_web, call_mcp_tool and the browser tools
decide through ``policy_v3.decide()``. Nothing they decide is more permissive
than before, except (a) a human-approved learned grant answering a role's
catch-all ask, and (b) a baseline or template rule that names the host or
tool now applying where the lane used to ignore it. An operator-overlay
fetch allow is not one of them: it does not let read_url run (operator
decision, P3-3)."""

from __future__ import annotations

import io
import json
import urllib.parse
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from skep.cli import main
from skep.supervisor import RunStore
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.policy_schema import _VERDICT_RANK, POLICY_DOCUMENT_SETTINGS_KEY
from skep.supervisor.roles import SHIPPED_ROLES
from skep.supervisor.serve.actions import policy_decide, remember_action_for_session
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import (
    execute_mutation,
    execute_read_tool,
    fetch_grant_decision,
    mutation_execution_decision,
)

from .net_lane_grid import (
    FETCH_PROBES,
    HOMES,
    MCP_PROBES,
    ROLES,
    _grant,
    build_net_home,
    fake_runner,
    fakes,
    rows,
)

FROZEN: dict[str, str] = json.loads(
    (
        Path(__file__).parents[1] / "fixtures" / "policy_v3" / "pre_p3_3_net_verdicts.json"
    ).read_text()
)
_RANK = {**_VERDICT_RANK, "card": _VERDICT_RANK["require_approval"]}
CATCH_ALL_ASKS = ("/net-fetch-gated", "/net-gated", "/mcp-gated")
# (b): the rules that name a host or tool and now reach the lane.
NAMED_ALLOWS = (
    "template:operator/net-fetch-github",
    "template:operator/net-fetch-github-api",
    "/fetch-docs",
    "/mcp-kb",
    "/mcp-calendar",
    "template:builder/net-search",
    "template:operator/net-search",
    "template:assistant/net-search",
)


LOCKED = ("builder", "assistant", "operator", "upgraded", "stale-global")


def _allowed_kind(grant: str, old_by: str, new_by: str) -> str | None:
    """Which of the allowed loosenings a new allow is, or None."""
    granted = grant != "none" and new_by.startswith(("grant:session/", "grant:global/"))
    if granted and (not old_by or old_by == "-" or old_by.endswith(CATCH_ALL_ASKS)):
        return "looser:grant"
    if new_by == "global/overlay-fetch":  # never a grant this round (operator decision)
        raise AssertionError(f"an overlay fetch allow granted: {new_by}")
    if new_by.endswith(NAMED_ALLOWS) or any(new_by.startswith(n) for n in NAMED_ALLOWS):
        return "looser:named-allow"
    return None


def classify(key: str, old: str, new: str) -> str:
    """``looser:<kind>`` (only the two allowed kinds), ``stricter``, ``label``
    or ``same``; raises on any other loosening."""
    kind, lane, _probe, _role, grant = key.split("|")
    if lane == "fetch-run":
        old_ran, new_ran = old.startswith("ran "), new.startswith("ran ")
        if new_ran and not old_ran:
            raise AssertionError(f"more permissive: {key}: {old} -> {new}")
        if old_ran and not new_ran:
            return "stricter"
        if not old_ran:
            return "same" if old == new else "label"
        _, old_by, old_size, old_hops = old.split(" ")
        _, new_by, new_size, new_hops = new.split(" ")
        if (old_size, new_size) == ("big", "small"):
            # It ran in-turn; now a human confirms a card first, and the card
            # lane refuses a denied redirect hop (it used to follow them all).
            return "stricter"
        if (old_size, new_size) == ("small", "big"):
            # In-turn now: the decision row's own flip, classified on that row.
            found = _allowed_kind(grant, "", new_by)
            if found is None:
                raise AssertionError(f"more permissive: {key}: {old} -> {new}")
            return found
        old_hops, new_hops = old_hops.removeprefix("hops="), new_hops.removeprefix("hops=")
        if new_hops == "follow" and old_hops != "follow":
            raise AssertionError(f"redirects unguarded: {key}: {old} -> {new}")
        if old_hops != "follow":
            hops = [i for i, (o, n) in enumerate(zip(old_hops, new_hops, strict=True))
                    if o == "F" and n == "T"]  # fmt: skip
            # (b) a granted read's redirect hop re-decides against the baseline
            # too: docs.python.org (the 4th hop) is a baseline allow there.
            if hops == [3] and kind in LOCKED:
                return "looser:baseline-hop"
            if hops:
                raise AssertionError(f"redirect widened: {key}: {old} -> {new}")
        if old_hops != new_hops or old_size != new_size:
            return "stricter"
        return "same" if old_by == new_by else "label"
    old_verdict, old_by = old.split(" ", 1)
    new_verdict, new_by = new.split(" ", 1)
    assert "error" not in (old_verdict, new_verdict), (key, old, new)
    if _RANK[new_verdict] < _RANK[old_verdict]:
        found = _allowed_kind(grant, old_by, new_by)
        if found is None:
            raise AssertionError(f"more permissive: {key}: {old} -> {new}")
        return found
    if _RANK[new_verdict] > _RANK[old_verdict]:
        return "stricter"
    return "same" if old_by == new_by else "label"


def flips(kind: str, root: Path) -> Counter[str]:
    seen: Counter[str] = Counter()
    for key, shown in rows(root, kind):
        seen[classify(key, FROZEN[key], shown)] += 1
    return seen


@pytest.mark.parametrize("kind", HOMES)
def test_no_network_lane_verdict_is_more_permissive_than_before(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The property over home x role x grant x every probe, held to what the
    lanes decided before P3-3 (frozen)."""
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.chdir(tmp_path)
    seen = flips(kind, tmp_path)
    if kind in ("zero", "tampered"):  # the baseline guard answers: nothing loosens
        assert not any(k.startswith("looser") for k in seen), seen


# --- the regressions, one per ledger row ---------------------------------------------------


@pytest.fixture()
def net(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[str], tuple[dict[str, Path], RunStore, ConfigHolder, list[Any]]]]:
    """``net(kind)`` → ``(paths, store, holder, fetched)`` over a net_lane_grid
    home, inside the fakes (no fetch, search or MCP call leaves the machine)."""
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.chdir(tmp_path)
    opened: list[RunStore] = []
    with fakes() as fetched:

        def make(kind: str) -> tuple[dict[str, Path], RunStore, ConfigHolder, list[Any]]:
            paths = build_net_home(tmp_path / kind, kind)
            config = build_config(paths["personal"], None)
            store = RunStore(config.db_path)
            opened.append(store)
            return paths, store, ConfigHolder(config, store), fetched

        yield make
    for store in opened:
        store.close()


def _fetch(store: RunStore, holder: ConfigHolder, url: str, chat: str | None = None) -> Any:
    return mutation_execution_decision(
        "read_url", {"url": url}, store=store, holder=holder, chat_id=chat
    )


def _mcp(store: RunStore, holder: ConfigHolder, tool: str, chat: str | None = None) -> Any:
    server, name = tool.split(":")
    return mutation_execution_decision(
        "call_mcp_tool",
        {"server_id": server, "tool": name, "arguments": {}},
        store=store,
        holder=holder,
        chat_id=chat,
    )


def _execute(store: RunStore, holder: ConfigHolder, name: str, args: dict[str, Any],
             decision: Any = None, chat: str | None = None) -> Any:  # fmt: skip
    return execute_mutation(
        name, args, store=store, holder=holder, runner=cast(Any, None), actor="op",
        decision=decision, chat_id=chat,
    )  # fmt: skip


def test_b29_a_baseline_fetch_allow_runs_read_url_and_a_baseline_deny_fetches_nothing(
    net: Callable[..., Any],
) -> None:
    """B29: the operator template's `net-fetch-github` reaches read_url — in-turn,
    no card, the granted budget; a baseline deny refuses with no card, and even
    a confirmed (stale) card fetches nothing (f1-27)."""
    _paths, store, holder, fetched = net("operator")
    url = "https://github.com/skep/skep"
    allowed = _fetch(store, holder, url)
    assert (allowed.verdict, allowed.decided_by) == ("allow", "template:operator/net-fetch-github")
    ran = _execute(store, holder, "read_url", {"url": url}, allowed)
    assert ran["decided_by"] == "template:operator/net-fetch-github"
    assert fetched[-1]["url"] == url and fetched[-1]["max_bytes"] > 100_000
    denied = _fetch(store, holder, "https://evil.example/x")
    assert (denied.verdict, denied.decided_by) == ("deny", "template:operator/no-evil")
    before = len(fetched)
    with pytest.raises(ValueError, match="denied by policy"):
        _execute(store, holder, "read_url", {"url": "https://evil.example/x"})
    assert len(fetched) == before  # nothing was fetched


def test_b29_a_baseline_mcp_rule_reaches_call_mcp_tool(net: Callable[..., Any]) -> None:
    """B29: the baseline's `kb:*` allow runs a read-only kb tool in-turn; its
    `bank:*` deny refuses with no card, and a confirmed card calls nothing."""
    _paths, store, holder, _fetched = net("builder")
    read = _mcp(store, holder, "kb:get_doc")
    assert (read.verdict, read.decided_by) == ("allow", "template:builder/mcp-kb")
    bank = _mcp(store, holder, "bank:get_balance")
    assert (bank.verdict, bank.decided_by) == ("deny", "template:builder/no-bank")
    with pytest.raises(ValueError, match="denied by policy"):
        _execute(store, holder, "call_mcp_tool", {"server_id": "bank", "tool": "get_balance"})


def test_b29_a_tampered_baseline_makes_every_network_lane_ask_or_refuse(
    net: Callable[..., Any],
) -> None:
    _paths, store, holder, _fetched = net("tampered")
    for decision in (
        _fetch(store, holder, "https://docs.python.org/3/"),
        _mcp(store, holder, "kb:get_doc"),
        _mcp(store, holder, "browser:browser_snapshot"),
    ):
        assert (decision.verdict, decision.decided_by) == (
            "require_approval",
            "baseline/baseline_tampered",
        )
    search = execute_read_tool("search_web", {"query": "q"}, store=store, holder=holder)
    assert search["results"] == [] and search["decided_by"] == "baseline/baseline_tampered"


MUTATING = (
    "calendar:delete_event",
    "kb:write_doc",
    "plain:budget_transfer",
    "plain:get_or_create_user",
    "plain:list_things",
    "ghost:get_thing",
)


def test_b09_a_mutating_tool_named_like_a_read_still_cards(net: Callable[..., Any]) -> None:
    """B09 residue: a template allow (`calendar:*`, `kb:*`) grants only a tool
    the server marks read-only; a destructive, an un-annotated or a read-verb
    tool that mutates cards — on every lane and in every role."""
    _paths, store, holder, _fetched = net("assistant")
    chats = [None] + [store.create_chat(title=r, model=None, role=r).chat_id
                      for r in ("builder", "assistant", "operator")]  # fmt: skip
    for chat in chats:
        for tool in MUTATING:
            decision = _mcp(store, holder, tool, chat)
            assert decision.verdict == "require_approval", (chat, tool, decision)
    assert _mcp(store, holder, "calendar:get_events").decided_by == (
        "template:assistant/mcp-calendar"
    )


def test_b78_search_web_follows_the_chats_role(
    net: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """B78: search_web decides with the chat's role — a role layer that denies
    search refuses it there, while a chat with no role still searches."""
    from skep.supervisor.policy_engine import Layer, LayerRule
    from skep.supervisor.roles import Role

    original = Role.layer

    def layer(self: Role) -> Layer | None:
        base = original(self)
        if self.name != "assistant" or base is None:
            return base
        deny = LayerRule("network", "deny", "no-search", "search", "*")
        return Layer(base.name, base.label, rules=(deny, *base.rules))

    monkeypatch.setattr(Role, "layer", layer)
    _paths, store, holder, _fetched = net("assistant")
    plain = store.create_chat(title="p", model=None).chat_id
    role = store.create_chat(title="a", model=None, role="assistant").chat_id
    ran = execute_read_tool("search_web", {"query": "q"}, store=store, holder=holder, chat_id=plain)
    assert ran["results"] and ran["decided_by"] == "template:assistant/net-search"
    refused = execute_read_tool(
        "search_web", {"query": "q"}, store=store, holder=holder, chat_id=role
    )
    assert refused["results"] == [] and refused["decided_by"] == "role:assistant/no-search"


def test_b56_a_session_fetch_grant_in_a_role_chat_runs_the_url_with_no_second_card(
    net: Callable[..., Any],
) -> None:
    """Decision #1 (B56, network half): keep=session on a builder chat's
    read_url card, and the same URL runs in-turn — the role's catch-all
    `net-fetch-gated *` no longer out-ranks the grant. Another host cards."""
    _paths, store, holder, fetched = net("builder")
    chat = store.create_chat(title="b", model=None, role="builder").chat_id
    url = "https://granted.example/page"

    def decide(target: str = url) -> Any:
        return mutation_execution_decision(
            "read_url", {"url": target}, store=store, holder=holder, chat_id=chat
        )

    first = decide()
    assert (first.verdict, first.decided_by) == (
        "require_approval",
        "template:builder/net-fetch-gated",
    )
    remember_action_for_session(store, tool="read_url", args={"url": url}, actor="op")
    again = decide()
    assert (again.verdict, again.decided_by) == (
        "allow",
        "grant:session/network:fetch:granted.example",
    )
    _execute(store, holder, "read_url", {"url": url}, again, chat)
    # Only the granted host: in a builder chat the role's catch-all ask still
    # binds the baseline's docs.python.org allow — a template allow is no grant.
    assert fetched[-1]["hops"] == "TFFF"
    assert decide("https://other.example/").verdict == "require_approval"


def test_read_only_is_a_fact_only_about_an_mcp_call_an_email_read_or_a_browse_read(
    net: Callable[..., Any],
) -> None:
    """REST and the CLI take ``read_only`` from the caller. It means something
    only for the requests the Queen classifies (an MCP call, an email read, a
    browse read); for anything else it must not turn an unmatched request
    into ``default/read-only`` allow."""
    _paths, store, holder, _fetched = net("legacy")
    for scope, action, value in (
        ("shell", "run", "make deploy"),
        ("network", "fetch", "unknown.example"),
        ("network", "connect", "unknown.example"),
        ("email", "send", "mail:send_message"),
        ("browse", "act", "browser:browser_click"),
        ("filesystem", "read", "/srv/data/notes.txt"),
    ):
        answer = policy_decide(holder, store, scope=scope, action=action, value=value,
                               read_only=True)  # fmt: skip
        assert answer["decided_by"] != "default/read-only", (scope, action, answer)
    for scope, action, value in (
        ("mcp", "call", "plain:get_page"),
        ("email", "read", "mail:list_messages"),
        ("browse", "read", "browser:browser_snapshot"),
    ):
        answer = policy_decide(holder, store, scope=scope, action=action, value=value,
                               read_only=True)  # fmt: skip
        assert answer["decided_by"] == "default/read-only", (scope, action, answer)


@pytest.mark.parametrize("kind", ["legacy", "builder"])
def test_an_operator_overlay_fetch_allow_does_not_let_read_url_run(
    kind: str, net: Callable[..., Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """Operator decision (P3-3): before this round read_url never read the
    overlay, and set_operator_policy called its network rules audit. An
    overlay fetch allow is recorded and `policy why` names it, but read_url
    still cards — on the Queen, REST and the CLI alike, in every role. An
    overlay fetch deny binds: refused, no card, nothing fetched."""
    from skep.supervisor.serve.actions import policy_why, set_operator_policy_rule

    paths, store, holder, fetched = net(kind)
    set_operator_policy_rule(store, scope="network", action="fetch", pattern="written.example",
                             verdict="allow")  # fmt: skip
    set_operator_policy_rule(store, scope="network", action="fetch", pattern="refused.example",
                             verdict="deny")  # fmt: skip
    for role in ROLES:
        chat = store.create_chat(title="c", model=None, role=role).chat_id
        flags = ["--role", role] if role else []
        for host, verdict in (("written.example", "require_approval"),
                              ("overlay.example", "require_approval"),
                              ("refused.example", "deny")):  # fmt: skip
            queen = _fetch(store, holder, f"https://{host}/p?q=1", chat)
            assert queen.verdict == verdict, (role, host, queen)
            rest = policy_decide(holder, store, scope="network", action="fetch", value=host,
                                 role=role)  # fmt: skip
            assert (rest["verdict"], rest["decided_by"]) == (queen.verdict, queen.decided_by)
            cli = _cli(paths["personal"], capsys, "decide", "network", "fetch", host, *flags)
            assert (cli["verdict"], cli["decided_by"]) == (queen.verdict, queen.decided_by)
    before = len(fetched)
    with pytest.raises(ValueError, match="denied by policy"):
        _execute(store, holder, "read_url", {"url": "https://refused.example/"})
    assert len(fetched) == before
    why = policy_why(holder, store, "", "network", "fetch", "written.example")
    assert why["operator"]["verdict"] == "require_approval"
    assert "op:network:fetch:written.example" in why["overlay_fetch_allow"]
    assert "overlay_fetch_allow" not in policy_why(
        holder, store, "", "network", "fetch", "unknown.example"
    )


@pytest.mark.parametrize("kind", ["legacy", "builder", "stale-global"])
def test_b56_a_stored_grant_matches_exactly_what_it_matched_before(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P3-4 owns the matchers: no role in the way, a stored fetch or MCP grant
    decides exactly the probes it decided before P3-3 — no more, no less."""
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.chdir(tmp_path)
    for key, shown in rows(tmp_path, kind):
        _kind, lane, _probe, role, grant = key.split("|")
        if lane not in ("fetch", "mcp") or role != "-" or grant == "none":
            continue
        old = FROZEN[key].split(" ")[1]
        was = old.endswith(("/network:fetch:granted.example", "/mcp:tracker:update_issue"))
        now = shown.split(" ")[1].startswith("grant:")
        assert was == now, (key, FROZEN[key], shown)


def _cli(home: Path, capsys: pytest.CaptureFixture[str], *argv: str) -> dict[str, Any]:
    capsys.readouterr()
    assert main(["--home", str(home), "policy", *argv]) == 0
    result: dict[str, Any] = json.loads(capsys.readouterr().out)
    return result


def _hook(home: Path, payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    with monkeypatch.context() as patch:
        patch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        out = io.StringIO()
        patch.setattr("sys.stdout", out)
        assert main(["--home", str(home), "policy", "hook"]) == 0
    result: dict[str, Any] = json.loads(out.getvalue())["hookSpecificOutput"]
    return result


@pytest.mark.parametrize("kind", HOMES)
def test_the_queen_rest_cli_and_hook_give_one_network_verdict_and_one_label(
    kind: str,
    net: Callable[..., Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """V1 for read_url, search_web, call_mcp_tool and browse: the Queen's
    verdict and ``decided_by`` equal REST `policy/decide`, `skep policy
    decide` and (no role; fetch and search) the hook, for the same request,
    fact and role with no repo — on every home, with a session grant held."""
    from skep.supervisor.mcp_client import (
        MCPTool,
        classify_mcp_risk,
        mcp_scope_decision,
        mcp_tool_scope_action,
        with_server_hints,
    )

    paths, store, holder, _fetched = net(kind)
    _grant(store, "session")
    hook = {"allow": "allow", "require_approval": "ask", "deny": "deny"}
    for role in ROLES:
        flags = ["--role", role] if role else []
        role_record = None if role is None else SHIPPED_ROLES[role]
        chat = store.create_chat(title="c", model=None, role=role).chat_id
        for _probe, url in FETCH_PROBES:
            host = urllib.parse.urlparse(url).hostname
            if not host:
                continue
            queen = fetch_grant_decision(store, holder, host, role=role_record)
            rest = policy_decide(holder, store, scope="network", action="fetch", value=host,
                                 role=role)  # fmt: skip
            want = (rest["verdict"], rest["decided_by"])
            assert (queen.verdict, queen.decided_by) == want, (kind, role, url)
            cli = _cli(paths["personal"], capsys, "decide", "network", "fetch", host, *flags)
            assert (cli["verdict"], cli["decided_by"]) == want, (kind, role, url)
            if role is None:
                answer = _hook(paths["personal"],
                               {"tool_name": "WebFetch", "tool_input": {"url": url}},
                               monkeypatch)  # fmt: skip
                assert answer["permissionDecision"] == hook[queen.verdict], (kind, url)
                assert str(queen.decided_by) in answer["permissionDecisionReason"], (kind, url)
        search = execute_read_tool("search_web", {"query": "q"}, store=store, holder=holder,
                                   chat_id=chat)  # fmt: skip
        rest = policy_decide(holder, store, scope="network", action="search", value="ddgs",
                             role=role)  # fmt: skip
        assert search["decided_by"] == rest["decided_by"], (kind, role)
        assert bool(search["results"]) == (rest["verdict"] == "allow"), (kind, role)
        if role is None:
            answer = _hook(paths["personal"],
                           {"tool_name": "WebSearch", "tool_input": {"query": "q"}},
                           monkeypatch)  # fmt: skip
            assert answer["permissionDecision"] == hook[rest["verdict"]], kind
            assert str(rest["decided_by"]) in answer["permissionDecisionReason"], kind
        for server, name in MCP_PROBES:
            tool = with_server_hints(store, MCPTool(server_id=server, name=name, description=""))
            scope, action = mcp_tool_scope_action(store, tool)
            read_only = (
                action == "read"
                if scope in ("email", "browse")
                else (classify_mcp_risk(tool) == "read")
            )
            queen = mcp_scope_decision(store, holder, tool, role=role_record)
            value = f"{server}:{name}"
            rest = policy_decide(holder, store, scope=scope, action=action, value=value,
                                 role=role, read_only=read_only)  # fmt: skip
            want = (rest["verdict"], rest["decided_by"])
            assert (queen.verdict, queen.decided_by) == want, (kind, role, value)
            ro = ["--read-only"] if read_only else []
            cli = _cli(paths["personal"], capsys, "decide", scope, action, value, *flags, *ro)
            assert (cli["verdict"], cli["decided_by"]) == want, (kind, role, value)


def test_every_network_lane_fails_closed_when_the_policy_will_not_load(
    net: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """I9: a policy document that will not load refuses read_url (and its card
    lane fetches nothing), search_web, call_mcp_tool and browse — none runs."""
    _paths, store, holder, fetched = net("operator")
    calls: list[str] = []

    def runner(config: Any, **_: Any) -> Any:
        inner = fake_runner(config)

        def run(method: str, params: Any) -> Any:
            calls.append(method)
            return inner(method, params)

        return run

    monkeypatch.setattr("skep.supervisor.mcp_client.runner_for_config", runner)
    store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, "{not json")
    url = "https://github.com/skep/skep"
    fetch = _fetch(store, holder, url)
    assert (fetch.verdict, fetch.reason) == ("deny", "network.deny.policy_unloadable")
    with pytest.raises(ValueError, match="did not load"):
        _execute(store, holder, "read_url", {"url": url})
    assert fetched == []
    search = execute_read_tool("search_web", {"query": "q"}, store=store, holder=holder)
    assert search["results"] == [] and "did not load" in search["error"]
    for tool in ("kb:get_doc", "browser:browser_snapshot", "mail:list_messages"):
        decision = _mcp(store, holder, tool)
        assert decision.verdict == "deny" and decision.reason.endswith("policy_unloadable")
    with pytest.raises(ValueError, match="denied by policy"):
        _execute(store, holder, "call_mcp_tool", {"server_id": "kb", "tool": "get_doc"})
    assert "tools/call" not in calls


@pytest.mark.parametrize("read_only", [True, False], ids=["prompt-schedule", "continuation"])
def test_an_unattended_turn_runs_no_network_lane_even_where_policy_allows(
    read_only: bool, net: Callable[..., Any]
) -> None:
    """Unattended turns keep today's refusal: search_web refuses, and a
    read_url / call_mcp_tool the policy would run in-turn becomes a card on a
    run-finish continuation and a refusal on a prompt tick — nothing is
    fetched or called."""
    from skep.supervisor.serve.chat import ChatEngine
    from skep.supervisor.serve.jobs import Dispatcher
    from skep.supervisor.serve.llm import LLM_BASE_URL, LLM_DEFAULT_MODEL

    from .fake_ollama import FakeOllama

    _paths, store, holder, fetched = net("operator")
    ollama = FakeOllama(api_key=None).start()
    try:
        store.set_setting(LLM_BASE_URL, ollama.base_url)
        store.set_setting(LLM_DEFAULT_MODEL, "qwen3")
        chat = store.create_chat(title="t", model=None)
        engine = ChatEngine(store=store, holder=holder, runner=Dispatcher(holder, store),
                            home=holder.current.home)  # fmt: skip
        base_url, api_key, model, protocol = engine.resolved_llm(store.get_chat(chat.chat_id))
        for name, args in (
            ("search_web", {"query": "q"}),
            ("read_url", {"url": "https://github.com/skep/skep"}),
            ("call_mcp_tool", {"server_id": "kb", "tool": "get_doc", "arguments": {}}),
        ):
            ollama.script_tool_call(name, args)
            if name == "search_web" or read_only:  # a card ends the turn instead
                ollama.script_reply("done")
            store.add_chat_message(chat.chat_id, role="user", content=f"use {name}")
            list(engine.turn_events(chat.chat_id, base_url=base_url, api_key=api_key,
                                    model=model, protocol=protocol, read_only=read_only,
                                    unattended=True))  # fmt: skip
        assert fetched == []
        search = [m.content for m in store.chat_messages(chat.chat_id)
                  if m.role == "tool" and m.tool_name == "search_web"]  # fmt: skip
        assert search and "scheduled turn" in search[0], search
        actions = {a.tool: a.status for a in store.chat_actions(chat.chat_id)}
        # A continuation may propose (a human pulls the card); a prompt tick never.
        assert actions == (
            {} if read_only else {"read_url": "proposed", "call_mcp_tool": "proposed"}
        ), actions
    finally:
        ollama.stop()
