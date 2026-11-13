"""ADR 0059 P3-4 (D4): grants get a lifetime, a matcher and a binding.

The frozen table (``grant_grid``) holds every grant kind a home can hold to
its pre-P3-4 answers: nothing is more permissive than before except the two
kinds the ADR decided — B101 (a grant applies on a home with no baseline) and
B18 (a worker "Allow & remember" under a locked baseline now takes effect, as
a project grant). Then one regression per ledger row."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import HTTPException

from skep.cli import main
from skep.supervisor import RunStore
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.config import SupervisorConfig
from skep.supervisor.policy_resolver import resolve_run_policy
from skep.supervisor.policy_schema import (
    OPERATOR_POLICY_SETTINGS_KEY,
    POLICY_DOCUMENT_SETTINGS_KEY,
    LearnedRule,
    PolicyDocument,
    document_from_settings,
    lifetime_of,
    matcher_of,
)
from skep.supervisor.serve import actions
from skep.supervisor.serve.actions import (
    keep_options,
    keep_refusal,
    list_policy_rules,
    persist_remembered_command,
    policy_decide,
    remember_action_for_session,
    remember_commands_for_session,
)
from skep.supervisor.serve.cards import card_summary
from skep.supervisor.serve.chat import ChatEngine
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import (
    fetch_grant_decision,
    mutation_execution_decision,
    queen_shell_decision,
)

from .grant_grid import GRANTED, HOMES, rows
from .queen_lane_grid import build_home

FROZEN: dict[str, str] = json.loads(
    (
        Path(__file__).parents[1] / "fixtures" / "policy_v3" / "pre_p3_4_grant_verdicts.json"
    ).read_text()
)
_RANK = {"allow": 0, "card": 1, "require_approval": 1, "deny": 2, "error": 2}
LOCKED = ("builder", "assistant")


def classify(key: str, old: str, new: str) -> str:
    """``wider:<kind>`` (only B101 and B18), ``narrower:<kind>``, ``label`` or
    ``same``; raises on any other widening."""
    home, face, probe, role, which, grant = key.split("|")
    if old == new:
        return "same"
    if face == "worker":
        old_held, old_rest = old.split(" ", 1)
        new_held, new_rest = new.split(" ", 1)
        if (old_held, new_held) == ("out", "in"):
            if grant == "worker-remember" and home in LOCKED and probe == "repo-a":
                return "wider:B18"
            raise AssertionError(f"worker grant widened: {key}: {old} -> {new}")
        if (old_held, new_held) == ("in", "out"):
            return "narrower:B39" if probe == "repo-b" else "narrower:worker"
        old_verdict, new_verdict = old_rest.split(" ")[0], new_rest.split(" ")[0]
        if _RANK[new_verdict] < _RANK[old_verdict]:
            raise AssertionError(f"worker decide widened: {key}: {old} -> {new}")
        return "label" if _RANK[new_verdict] == _RANK[old_verdict] else "narrower:worker"
    old_verdict = old.split(" ", 1)[0]
    new_verdict, new_by = new.split(" ", 1)
    if _RANK[new_verdict] < _RANK[old_verdict]:
        if home == "zero" and new_by.startswith(("grant:session/", "grant:global/")):
            return "wider:B101"
        # The keep now covers exactly the subject the human approved, where
        # before it stored something that never matched (items 6 and 4):
        if grant == "bg-always" and probe == "bg-granted":
            return "wider:B21-subject"  # start_process keep → run_background
        if grant == "compound-always" and probe in ("compound", "granted"):
            return "wider:B61-subject"  # one grant per segment of the kept line
        raise AssertionError(f"more permissive: {key}: {old} -> {new}")
    if _RANK[new_verdict] > _RANK[old_verdict]:
        if grant.endswith("-session") and (role, which) != ("-", "a"):
            return "narrower:B39-chat"  # the session keep was made in the plain chat A
        if face == "fetch" and grant in ("fetch-session", "fetch-always"):
            return "narrower:B74"
        if grant == "bg-always":
            return "narrower:B21"  # a start_process keep is run_background, never run
        if probe == "granted-amp":
            return "narrower:B21-amp"
        raise AssertionError(f"unexplained narrowing: {key}: {old} -> {new}")
    return "label"


LOCKED_FLIPS = {
    "label": 21, "narrower:B21-amp": 13, "narrower:B39-chat": 40, "narrower:B21": 12,
    "wider:B21-subject": 6, "wider:B61-subject": 12, "narrower:B74": 28, "narrower:B39": 1,
    "wider:B18": 1,
}  # fmt: skip
# Every flip against the frozen table, per home (the round's report lists them).
EXPECTED_FLIPS: dict[str, dict[str, int]] = {
    "legacy": {**{k: v for k, v in LOCKED_FLIPS.items() if k != "wider:B18"}, "label": 15},
    "zero": {"wider:B101": 81, "narrower:B39": 1},
    "builder": LOCKED_FLIPS,
    "assistant": LOCKED_FLIPS,
    "tampered": {},
}


@pytest.mark.parametrize("kind", HOMES)
def test_no_grant_verdict_is_wider_than_before_but_b101_and_b18(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Homes x roles x chats x grant kinds x probes, held to the pre-P3-4
    answers. Wider only by decision: B101, B18, and the kept subject itself
    where the keep used to store a rule that never matched (B21, B61)."""
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.chdir(tmp_path)
    seen: Counter[str] = Counter()
    for key, shown in rows(tmp_path / kind, kind):
        seen[classify(key, FROZEN[key], shown)] += 1
    del seen["same"]
    assert dict(seen) == EXPECTED_FLIPS[kind]


