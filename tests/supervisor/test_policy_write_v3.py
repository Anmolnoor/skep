"""ADR 0059 P3-5 (D6, V11): look before every policy write.

Every policy writer runs the one write-time check (``policy_write.vet_write``)
on the would-be stack before it stores anything: a NEW §5.4 forbidden
combination is refused naming the rule and its layer, and the store stays
byte-identical; a deny is always accepted; the write's answer (and the card
that proposes it) carries an advisory replay of recent decisions."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from skep.cli import main
from skep.supervisor import RunStore, policy_write
from skep.supervisor import policy_baseline as pb
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.config import SupervisorConfig
from skep.supervisor.policy_schema import (
    POLICY_DOCUMENT_SETTINGS_KEY,
    SCOPES,
    LearnedRule,
    PolicyDocument,
    document_from_settings,
)
from skep.supervisor.policy_templates import TEMPLATE_NAMES, load_policy_template
from skep.supervisor.policy_v3 import load_stack
from skep.supervisor.policy_write import Allow, PolicyWriteRejected, vet_write
from skep.supervisor.roles import SHIPPED_ROLES
from skep.supervisor.serve import actions
from skep.supervisor.serve.registry import setup_project_record
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import policy_write_preview

from .conftest import predating_home
from .fake_ollama import FakeOllama
from .test_serve_chat import sse_events
from .test_serve_chat_tools import chat_client

CHAT = "chat-1"


@dataclass
class Home:
    root: Path
    config: SupervisorConfig
    store: RunStore
    holder: ConfigHolder

    def lock(self, template: str = "builder", *extra: dict[str, str]) -> None:
        layer = load_policy_template(template).model_dump(mode="json")
        layer["rules"] = [*layer["rules"], *extra]
        pb.write_lock(self.config.home, pb._load_layer(layer), approved_by="op")

    def snapshot(self) -> tuple[Any, ...]:
        files = sorted(
            (str(p), p.read_bytes())
            for p in self.config.home.glob("policy/*")
            if p.is_file()
        )  # fmt: skip
        return self.store.all_settings(), files

    def learned(self) -> list[LearnedRule]:
        doc = document_from_settings(self.store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY))
        return [] if doc is None else list(doc.learned)

    def log(self, *rows: dict[str, Any] | str) -> None:
        path = self.config.audit_dir / "policy" / "decisions.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            for row in rows:
                handle.write((row if isinstance(row, str) else json.dumps(row)) + "\n")


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Home]:
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    root = tmp_path / "skep"
    config = build_config(root, None)
    config.home.mkdir(parents=True)
    store = RunStore(predating_home(config.db_path))
    store.add_project_policy(
        project_id="p", name="p", strategy="trusted_local_dev", phase="build", policy={}
    )
    yield Home(root, config, store, ConfigHolder(config, store))
    store.close()


NO_CURL = {"scope": "shell", "verdict": "deny", "action": "run", "pattern": "curl",
           "rule_id": "no-curl"}  # fmt: skip
NO_EXAMPLE = {"scope": "network", "verdict": "deny", "action": "fetch",
              "pattern": "example.com", "rule_id": "no-example"}  # fmt: skip


# -- the grant and overlay writers, each with a subject a deny can collide with --

GrantWriter = Callable[[Home, str], Any]


def _keep(h: Home, command: str) -> Any:
    return actions.remember_action_for_session(
        h.store, tool="run_shell", args={"command": command}, actor="op", tier="always"
    )


def _session_keep(h: Home, command: str) -> Any:
    return actions.remember_action_for_session(
        h.store, tool="run_shell", args={"command": command}, actor="op", tier="session",
        chat_id=CHAT,
    )  # fmt: skip


def _worker_remember(h: Home, command: str) -> Any:
    return actions.persist_remembered_command(h.store, h.holder, h.root / "repo", command.split())


def _fetch_domain(h: Home, host: str) -> Any:
    return actions.learn_policy_rule(
        h.store, rule_id=f"network:fetch:{host}", action="fetch", pattern=host, scope="network",
        provenance="allow-always:op",
    )  # fmt: skip


def _overlay_fetch(h: Home, host: str) -> Any:
    return actions.set_operator_policy_rule(
        h.store, scope="network", action="fetch", pattern=host, verdict="allow", holder=h.holder
    )


# writer, a harmless subject, a subject inside the baseline deny, the deny's name
GRANT_WRITERS: dict[str, tuple[GrantWriter, str, str, str]] = {
    "keep=always": (_keep, "ls -la", "curl -sI https://example.com", "no-curl"),
    "keep=session": (_session_keep, "ls -la", "curl -sI https://example.com", "no-curl"),
    "worker remember": (_worker_remember, "make test", "curl -sI", "no-curl"),
    "allow_fetch_domain": (_fetch_domain, "docs.example.org", "example.com", "no-example"),
    "set_operator_policy": (_overlay_fetch, "docs.example.org", "example.com", "no-example"),
}


@pytest.mark.parametrize("writer", GRANT_WRITERS)
def test_b23_a_write_inside_a_baseline_deny_is_refused_naming_it(home: Home, writer: str) -> None:
    write, harmless, inside, deny = GRANT_WRITERS[writer]
    home.lock("builder", NO_CURL, NO_EXAMPLE)
    before = home.snapshot()
    with pytest.raises((ValueError, HTTPException)) as refused:
        write(home, inside)
    text = str(getattr(refused.value, "detail", refused.value))
    assert f"template:builder/{deny}" in text and "§5.4 check 6" in text
    assert "nothing was stored" in text
    assert home.snapshot() == before  # byte-identical
    write(home, harmless)  # an ordinary grant still lands
    assert home.snapshot() != before


@pytest.mark.parametrize("writer", ["keep=always", "worker remember"])
def test_b23_a_setup_document_deny_is_named_too(home: Home, writer: str) -> None:
    write, _harmless, inside, _deny = GRANT_WRITERS[writer]
    document = PolicyDocument.model_validate(
        {"scopes": [{"scope": "shell", "deny": [
            {"rule_id": "doc-no-curl", "action": "run", "pattern": "curl"}]}]}
    )  # fmt: skip
    home.store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, document.model_dump_json())
    before = home.snapshot()
    with pytest.raises(ValueError, match="global/doc-no-curl"):
        write(home, inside)
    assert home.snapshot() == before


def test_b23_the_floor_and_a_role_deny_are_named(home: Home) -> None:
    with pytest.raises(PolicyWriteRejected, match="floor/"):
        actions.learn_policy_rule(
            home.store, rule_id="sudo", action="run", pattern="sudo rm -rf /", scope="shell",
            provenance="allow-always:op",
        )  # fmt: skip
    with pytest.raises(PolicyWriteRejected, match="role:assistant/coding-denied"):
        vet_write(
            home.store, new=[Allow("coding", "edit", "src/*", "g")], role=SHIPPED_ROLES["assistant"]
        )
    vet_write(
        home.store, new=[Allow("coding", "edit", "src/*", "g")], role=SHIPPED_ROLES["builder"]
    )


# -- §5.4 checks 1 and 2, on the writers that can create them --


def test_check_1_a_secret_floor_read_allow_is_refused(home: Home) -> None:
    secret = str(Path.home() / ".ssh" / "*")
    before = home.snapshot()
    with pytest.raises(HTTPException) as refused:
        actions.set_operator_policy_rule(
            home.store, scope="filesystem", action="read", pattern=secret, verdict="allow"
        )
    assert "§5.4 check 1" in str(refused.value.detail)
    assert f"op:filesystem:read:{secret}" in str(refused.value.detail)
    assert home.snapshot() == before
    inside = f"{home.config.home}/notes/*"  # inside an operator root (the skep home)
    actions.set_operator_policy_rule(
        home.store, scope="filesystem", action="read", pattern=inside, verdict="allow"
    )


def test_check_2_an_outside_read_beside_egress_to_any_host_is_a_warning(
    home: Home, tmp_path: Path
) -> None:
    """W16 (the operator's choice): the default operator document allows
    ``network search *`` (search_web), so an outside-the-workspace read allow
    is the f1-07 pair. It lands with a warning saying what happens instead:
    reads there make the chat ask before it searches or fetches (the trifecta
    guard, at decide time). No advice to deny search or /workon the folder."""
    outside = f"{tmp_path}/data/*"
    landed = actions.set_operator_policy_rule(
        home.store, scope="filesystem", action="read", pattern=outside, verdict="allow"
    )
    (warning,) = landed["warnings"]
    assert "global/net:search" in warning and outside in warning
    assert "ask before it searches or fetches" in warning
    assert "deny" not in warning and "/workon" not in warning
    assert outside in str(home.store.get_setting("operator_policy_document"))


def test_check_2_on_a_baseline_write(home: Home, tmp_path: Path) -> None:
    """A custom layer that adds an outside read to the builder template (which
    allows search) locks, and its answer carries the warning (W16)."""
    layer = load_policy_template("builder").model_dump(mode="json")
    layer["template"] = "custom"
    layer["rules"].append({"scope": "filesystem", "verdict": "allow", "action": "read",
                           "pattern": f"{tmp_path}/data/*", "rule_id": "data-read"})  # fmt: skip
    path = tmp_path / "layer.json"
    path.write_text(json.dumps(layer))
    approved = actions.approve_policy_baseline(
        home.config.home, file=str(path), approved_by="op", store=home.store
    )
    assert any("template:custom/data-read" in w for w in approved["warnings"])
    assert pb.lock_path(home.config.home).exists()


def test_check_3_a_rule_that_lets_the_host_shell_install_is_refused(home: Home) -> None:
    """No writer offers a wildcard host-shell grant; a learned prefix rule
    unbound (what an older writer could store) is the shape check 3 catches."""
    rule = LearnedRule(
        rule_id="pip", action="run", pattern="pip install", scope="shell",
        provenance="allow-always:op", matcher="prefix",
    )  # fmt: skip
    with pytest.raises(PolicyWriteRejected, match=r"§5\.4 check 3"):
        actions.store_grant(home.store, rule)
    assert home.learned() == []
    exact = rule.model_copy(update={"rule_id": "pip-one", "matcher": "exact_argv",
                                    "pattern": "pip install requests==2.32.3"})  # fmt: skip
    actions.store_grant(home.store, exact)  # a card's exact line is fine


# -- every writer runs the one check: a writer that skips it fails here --


def _writers(home: Home) -> dict[str, Callable[[], Any]]:
    repo = home.root / "repos" / "r"
    repo.mkdir(parents=True, exist_ok=True)
    return {
        "store_grant/keep": lambda: _keep(home, "ls -la"),
        "learn_policy_rule": lambda: _fetch_domain(home, "docs.example.org"),
        "worker remember": lambda: _worker_remember(home, "make test"),
        "set_operator_policy": lambda: _overlay_fetch(home, "docs.example.org"),
        "PUT /api/policy": lambda: actions.update_policy(
            home.store, home.holder, {"card_timeout_seconds": 60}
        ),
        "set_policy_group": lambda: actions.set_policy_group(
            home.store, name="web", policy={"default_network": ["example.org"]}
        ),
        "attach_policy_group": lambda: actions.attach_policy_group(
            home.store, project_id="p", name="python-bootstrap"
        ),
        "project setup": lambda: setup_project_record(
            run_store=home.store,
            root=home.root / "repos",
            project_id="q",
            name="q",
            strategy="trusted_local_dev",
            phase="build",
            repo_path=str(repo),
            seed_default_schedules=False,
            seed_shell_commands=False,
        ),
        "baseline approve": lambda: actions.approve_policy_baseline(
            home.config.home, template="builder", approved_by="op", store=home.store
        ),
        "setup --template": lambda: main(
            ["--home", str(home.root), "setup", "--template", "builder", "--apply"]
        ),
    }


def test_every_policy_writer_runs_the_one_check_before_it_writes(
    home: Home, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise PolicyWriteRejected("policy write refused, nothing was stored: probe")

    monkeypatch.setattr(policy_write, "vet_write", refuse)
    for name, write in _writers(home).items():
        before = home.snapshot()
        try:
            outcome = write()
        except (ValueError, HTTPException) as exc:
            assert "probe" in str(getattr(exc, "detail", exc)), name
        else:
            assert outcome == 2, f"{name} wrote without the check"
        assert home.snapshot() == before, name


def test_every_writer_passes_an_ordinary_write(home: Home) -> None:
    for name, write in _writers(home).items():
        outcome = write()
        assert outcome is not None and outcome != 2, name


# -- shipped templates, stricter writes, re-approval --


@pytest.mark.parametrize("template", TEMPLATE_NAMES)
def test_every_shipped_template_passes_its_own_check(home: Home, template: str) -> None:
    _keep(home, "ls -la")  # a home with an ordinary grant
    result = actions.approve_policy_baseline(
        home.config.home, template=template, approved_by="op", store=home.store
    )
    assert result["approved"]["template"] == template
    again = actions.approve_policy_baseline(
        home.config.home, reapprove=True, approved_by="op", store=home.store
    )
    assert again["approved"]["template"] == template
    assert main(["--home", str(home.root), "setup", "--template", template, "--apply"]) == 0
    for name in TEMPLATE_NAMES:  # switching between them, both ways
        assert main(["--home", str(home.root), "setup", "--template", name, "--apply"]) == 0


def test_a_deny_is_always_accepted_and_names_the_grant_it_shadows(home: Home) -> None:
    _fetch_domain(home, "example.com")
    result = actions.set_operator_policy_rule(
        home.store, scope="network", action="fetch", pattern="example.com", verdict="deny"
    )
    assert any("network:fetch:example.com" in w for w in result["warnings"])
    _keep(home, "curl -sI https://example.com")
    result = actions.approve_policy_baseline(
        home.config.home, template="builder", approved_by="op", store=home.store
    )
    home.lock("builder", NO_CURL)  # a stricter baseline over the grant re-locks
    layer = pb.current_layer(home.config.home)
    vet_write(home.store, baseline=layer)


def test_an_existing_home_with_a_forbidden_combination_keeps_working(home: Home) -> None:
    """A grant inside a deny and a legacy ``filesystem read *`` written before
    the check: the next unrelated write still lands (the check only stops a
    NEW combination), and decide() still ignores the grant."""
    document = PolicyDocument.model_validate(
        {
            "scopes": [{"scope": "shell", "deny": [
                {"rule_id": "doc-no-curl", "action": "run", "pattern": "curl"}]}],
            "learned": [
                {"rule_id": "curl-old", "scope": "shell", "action": "run",
                 "pattern": "curl -sI https://example.com", "provenance": "allow-always:op"},
                {"rule_id": "fs:all", "scope": "filesystem", "action": "read", "pattern": "*",
                 "provenance": "allow-always:op"},
            ],
        }
    )  # fmt: skip
    home.store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, document.model_dump_json())
    _keep(home, "ls -la")
    record = actions.policy_decide(
        home.holder, home.store, scope="shell", action="run", value="curl -sI https://example.com"
    )
    assert record["verdict"] == "deny"
    assert any("curl-old" in g for g in record["ignored_grants"])


# -- replay --


def _row(
    scope: str, action: str, value: str, verdict: str, by: str, **extra: Any
) -> dict[str, Any]:
    return {"actor": "queen", "repo": None, "scope": scope, "action": action, "value": value,
            "source": "operator", "verdict": verdict, "rule_id": by.partition("/")[2],
            "decided_by": by, **extra}  # fmt: skip


def test_replay_counts_flips_and_what_it_cannot_replay(home: Home) -> None:
    root_file = f"{home.config.home}/notes.txt"
    home.log(
        _row("shell", "run", "ls -la", "require_approval", "default/unmatched"),
        _row("shell", "run", "ls -la", "require_approval", "default/unmatched"),
        _row("filesystem", "read", root_file, "allow", "global/operator-root"),
        _row("shell", "run", "env TOKEN=***", "require_approval", "default/unmatched"),
        _row("shell", "run", "make", "allow", "dispatch/x", repo="/r"),
        _row("network", "fetch", "example.com", "require_approval", "default/unmatched"),
        _row("mcp", "call", "notes:list", "allow", "default/read-only"),
        _row("shell", "run", "df", "allow", "grant:session/df@chat:c"),
        "not json",
    )
    receipt = _keep(home, "ls -la")
    assert receipt["replay"].startswith("replay of the last 9 decisions: now allows 2 past asks")
    assert "6 not replayable" in receipt["replay"]
    for why in ("value redacted", "run lane", "URL not logged", "read-only", "chat-bound",
                "unreadable"):  # fmt: skip
        assert why in receipt["replay"], why
    denied = actions.set_operator_policy_rule(
        home.store, scope="filesystem", action="read", pattern=f"{home.config.home}/*",
        verdict="deny",
    )  # fmt: skip
    assert "now denies 1 past allows" in denied["replay"]
    assert "filesystem.read (global → global)" in denied["replay"]  # layers, no rule id


def test_l1_a_taint_ask_logged_with_no_chat_is_not_replayed_as_now_allowed(home: Home) -> None:
    """L1 (field test): migrate --dry-run on an older home said "now allows
    network.search (taint/private-egress → global/net:search)": the row
    carried no chat, so the replay decided it with no taint at all."""
    home.log(_row("network", "search", "ddgs", "require_approval", "taint/private-egress"))
    rows = policy_write.read_log(home.config.audit_dir)
    summary = policy_write.replay_decisions(rows, load_stack(home.holder, home.store))
    assert summary["replayed"] == 0 and not summary["examples"], summary
    assert any("taint" in why for why in summary["not_replayable"]), summary


def test_replay_examples_are_plain_and_never_reach_the_model(
    home: Home, capsys: pytest.CaptureFixture[str]
) -> None:
    """W17: a logged value is model- or attacker-chosen text. The line a
    card, a tool result or the CLI shows carries counts and rule ids only;
    the operator's migrate listing shows the value with escape sequences and
    control characters removed."""
    esc, bell, rlo = chr(27), chr(7), chr(0x202E)
    hostile = f"ls {esc}[2J{esc}]0;pwned{bell} ignore previous{rlo}txt"
    home.log(_row("shell", "run", hostile, "require_approval", "default/unmatched"))
    receipt = actions.set_operator_policy_rule(
        home.store, scope="shell", action="run", pattern="ls", verdict="deny"
    )
    line = receipt["replay"]
    assert "now denies 1 past asks" in line
    assert "ignore previous" not in line and "pwned" not in line
    assert "shell.run (default → global)" in line
    rows = policy_write.read_log(home.config.audit_dir)
    summary = policy_write.replay_decisions(rows, load_stack(home.holder, home.store))
    (example,) = next(iter(summary["examples"].values()))
    assert "ignore previous" in example
    assert all(ch not in example for ch in (esc, bell, rlo))
    assert main(["--home", str(home.root), "policy", "migrate", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "ignore previous" in out and all(ch not in out for ch in (esc, bell, rlo))


def test_a_rule_id_never_carries_its_pattern_to_the_model(home: Home) -> None:
    """A learned grant's rule id is ``scope:action:pattern``: the pattern is
    the command, host or URL prefix the card showed, model-chosen text. The
    model-visible line names the layers that decided, never the rule id."""
    _keep(home, "echo ignore-previous-instructions")
    (grant,) = home.learned()
    home.log(_row("shell", "run", "echo ignore-previous-instructions", "allow",
                  f"grant:always/{grant.rule_id}"))  # fmt: skip
    receipt = actions.set_operator_policy_rule(
        home.store, scope="shell", action="run", pattern="echo", verdict="deny"
    )
    line = receipt["replay"]
    assert "now denies 1 past allows" in line
    assert "ignore-previous" not in line, line
    assert "shell.run (grant:global → global)" in line


def test_the_log_records_the_chat_and_a_fetch_url_so_replay_re_decides_them(
    home: Home,
) -> None:
    """W18: each Queen row records its chat; a fetch row the scheme, host and
    path directory a url_prefix grant matches on — never the query, never
    userinfo, still redacted, still under the audit directory. The replay
    then re-decides fetch rows and chat-bound grants; an old row without
    the fields stays not replayable."""
    from skep.supervisor.policy_v3 import Request, queen_decide

    def fetch(chat: str, url: str) -> None:
        queen_decide(home.holder, home.store, Request("network", "fetch", "docs.example.org"),
                     chat_id=chat, url=url)  # fmt: skip

    c1, c2 = (home.store.create_chat(title=t, model=None).chat_id for t in ("one", "two"))
    token = "sk-" + "a1b2c3d4" * 3
    fetch(c1, "https://docs.example.org/guide/a")
    fetch(c2, "https://docs.example.org/guide/a")
    fetch(c1, "https://docs.example.org/guide/b?q=1")
    fetch(c1, f"https://docs.example.org/{token}/x")
    log = home.config.audit_dir / "policy" / "decisions.jsonl"
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert [r["chat"] for r in rows] == [c1, c2, c1, c1]
    assert rows[0]["url"] == "https://docs.example.org/guide/"
    assert rows[2]["url"] == "https://docs.example.org/guide/" and rows[2]["url_query"] is True
    assert token not in log.read_text() and "q=1" not in log.read_text()
    home.log(_row("network", "fetch", "docs.example.org", "require_approval", "default/x"))

    actions.remember_action_for_session(
        home.store, tool="read_url", args={"url": "https://docs.example.org/guide/z"},
        actor="op", tier="session", chat_id=c1,
    )  # fmt: skip
    summary = policy_write.migration_report(home.holder, home.store)["replay"]
    assert summary["flips"] == {"require_approval->allow": 1}  # c1's /guide/a only
    skipped = summary["not_replayable"]
    assert skipped.get("URL not logged (a url_prefix grant needs it)") == 1  # the old row
    assert skipped.get("query not logged") == 1
    assert skipped.get("value redacted in the log") == 1
    # a standing write replays the chat rows as their chats decide
    report: dict[str, Any] = {}
    actions.learn_policy_rule(
        home.store, rule_id="net:other", action="fetch", pattern="other.example.org",
        scope="network", provenance="allow-always:op", report=report,
    )  # fmt: skip
    assert report["replay"]["replayed"] == 2 and not report["replay"]["flips"]
    # a baseline write keeps no old layer to replay a chat row against
    approved = actions.approve_policy_baseline(
        home.config.home, template="builder", approved_by="op", store=home.store
    )
    assert "2 chat row on a baseline write (not replayed)" in approved["replay"]


def test_replay_never_blocks_and_says_when_the_log_is_empty(home: Home) -> None:
    receipt = _keep(home, "ls -la")
    assert receipt["replay"] == "replay: the decision log is empty — no past decision to compare"


# -- the card that proposes a policy write --


def test_a_policy_write_card_carries_the_replay_and_a_refused_one_gets_no_card(
    home: Home, tmp_path: Path
) -> None:
    home.log(_row("network", "search", "ddgs", "allow", "global/net:search"))
    card = policy_write_preview(
        home.store, home.holder, "set_operator_policy",
        {"scope": "network", "action": "search", "pattern": "*", "verdict": "deny"},
    )  # fmt: skip
    assert "now denies 1 past allows" in card["replay"]
    assert home.store.get_setting("operator_policy_document") is None  # nothing written
    warned = policy_write_preview(
        home.store, home.holder, "set_operator_policy",
        {"scope": "filesystem", "action": "read", "pattern": f"{tmp_path}/x/*",
         "verdict": "allow"},
    )  # fmt: skip
    assert any("ask before it searches" in w for w in warned["policy_warnings"])  # W16
    with pytest.raises(PolicyWriteRejected, match=r"§5\.4 check 1"):
        policy_write_preview(
            home.store, home.holder, "set_operator_policy",
            {"scope": "filesystem", "action": "read", "pattern": f"{Path.home()}/.ssh/*",
             "verdict": "allow"},
        )  # fmt: skip
    keep = policy_write_preview(home.store, home.holder, "run_shell", {"command": "ls -la"})
    assert keep == {}  # a keep-able card carries no replay: it is raised on every ask
    home.lock("builder", NO_CURL)
    refused = policy_write_preview(
        home.store, home.holder, "run_shell", {"command": "curl -sI https://example.com"}
    )
    assert refused["keep"] == ["once"] and "template:builder/no-curl" in refused["keep_refused"]
    for name, args in (
        ("set_policy", {"card_timeout_seconds": 60}),
        ("attach_policy_group", {"project_id": "p", "name": "python-bootstrap"}),
        ("approve_policy_baseline", {"reapprove": True}),
        ("allow_fetch_domain", {"domain": "docs.example.org"}),
    ):
        assert policy_write_preview(home.store, home.holder, name, args)["replay"], name
    assert policy_write_preview(home.store, home.holder, "set_policy", {"nope": 1}) == {}


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


def test_the_chat_card_shows_the_replay_and_refuses_at_proposal(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    secret = str(Path.home() / ".ssh" / "*")
    ollama.script_tool_call(
        "set_operator_policy",
        {"scope": "network", "action": "search", "pattern": "*", "verdict": "deny"},
    )
    events = sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "x"}).text)
    (card,) = [d["card"] for name, d in events if name == "action"]
    assert card["replay"].startswith("replay")
    ollama.script_tool_call(
        "set_operator_policy",
        {"scope": "filesystem", "action": "read", "pattern": secret, "verdict": "allow"},
    )
    ollama.script_reply("ok")
    other = client.post("/api/chats", json={}).json()["chat_id"]
    events = sse_events(client.post(f"/api/chats/{other}/messages", json={"content": "y"}).text)
    assert [d for name, d in events if name == "action"] == []
    (refusal,) = [d["result"] for name, d in events if name == "tool"]
    assert refusal["ok"] is False and "§5.4 check 1" in refusal["error"]


# -- B38: `skep setup --template` keeps the grants --


def test_b38_setup_template_keeps_grants_and_shows_them(
    home: Home, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--home", str(home.root), "setup", "--template", "builder"]) == 0
    _keep(home, "ls -la")
    _worker_remember(home, "make test")  # a worker project grant (the P3-4 review case)
    capsys.readouterr()
    assert main(["--home", str(home.root), "setup", "--template", "operator"]) == 1
    shown = capsys.readouterr().out
    assert "kept grant 'shell:run:ls -la'" in shown
    assert "kept grant 'shell:run:make test@repo:" in shown
    assert main(["--home", str(home.root), "setup", "--template", "operator", "--apply"]) == 0
    assert "kept grant 'shell:run:ls -la'" in capsys.readouterr().out
    assert [r.pattern for r in home.learned()] == ["ls -la", "make test"]


def test_b38_a_grant_the_new_template_denies_is_dropped_with_a_line(home: Home) -> None:
    _keep(home, "ls -la")
    _keep(home, "curl -sI https://example.com")
    template = load_policy_template("operator").document()
    template.scopes.append(
        PolicyDocument.model_validate(
            {"scopes": [{"scope": "shell", "deny": [
                {"rule_id": "no-curl", "action": "run", "pattern": "curl"}]}]}
        ).scopes[0]
    )  # fmt: skip
    kept, lines = policy_write.keep_grants(home.holder, home.store, template)
    assert [r.pattern for r in kept.learned] == ["ls -la"]
    assert (
        "dropped grant 'shell:run:curl -sI https://example.com': it lands inside deny "
        "global/no-curl" in lines
    )


# -- migrate --dry-run, approve --diff, doctor, B31, B82 --


def _files(root: Path) -> dict[str, bytes]:
    """Every file under the home but the sqlite store itself (opening a store
    runs its idempotent schema check and WAL bookkeeping)."""
    return {
        str(p): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and "supervisor.sqlite3" not in p.name
    }


def test_migrate_dry_run_prints_every_flip_and_writes_nothing(
    home: Home, capsys: pytest.CaptureFixture[str]
) -> None:
    home.log(_row("shell", "run", "ls -la", "require_approval", "default/unmatched"))
    _keep(home, "ls -la")  # since that row was logged, a grant answers it
    settings, files = home.store.all_settings(), _files(home.root)
    capsys.readouterr()
    assert main(["--home", str(home.root), "policy", "migrate", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "nothing is written" in out
    assert "now allows 1 past asks:" in out
    assert "shell.run `ls -la` (default/unmatched → grant:global/shell:run:ls -la)" in out
    assert home.store.all_settings() == settings
    assert _files(home.root) == files
    assert main(["--home", str(home.root), "policy", "migrate"]) == 2  # no write mode exists


def test_doctor_and_onboard_check_print_the_same_one_line(home: Home) -> None:
    from skep.status import build_status

    assert not [a for a in build_status(home.root)["advisories"] if "policy v3:" in a]
    home.log(_row("shell", "run", "ls -la", "require_approval", "default/unmatched"))
    _keep(home, "ls -la")
    (line,) = [a for a in build_status(home.root)["advisories"] if "policy v3:" in a]
    assert line.startswith("policy v3: 1 of the last 1 logged decisions decide differently now")
    assert line.endswith("`skep policy migrate --dry-run` lists them")


def test_b31_an_upgraded_home_whose_baseline_now_composes_says_so(home: Home) -> None:
    home.store.set_setting("policy_engine_v2_scopes", ["shell"])  # an upgraded home
    home.lock("builder")
    report = policy_write.migration_report(home.holder, home.store)
    assert report["b31"] is not None and "filesystem" in report["b31"]
    line = policy_write.migration_line(report)
    assert line is not None and "B31: this home predates the engine-v2 scope switch" in line
    home.store.set_setting("policy_engine_v2_scopes", list(SCOPES))
    assert policy_write.migration_report(home.holder, home.store)["b31"] is None


def test_an_already_held_combination_is_listed_not_refused(home: Home) -> None:
    document = PolicyDocument.model_validate(
        {"learned": [{"rule_id": "fs:all", "scope": "filesystem", "action": "read",
                      "pattern": "*", "provenance": "allow-always:op"}]}
    )  # fmt: skip
    home.store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, document.model_dump_json())
    held = policy_write.migration_report(home.holder, home.store)["held"]
    assert any("§5.4 check 1" in h for h in held)
    assert not any("check 2" in h for h in held)  # W16: a warning, not a forbidden pair
    _keep(home, "ls -la")  # the next write still lands


def test_reapprove_diff_shows_the_replay_before_it_relocks(
    home: Home, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    approve = ["--home", str(home.root), "policy", "approve"]
    assert main([*approve, "--template", "builder", "--yes"]) == 0
    home.log(_row("network", "search", "ddgs", "allow", "template:builder/net-search"))
    capsys.readouterr()
    assert main([*approve, "--reapprove", "--diff", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "what this layer changes:" in out and "replay of the last 1 decisions" in out
    layer = load_policy_template("builder").model_dump(mode="json")
    layer["template"] = "custom"
    layer["rules"].append({"scope": "filesystem", "verdict": "allow", "action": "read",
                           "pattern": f"{Path.home()}/.ssh/*", "rule_id": "data-read"})  # fmt: skip
    path = tmp_path / "layer.json"
    path.write_text(json.dumps(layer))
    before = home.snapshot()
    assert main([*approve, "--file", str(path), "--reapprove", "--diff", "--yes"]) == 2
    assert "template:custom/data-read" in capsys.readouterr().err
    assert home.snapshot() == before


def test_b82_put_policy_keeps_card_timeout_and_names_unknown_keys(config: SupervisorConfig) -> None:
    from .conftest import serve_client

    client = serve_client(config)
    assert client.put("/api/policy", json={"card_timeout_seconds": 600}).status_code == 200
    assert client.get("/api/policy").json()["card_timeout_seconds"] == 600
    refused = client.put("/api/policy", json={"card_timeout": 5})
    assert refused.status_code == 400 and "card_timeout" in refused.json()["detail"]


def test_a_corrupt_or_unreadable_log_never_fails_a_write(home: Home) -> None:
    """The replay is advisory: a row nothing can re-decide (a path with a NUL
    in it), a line too deeply nested to parse, or a log the serve cannot read
    is counted or skipped — the keep still lands."""
    home.log(_row("filesystem", "read", "/tmp/a\u0000b", "require_approval", "default/unmatched"))
    home.log("[" * 100_000)
    receipt = _keep(home, "ls -la")
    assert "2 not replayable (2 unreadable row)" in receipt["replay"]
    path = home.config.audit_dir / "policy" / "decisions.jsonl"
    path.chmod(0)
    try:
        if os.access(path, os.R_OK):  # pragma: no cover - running as root
            pytest.skip("root reads a 0000 file")
        receipt = _keep(home, "pwd")
        assert "could not be read" in receipt["replay"]
    finally:
        path.chmod(0o644)
    assert [r.pattern for r in home.learned()] == ["ls -la", "pwd"]


def test_a_deny_on_a_home_that_already_holds_a_combination_is_accepted(
    home: Home, tmp_path: Path
) -> None:
    """A held §5.4 pair (an outside read beside connect AND search to any
    host): denying one egress leaves the other pair, which was already held —
    the deny tightens and lands. A second outside read is a new pair."""
    from skep.supervisor.policy_schema import OPERATOR_POLICY_SETTINGS_KEY

    home.store.set_setting(OPERATOR_POLICY_SETTINGS_KEY, json.dumps({"scopes": [
        {"scope": "filesystem", "allow": [
            {"rule_id": "old-read", "action": "read", "pattern": f"{tmp_path}/data/**"}]},
        {"scope": "network", "allow": [
            {"rule_id": "old-connect", "action": "connect", "pattern": "*"},
            {"rule_id": "old-search", "action": "search", "pattern": "*"}]},
    ]}))  # fmt: skip
    actions.set_operator_policy_rule(
        home.store, scope="network", action="connect", pattern="*", verdict="deny"
    )
    more = actions.set_operator_policy_rule(
        home.store, scope="filesystem", action="read", pattern=f"{tmp_path}/more/**",
        verdict="allow",
    )  # fmt: skip
    assert any("ask before it searches" in w for w in more["warnings"])  # W16: lands


def test_a_stricter_baseline_over_held_shell_grants_is_accepted(home: Home) -> None:
    """Two held grants that let the host shell install: a baseline denying the
    first leaves the second, already held — the stricter layer locks."""
    home.store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, json.dumps({"learned": [
        {"rule_id": "old-pip", "scope": "shell", "action": "run", "pattern": "pip",
         "provenance": "allow-always:op", "matcher": "prefix"},
        {"rule_id": "old-curl", "scope": "shell", "action": "run", "pattern": "curl",
         "provenance": "allow-always:op", "matcher": "prefix"},
    ]}))  # fmt: skip
    layer = load_policy_template("builder").model_dump(mode="json")
    layer["rules"].append({"scope": "shell", "verdict": "deny", "action": "run",
                           "pattern": "pip", "rule_id": "no-pip"})  # fmt: skip
    vet_write(home.store, holder=home.holder, baseline=pb._load_layer(layer))