# -- one regression per ledger row ------------------------------------------


@dataclass
class Home:
    paths: dict[str, Path]
    config: SupervisorConfig
    store: RunStore
    holder: ConfigHolder
    chat_a: str
    chat_b: str

    def decide(self, tool: str, chat: str | None = None, **args: Any) -> str:
        decision = mutation_execution_decision(
            tool, args, store=self.store, holder=self.holder, chat_id=chat or self.chat_a
        )
        return "card" if decision is None else f"{decision.verdict} {decision.decided_by}"

    def keep(self, tool: str, tier: str = "always", chat: str | None = None, **args: Any) -> Any:
        return remember_action_for_session(
            self.store, tool=tool, args=args, actor="op", tier=tier, chat_id=chat or self.chat_a
        )

    def learned(self) -> list[LearnedRule]:
        raw = self.store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY)
        return list((document_from_settings(raw) or PolicyDocument()).learned)

    def allowlist(self, repo: Path) -> list[list[str]]:
        resolved = resolve_run_policy(
            store=self.store, config=self.config, repo=repo, caste="coding", network=None,
            env_allowlist=None, wall_clock_seconds=None, max_iterations=None,
            max_actions=None, max_provider_calls=None, execution_mode="sandbox",
        )  # fmt: skip
        return resolved.permissions.shell_allowlist


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], Home]]:
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    opened: list[RunStore] = []

    def build(kind: str) -> Home:
        paths = build_home(tmp_path / kind, kind)
        monkeypatch.chdir(paths["outside"])
        config = build_config(paths["personal"], None)
        store = RunStore(config.db_path)
        opened.append(store)
        chat_a = store.create_chat(title="a", model=None).chat_id
        chat_b = store.create_chat(title="b", model=None).chat_id
        return Home(paths, config, store, ConfigHolder(config, store), chat_a, chat_b)

    yield build
    for store in opened:
        store.close()


URL_PAGE = "https://granted.example/docs/page"


def test_b74_a_read_url_keep_covers_its_directory_never_the_host(home: Any) -> None:
    h = home("legacy")
    grant = h.keep("read_url", url=URL_PAGE)
    assert grant["matcher"] == "url_prefix" and grant["pattern"] == "https://granted.example/docs/"
    assert grant["lifetime"] == "global" and grant["status"] == "added"
    allowed = "allow grant:global/network:fetch:https://granted.example/docs/"
    assert h.decide("read_url", url=URL_PAGE) == allowed
    assert h.decide("read_url", url="https://granted.example/docs/other") == allowed
    for near_miss in (
        "https://granted.example/collect?data=secret",  # the exfiltration shape
        URL_PAGE + "?data=secret",  # a query the card did not have
        "https://granted.example/docs/../collect",  # dot segments
        "https://granted.example/docs%2F..%2Fcollect",
        "http://granted.example/docs/page",  # another scheme
        "https://granted.example:8443/docs/page",  # another port
        "https://user:pw@granted.example/docs/page",
        "https://docs.granted.example/docs/page",
    ):
        assert h.decide("read_url", url=near_miss).startswith("require_approval"), near_miss
    # A redirect hop is decided by host only: a url_prefix grant never covers it.
    assert fetch_grant_decision(h.store, h.holder, "granted.example").verdict != "allow"


def test_b74_a_card_with_a_query_grants_that_query_only(home: Any) -> None:
    h = home("legacy")
    h.keep("read_url", url="https://granted.example/api/items?page=1")
    assert h.decide("read_url", url="https://granted.example/api/other?page=1").startswith("allow")
    assert h.decide("read_url", url="https://granted.example/api/other").startswith("allow")
    assert h.decide("read_url", url="https://granted.example/api/items?page=2").startswith(
        "require"
    )


def test_b74_a_legacy_host_grant_keeps_its_whole_host_and_doctor_lists_it(home: Any) -> None:
    h = home("legacy")
    h.store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, json.dumps({"learned": [
        {"rule_id": "network:fetch:granted.example", "action": "fetch",
         "pattern": "granted.example", "scope": "network", "provenance": "allow-always:op"},
    ]}))  # fmt: skip
    by = "allow grant:global/network:fetch:granted.example"
    assert h.decide("read_url", url="https://granted.example/collect?data=x") == by
    rules = list_policy_rules(h.store)["rules"]
    assert [(r["matcher"], r["lifetime"], r["bound"]) for r in rules] == [
        ("legacy-host", "global", "everywhere")
    ]
    from skep.status import _legacy_host_advisories

    lines = _legacy_host_advisories(h.paths["personal"])
    assert any("legacy-host" in line and "granted.example" in line for line in lines), lines


def test_b39_a_queen_session_keep_holds_in_its_own_chat_only(home: Any) -> None:
    h = home("legacy")
    grant = h.keep("run_shell", tier="session", command=GRANTED)
    assert (grant["lifetime"], grant["bound"]) == ("session", "this chat")
    assert h.decide("run_shell", command=GRANTED).startswith("allow grant:session/")
    assert h.decide("run_shell", chat=h.chat_b, command=GRANTED) == (
        "require_approval default/unmatched"
    )
    rest = policy_decide(h.holder, h.store, scope="shell", action="run", value=GRANTED)
    assert rest["verdict"] == "require_approval"  # no chat: no chat-bound grant
    with pytest.raises(ValueError, match="bound to its chat"):
        remember_action_for_session(
            h.store, tool="run_shell", args={"command": GRANTED}, actor="op", tier="session"
        )


def test_b39_a_worker_approve_holds_in_its_own_repo_only(home: Any) -> None:
    h = home("legacy")
    other = h.paths["repo"].parent / "repo-b"
    other.mkdir()
    added = remember_commands_for_session(h.store, [["make", "check"]], repo=h.paths["repo"])
    assert added == [["make", "check"]]
    assert ["make", "check"] in h.allowlist(h.paths["repo"])
    assert ["make", "check"] not in h.allowlist(other)
    # The old global session tier is no longer read (B39), nor written.
    h.store.set_setting("session_allowed_shell_commands", [["make", "lint"]])
    assert ["make", "lint"] not in h.allowlist(other)
    # Never a role's catch-all ask: the worker grant stays off the Queen lane.
    assert h.decide("run_shell", command="make check").startswith("require_approval")


def test_b61_a_compound_keep_stores_one_grant_per_segment(home: Any) -> None:
    h = home("legacy")
    grant = h.keep("run_shell", command="ls -la && " + GRANTED)
    assert grant["patterns"] == ["ls -la", GRANTED]
    assert h.decide("run_shell", command="ls -la && " + GRANTED).startswith("allow grant:")
    assert h.decide("run_shell", command=GRANTED + " && ls -la").startswith("allow grant:")
    assert h.decide("run_shell", command="ls -la && rm -rf x").startswith("require_approval")
    before = h.learned()
    for line in ("ls && git push origin main", "ls && $(curl x)", "echo hi && sudo ls"):
        assert keep_refusal("run_shell", {"command": line}, "always") is not None, line
        with pytest.raises(ValueError):
            h.keep("run_shell", command=line)
    assert h.learned() == before


def test_b59_a_second_keep_upgrades_in_place_and_says_so(home: Any) -> None:
    h = home("legacy")
    assert h.keep("read_url", tier="session", url=URL_PAGE)["status"] == "added"
    upgraded = h.keep("read_url", tier="always", url=URL_PAGE)
    assert (upgraded["status"], upgraded["lifetime"], upgraded["bound"]) == (
        "upgraded", "global", "everywhere",
    )  # fmt: skip
    [rule] = h.learned()
    assert (rule.lifetime, rule.bound, rule.provenance) == ("global", {}, "allow-always:op")
    assert h.decide("read_url", chat=h.chat_b, url=URL_PAGE).startswith("allow grant:global/")
    again = h.keep("read_url", tier="session", url=URL_PAGE)
    assert (again["status"], again["lifetime"]) == ("kept", "global")
    assert len(h.learned()) == 1


def test_b21_start_process_keeps_run_background_and_a_run_grant_never_covers_amp(
    home: Any,
) -> None:
    h = home("legacy")
    grant = h.keep("start_process", command="npm run dev")
    assert grant["action"] == "run_background"
    assert h.decide("start_process", command="npm run dev").startswith("allow grant:global/")
    assert h.decide("run_shell", command="npm run dev").startswith("require_approval")
    h.keep("run_shell", command=GRANTED)
    assert h.decide("run_shell", command=GRANTED).startswith("allow")
    assert h.decide("run_shell", command=GRANTED + " &").startswith("require_approval")
    assert h.decide("run_shell", command=f"{GRANTED} & sleep 1").startswith("require_approval")


def test_b18_allow_and_remember_under_a_locked_baseline_takes_effect(home: Any) -> None:
    h = home("builder")
    repo = h.paths["repo"]
    assert ["make", "check"] not in h.allowlist(repo)
    where = persist_remembered_command(h.store, h.holder, repo, ["make", "check"])
    assert where == "project:app"
    assert "allowed_shell_commands" not in (h.store.get_project_policy("app").policy or {})
    assert ["make", "check"] in h.allowlist(repo)
    [rule] = h.learned()
    assert (rule.lifetime, rule.matcher, rule.bound) == ("project", "prefix", {"project_id": "app"})
    unbound = repo.parent / "loose"
    unbound.mkdir()
    assert persist_remembered_command(h.store, h.holder, unbound, ["make"]) == (
        f"repo:{unbound.resolve()}"
    )


def _egress(h: Any, repo: Path) -> list[str]:
    resolved = resolve_run_policy(
        store=h.store, config=h.config, repo=repo, caste="coding", network=None,
        env_allowlist=None, wall_clock_seconds=None, max_iterations=None,
        max_actions=None, max_provider_calls=None, execution_mode="sandbox",
    )  # fmt: skip
    return list(resolved.permissions.network)


def _setup_document_denies(h: Any, host: str) -> None:
    from skep.supervisor.policy_schema import (
        POLICY_DOCUMENT_SETTINGS_KEY,
        PolicyDocument,
        PolicyRule,
        ScopePolicy,
        document_from_settings,
    )

    document = (
        document_from_settings(h.store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY))
        or PolicyDocument()
    )
    deny = PolicyRule(rule_id=f"no-{host}", action="connect", pattern=host)
    others = [scope for scope in document.scopes if scope.scope != "network"]
    updated = document.model_copy(
        update={"scopes": [*others, ScopePolicy(scope="network", deny=[deny])]}
    )
    h.store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, updated.model_dump_json())


def test_b18_allow_host_and_remember_under_a_locked_baseline_takes_effect(home: Any) -> None:
    """W28 (B18's network half): the allow-host remember wrote the project's
    default_network, which the locked baseline's enforced knobs ignored while
    the receipt said remembered. Now a project grant, bound like the shell
    remember, that the run compile reads."""
    h = home("builder")
    repo = h.paths["repo"]
    assert "docs.example.com" not in _egress(h, repo)
    assert (
        actions.persist_remembered_network_host(h.store, h.holder, repo, "docs.example.com")
        == "project:app"
    )
    assert "default_network" not in (h.store.get_project_policy("app").policy or {})
    assert "docs.example.com" in _egress(h, repo)
    [rule] = [rule for rule in h.learned() if rule.scope == "network"]
    assert (rule.action, rule.pattern, rule.lifetime, rule.matcher, rule.bound) == (
        "connect", "docs.example.com", "project", "host", {"project_id": "app"},
    )  # fmt: skip
    unbound = repo.parent / "loose-net"
    unbound.mkdir()
    assert (
        actions.persist_remembered_network_host(h.store, h.holder, unbound, "docs.example.com")
        == f"repo:{unbound.resolve()}"
    )
    assert "docs.example.com" in _egress(h, unbound)


def test_a_remembered_host_a_later_deny_covers_is_dropped_at_compile(home: Any) -> None:
    h = home("builder")
    repo = h.paths["repo"]
    actions.persist_remembered_network_host(h.store, h.holder, repo, "docs.example.com")
    _setup_document_denies(h, "docs.example.com")
    assert "docs.example.com" not in _egress(h, repo)


def test_a_remember_inside_a_deny_is_refused_at_write_time(home: Any) -> None:
    from skep.supervisor.policy_write import PolicyWriteRejected

    h = home("builder")
    _setup_document_denies(h, "docs.example.com")
    with pytest.raises(PolicyWriteRejected):
        actions.persist_remembered_network_host(
            h.store, h.holder, h.paths["repo"], "docs.example.com"
        )
    assert not [rule for rule in h.learned() if rule.scope == "network"]


def test_b101_grants_apply_on_a_home_with_no_baseline_never_a_tampered_one(home: Any) -> None:
    zero = home("zero")
    zero.keep("run_shell", command=GRANTED)
    assert zero.decide("run_shell", command=GRANTED).startswith("allow grant:global/")
    assert zero.decide("run_shell", command="df -h") == "require_approval baseline/no_baseline"
    tampered = home("tampered")
    tampered.keep("run_shell", command=GRANTED)
    assert tampered.decide("run_shell", command=GRANTED) == (
        "require_approval baseline/baseline_tampered"
    )


def test_b32_a_keep_the_card_cannot_honour_is_refused_before_it_runs(home: Any) -> None:
    h = home("legacy")
    engine = ChatEngine(store=h.store, holder=h.holder, runner=cast(Any, None), home=h.config.home)
    action_id = h.store.add_chat_action(h.chat_a, tool="set_personality", args={"value": "x"})
    chat = h.store.get_chat(h.chat_a)
    with pytest.raises(HTTPException) as refused:
        engine.verdict_events(h.chat_a, chat, action_id, confirm=True, keep="always")
    assert refused.value.status_code == 400
    assert "set_personality has no standing grant" in str(refused.value.detail)
    assert h.store.get_chat_action(action_id).status == "proposed"  # nothing ran
    assert card_summary("set_personality", {"value": "x"}).get("keep") is None
    assert card_summary("run_shell", {"command": "git push"})["keep"] == ["once"]
    assert card_summary("run_shell", {"command": "ls"})["keep"] == ["once", "session", "always"]
    assert keep_options("read_url", {"url": "ftp://x.example/"}) == ["once"]


def test_legacy_rows_load_unchanged_and_match_what_they_matched(home: Any) -> None:
    h = home("legacy")
    old = [
        {"rule_id": f"shell:run:{GRANTED}", "action": "run", "pattern": GRANTED,
         "scope": "shell", "provenance": "allow-always:op", "created_at": None},
        {"rule_id": "network:fetch:granted.example", "action": "fetch",
         "pattern": "granted.example", "scope": "network", "provenance": "session:op"},
        {"rule_id": "mcp:tracker:update_issue", "action": "call",
         "pattern": "tracker:update_issue", "scope": "mcp", "provenance": "allow-always:op"},
    ]  # fmt: skip
    h.store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, json.dumps({"learned": old[:2]}))
    h.store.set_setting(OPERATOR_POLICY_SETTINGS_KEY, json.dumps({"learned": old[2:]}))
    rules = h.learned()
    assert [(lifetime_of(r), matcher_of(r), r.bound) for r in rules] == [
        ("global", "exact_argv", {}),
        ("session", "host", {}),
    ]
    assert h.decide("run_shell", chat=h.chat_b, command=GRANTED).startswith("allow grant:global/")
    assert h.decide("run_shell", command=GRANTED + " -x").startswith("require_approval")
    assert h.decide("read_url", chat=h.chat_b, url="https://granted.example/a?b").startswith(
        "allow grant:session/"
    )  # an unbound pre-P3-4 session row: what it matched until the restart drops it
    assert actions.clear_session_policy_rules(h.store) == 1


def test_queen_rest_and_cli_agree_on_a_grant_and_its_near_miss(
    home: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    h = home("builder")
    h.keep("run_shell", command=GRANTED)
    for command in (GRANTED, GRANTED + " --upload"):
        queen = queen_shell_decision(h.store, h.holder, command=command, cwd=None)
        assert queen is not None
        rest = policy_decide(h.holder, h.store, scope="shell", action="run", value=command)
        capsys.readouterr()
        assert main(["--home", str(h.paths["personal"]), "policy", "decide", "shell", "run",
                     command]) == 0  # fmt: skip
        cli = json.loads(capsys.readouterr().out)
        assert (queen.verdict, queen.decided_by) == (rest["verdict"], rest["decided_by"])
        assert (cli["verdict"], cli["decided_by"]) == (rest["verdict"], rest["decided_by"])


def test_an_exact_grant_never_covers_a_line_sh_reads_as_other_commands(home: Any) -> None:
    """``/bin/sh -c`` reads an unquoted newline as ``;`` and ``#`` as a comment
    only at the start of a word. The segmenter read a newline as a space and
    ``--fast#x`` as ``--fast``, so a kept ``echo git push origin main`` ran
    ``echo`` then ``git push`` with no card."""
    from skep.supervisor.policy_schema import backgrounds, shell_segments
    from skep.supervisor.shell_prefixes import queen_command_line_refusal

    h = home("builder")
    h.keep("run_shell", command="echo git push origin main")
    h.keep("run_shell", command=GRANTED)
    for line in ("echo\ngit push origin main", "echo git\npush origin main", GRANTED + "#x"):
        assert not h.decide("run_shell", command=line).startswith("allow"), line
    assert h.decide("run_shell", command=GRANTED + " # a note").startswith("allow grant:")
    assert h.decide("run_shell", command="echo 'git\npush'").startswith("require_approval")
    assert shell_segments("ls\nmake # x; rm\n'a\nb'") == ["ls", "make", "'a\nb'"]
    assert shell_segments("ls \\\n-la") == ["ls -la"]
    assert backgrounds("npm run dev &\nls")
    assert queen_command_line_refusal("ls\ngit push origin main") is not None


def test_the_card_says_what_a_keep_would_cover() -> None:
    """A compound keep grants each command on its own and a read_url keep a
    whole URL directory (the whole host for a top-level page): the card says
    so, never just "always"."""
    compound = card_summary("run_shell", {"command": "ls -la && " + GRANTED})
    assert compound["keep_covers"] == (
        f"a keep allows each of these on its own, exactly: `ls -la`; `{GRANTED}`"
    )
    page = card_summary("read_url", {"url": "https://granted.example/page?x=1"})
    assert page["keep_covers"] == (
        "a keep allows any page under https://granted.example/ with no query or ?x=1"
    )
    assert card_summary("start_process", {"command": "npm run dev"})["keep_covers"] == (
        "a keep allows exactly `npm run dev`, as a background process"
    )
    assert "keep_covers" not in card_summary("run_shell", {"command": "git push"})
    assert "keep_covers" not in card_summary("set_personality", {"value": "x"})


def test_an_allow_fetch_domain_grant_is_a_host_grant_not_a_legacy_one(home: Any) -> None:
    """allow_fetch_domain grants a whole host on purpose (the ``host``
    matcher); doctor's legacy-host list is for rows written before P3-4."""
    from skep.status import _legacy_host_advisories
    from skep.supervisor.serve.tools import execute_mutation

    h = home("legacy")
    execute_mutation(
        "allow_fetch_domain", {"domain": "granted.example"}, store=h.store, holder=h.holder,
        runner=cast(Any, None), actor="op", chat_id=h.chat_a,
    )  # fmt: skip
    [rule] = list_policy_rules(h.store)["rules"]
    assert (rule["matcher"], rule["lifetime"], rule["bound"]) == ("host", "global", "everywhere")
    assert h.decide("read_url", url="https://granted.example/any?q=1").startswith("allow grant:")
    assert _legacy_host_advisories(h.paths["personal"]) == []


def test_b32_a_keep_a_deny_would_reject_is_refused_before_the_command_runs(
    home: Any, tmp_path: Path
) -> None:
    """store_grant refuses a grant that reaches into a deny. That check ran
    AFTER the confirmed command, so the command ran and the card said
    "failed: bad arguments". Now the confirm is a 400 and nothing runs."""
    from skep.supervisor.policy_schema import PolicyRule, ScopePolicy

    h = home("legacy")
    marker = tmp_path / "ran.txt"
    document = document_from_settings(h.store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY))
    document = document or PolicyDocument()
    shell = ScopePolicy(scope="shell", deny=[
        PolicyRule(rule_id="no-touch-x", action="run", pattern=f"touch {marker} --x"),
    ])  # fmt: skip
    others = [s for s in document.scopes if s.scope != "shell"]
    h.store.set_setting(
        POLICY_DOCUMENT_SETTINGS_KEY,
        document.model_copy(update={"scopes": [*others, shell]}).model_dump_json(),
    )
    args = {"command": f"touch {marker}", "cwd": str(tmp_path)}
    engine = ChatEngine(store=h.store, holder=h.holder, runner=cast(Any, None), home=h.config.home)
    action_id = h.store.add_chat_action(h.chat_a, tool="run_shell", args=args)
    with pytest.raises(HTTPException) as refused:
        engine.verdict_events(
            h.chat_a, h.store.get_chat(h.chat_a), action_id, confirm=True, keep="always"
        )
    assert refused.value.status_code == 400
    assert "no-touch-x" in str(refused.value.detail)
    assert not marker.exists()
    assert h.store.get_chat_action(action_id).status == "proposed"


def test_a_confirm_that_names_no_keep_runs_once_when_the_default_keep_would_be_refused(
    home: Any, tmp_path: Path
) -> None:
    """The write-time check narrows such a card to "once", and the web UI then
    sends no keep at all. The confirm's default was "session" judged by taint
    alone, so a plain Approve on that card was a 400. A keep nobody named
    falls back to once; a keep the client did name is still refused."""
    from skep.supervisor.policy_schema import PolicyRule, ScopePolicy

    h = home("legacy")
    marker = tmp_path / "ran.txt"
    document = document_from_settings(h.store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY))
    document = document or PolicyDocument()
    shell = ScopePolicy(scope="shell", deny=[
        PolicyRule(rule_id="no-touch-x", action="run", pattern=f"touch {marker} --x"),
    ])  # fmt: skip
    others = [s for s in document.scopes if s.scope != "shell"]
    h.store.set_setting(
        POLICY_DOCUMENT_SETTINGS_KEY,
        document.model_copy(update={"scopes": [*others, shell]}).model_dump_json(),
    )
    args = {"command": f"touch {marker}", "cwd": str(tmp_path)}
    engine = ChatEngine(store=h.store, holder=h.holder, runner=cast(Any, None), home=h.config.home)
    action_id = h.store.add_chat_action(h.chat_a, tool="run_shell", args=args)
    with pytest.raises(HTTPException) as named:
        engine.verdict_events(
            h.chat_a, h.store.get_chat(h.chat_a), action_id, confirm=True, keep="session"
        )
    assert "no-touch-x" in str(named.value.detail)
    with pytest.raises(HTTPException) as unnamed:
        engine.verdict_events(h.chat_a, h.store.get_chat(h.chat_a), action_id, confirm=True)
    # Past the keep check: what stops this bare test home is that it has no model.
    assert unnamed.value.status_code == 409
    assert "keep" not in str(unnamed.value.detail)


def test_the_cli_lists_and_revokes_a_grant(home: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """W29: `skep policy grants` / `skep policy revoke <rule_id>`, through the
    same verbs REST uses (ADR 0050)."""
    h = home("builder")
    repo = h.paths["repo"]
    persist_remembered_command(h.store, h.holder, repo, ["make", "check"])
    [rule] = h.learned()
    personal = str(h.paths["personal"])
    capsys.readouterr()

    assert main(["--home", personal, "policy", "grants"]) == 0
    listed = capsys.readouterr().out
    assert rule.rule_id in listed
    assert "shell.run" in listed and "make check" in listed
    assert "prefix" in listed and "project" in listed and "project app" in listed

    assert main(["--home", personal, "policy", "revoke", "no-such-rule"]) == 2
    assert "no learned rule 'no-such-rule'" in capsys.readouterr().err
    assert main(["--home", personal, "policy", "revoke", rule.rule_id]) == 0
    assert f"revoked: {rule.rule_id}" in capsys.readouterr().out
    assert h.learned() == []
    assert main(["--home", personal, "policy", "grants"]) == 0
    assert "no learned grants" in capsys.readouterr().out
