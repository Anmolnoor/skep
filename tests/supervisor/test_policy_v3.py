"""ADR 0059 P3-1: one ``policy_v3.decide()`` behind REST, the CLI, the hook
and ACP — one verdict and one ``decided_by`` per action across them (V1),
one label vocabulary (B79, B34), "no rule matched" asks where it denied
(B77), and nothing else more permissive than before."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from skep.cli import main
from skep.supervisor import RunStore
from skep.supervisor.acp.govern import open_governor
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.policy_schema import (
    _VERDICT_RANK,
    POLICY_DOCUMENT_SETTINGS_KEY,
    SCOPE_ACTIONS,
)
from skep.supervisor.policy_v3 import SCOPE_ACTIONS as UNMATCHED_VERDICT
from skep.supervisor.policy_v3 import UNMATCHED, Request, decide, load_stack
from skep.supervisor.roles import SHIPPED_ROLES, parse_delegation_chain
from skep.supervisor.serve.actions import policy_decide, policy_why
from skep.supervisor.serve.settings import ConfigHolder
from skep.worker_contract import Permissions

from .conftest import serve_client
from .policy_v3_grid import HOMES, PROBES, build_home, rows

FROZEN = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "policy_v3" / "pre_v3_verdicts.json").read_text()
)
LEGACY_LABELS = ("operator-default/", "policy/", "trusted_local_dev/", "builder/")


@pytest.fixture(autouse=True)
def _fake_user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The secret floor reads ``Path.home()``: never the real one."""
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))


def test_scope_actions_ask_everywhere_but_a_shell_in_a_repo() -> None:
    assert set(UNMATCHED_VERDICT) == {(s, a) for s, acts in SCOPE_ACTIONS.items() for a in acts}
    assert {k for k, v in UNMATCHED_VERDICT.items() if v != "require_approval"} == {
        ("shell", "run_repo")
    }
    assert UNMATCHED_VERDICT[("shell", "run_repo")] == "deny"


@pytest.mark.parametrize("kind", HOMES)
def test_no_verdict_is_more_permissive_than_before_but_unmatched(kind: str, tmp_path: Path) -> None:
    """The property: home x role x chain x repo x f1-01 probe, against the
    verdicts the pre-v3 code gave (frozen). A cell that was the unmatched
    ``default-deny`` now gets the scope action's unmatched verdict or
    stricter — never allow; every other cell keeps its verdict exactly, and
    a floor denial stays the same floor denial. P3-2: with no repo, a read
    inside an operator root allows by ``global/operator-root`` — the rule
    the Queen's read_file has always allowed it by, now in the one stack."""
    actions = {probe_id: (scope, action) for probe_id, scope, action, _ in PROBES}
    flipped = 0
    for row in rows(tmp_path, kind):
        old_verdict, old_rule, old_by = FROZEN[row["key"]].split(" ", 2)
        new_verdict = row["verdict"]
        assert not row["decided_by"].startswith(LEGACY_LABELS), row
        probe, where = row["key"].split("|")[1:3]
        if row["decided_by"] == "global/operator-root":
            assert (actions[probe], where) == (("filesystem", "read"), "none"), row
            continue
        if old_rule == "default-deny":
            floor = UNMATCHED_VERDICT[actions[probe]]
            assert _VERDICT_RANK[new_verdict] >= _VERDICT_RANK[floor], row
            flipped += new_verdict != old_verdict
            continue
        assert new_verdict == old_verdict, (row, FROZEN[row["key"]])
        if old_by.startswith("floor/"):
            assert row["decided_by"] == old_by, row
    if kind not in ("tampered", "zero"):  # there the baseline guard answers every cell
        assert flipped, "B77: some unmatched cell asks now"


def _rest(home: Path, **body: Any) -> dict[str, Any]:
    response = serve_client(build_config(home, None)).post("/api/policy/decide", json=body)
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def _cli(home: Path, capsys: pytest.CaptureFixture[str], *argv: str) -> dict[str, Any]:
    capsys.readouterr()
    assert main(["--home", str(home), "policy", *argv]) == 0
    result: dict[str, Any] = json.loads(capsys.readouterr().out)
    return result


def _hook_payload(scope: str, action: str, value: str, repo: Path) -> dict[str, Any] | None:
    """The Claude Code tool call the hook maps to this probe, if any."""
    if (scope, action) == ("shell", "run"):
        return {"tool_name": "Bash", "tool_input": {"command": value}}
    if (scope, action) == ("filesystem", "write"):
        path = str(repo / "a.txt") if value == "workspace" else value
        return {"tool_name": "Write", "tool_input": {"file_path": path}}
    if (scope, action) == ("network", "fetch"):
        return {"tool_name": "WebFetch", "tool_input": {"url": f"https://{value}/x"}}
    if (scope, action) == ("network", "search"):
        return {"tool_name": "WebSearch", "tool_input": {"query": value}}
    return None


def _hook(
    home: Path, payload: dict[str, Any], repo: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    with monkeypatch.context() as patch:
        patch.setattr("sys.stdin", io.StringIO(json.dumps({**payload, "cwd": str(repo)})))
        out = io.StringIO()
        patch.setattr("sys.stdout", out)
        assert main(["--home", str(home), "policy", "hook"]) == 0
    result: dict[str, Any] = json.loads(out.getvalue())["hookSpecificOutput"]
    return result


_HOOK = {"allow": "allow", "require_approval": "ask", "deny": "deny"}


@pytest.mark.parametrize("kind", ["legacy", "builder", "zero"])
def test_every_face_gives_one_verdict_and_one_label(
    kind: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """V1 / f1-01: REST, `skep policy decide`, `skep policy why`, the hook
    and `skep acp govern` answer every probe class with one verdict and one
    ``decided_by`` — on a home that predates the baseline, a locked builder
    baseline, and the zero state."""
    home, repo = build_home(tmp_path, kind)
    config = build_config(home, None)
    governor = open_governor(home, ["fx"])
    for _probe, scope, action, template in PROBES:
        value = template.format(home=tmp_path / "fakehome", skep=config.home, repo=repo)
        for where in (str(repo), None):
            rest = _rest(home, scope=scope, action=action, value=value, repo=where)
            want = (rest["verdict"], rest["decided_by"])
            flags = ["--repo", where] if where else []
            cli = _cli(home, capsys, "decide", scope, action, value, *flags)
            assert (cli["verdict"], cli["decided_by"]) == want, (_probe, where)
            acp = governor._decide((scope, action, value), where or "")
            assert (acp["verdict"], acp["decided_by"]) == want, (_probe, where)
            assert not rest["decided_by"].startswith(LEGACY_LABELS)
        why = _cli(home, capsys, "why", str(repo), scope, action, value)
        no_repo = _rest(home, scope=scope, action=action, value=value)
        assert (why["operator"]["verdict"], why["operator"]["decided_by"]) == (
            no_repo["verdict"],
            no_repo["decided_by"],
        ), _probe
        in_repo = _rest(home, scope=scope, action=action, value=value, repo=str(repo))
        if (scope, action) not in (("network", "search"), ("network", "fetch")):
            assert (why["run"]["verdict"], why["run"]["decided_by"]) == (
                in_repo["verdict"],
                in_repo["decided_by"],
            ), _probe
        payload = _hook_payload(scope, action, value, repo)
        if payload is not None:
            hook = _hook(home, payload, repo, monkeypatch)
            mapped = hook_value(payload, repo, value)
            decided = _rest(home, scope=scope, action=action, value=mapped, repo=str(repo))
            assert hook["permissionDecision"] == _HOOK[decided["verdict"]], _probe
            assert decided["decided_by"] in hook["permissionDecisionReason"], _probe


def hook_value(payload: dict[str, Any], repo: Path, value: str) -> str:
    """What the hook decides for the payload (it maps a checkout path to the
    run contract's ``workspace`` and a URL to its host)."""
    from skep.supervisor.policy_hook import hook_probe

    probe = hook_probe({**payload, "cwd": str(repo)})
    assert probe is not None
    return probe[2]


def _decide(home: Path, **kwargs: Any) -> dict[str, Any]:
    config = build_config(home, None)
    store = RunStore(config.db_path)
    try:
        return policy_decide(ConfigHolder(config, store), store, **kwargs)
    finally:
        store.close()


def test_b77_unmatched_asks_on_rest_and_the_cli_and_a_repo_shell_still_denies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home, repo = build_home(tmp_path, "legacy")
    for where in ({"repo": str(repo)}, {}):
        rest = _rest(home, scope="shell", action="run", value="rm -rf build", **where)
        assert (rest["verdict"], rest["rule_id"], rest["decided_by"]) == (
            "require_approval",
            "unmatched",
            UNMATCHED,
        )
        assert rest["layer"] == "default"
        repo_shell = _rest(home, scope="shell", action="run_repo", value="make", **where)
        assert (repo_shell["verdict"], repo_shell["decided_by"]) == ("deny", UNMATCHED)
    cli = _cli(home, capsys, "decide", "filesystem", "write", "/etc/x", "--repo", str(repo))
    assert (cli["verdict"], cli["decided_by"]) == ("require_approval", UNMATCHED)
    # A chained line: the unmatched segment asks, an explicit deny elsewhere still denies.
    chained = _decide(home, scope="shell", action="run", value="git status && rm -rf x",
                      repo=str(repo))  # fmt: skip
    assert (chained["verdict"], chained["decided_by"]) == ("require_approval", UNMATCHED)
    opaque = _decide(home, scope="shell", action="run", value="echo $(cat x)", repo=str(repo))
    assert opaque["verdict"] == "require_approval"


def test_b79_every_verdict_names_its_layer_in_one_vocabulary(tmp_path: Path) -> None:
    """f1-32: a learned grant, a `skep setup` document rule, a knob from the
    project, a group or the baseline, and what every run gets by construction
    each say where they came from."""
    home, repo = build_home(tmp_path, "legacy")
    config = build_config(home, None)
    store = RunStore(config.db_path)
    try:
        document = json.loads(str(store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY)))
        document["learned"] = [
            {"rule_id": "shell:run:uname -a", "scope": "shell", "action": "run",
             "pattern": "uname -a", "provenance": "learned"},
            {"rule_id": "network:fetch:docs.python.org", "scope": "network", "action": "fetch",
             "pattern": "docs.python.org", "provenance": "session:chat-1"},
        ]  # fmt: skip
        store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, json.dumps(document))
        store.add_project_policy(
            project_id="app", name="app", strategy="trusted_local_dev", phase="build",
            policy={"allowed_shell_commands": [["pnpm", "test"]], "policy_groups": ["npm"]},
        )  # fmt: skip
        store.set_setting("policy_groups", {"npm": {"default_network": ["registry.npmjs.org"]}})
    finally:
        store.close()

    def by(scope: str, action: str, value: str, repo_arg: Path | None = None) -> str:
        record = _decide(home, scope=scope, action=action, value=value,
                         repo=str(repo_arg) if repo_arg else None)  # fmt: skip
        return f"{record['verdict']} {record['decided_by']}"

    assert by("shell", "run", "uname -a") == "allow grant:global/shell:run:uname -a"
    assert by("network", "fetch", "docs.python.org") == (
        "allow grant:session/network:fetch:docs.python.org"
    )
    assert by("network", "fetch", "evil.example") == "deny global/net:evil.example"
    assert by("network", "search", "ddgs") == "allow global/net:search"
    assert by("shell", "run", "pnpm test", repo) == "allow project:app/shell:pnpm test"
    assert by("network", "connect", "registry.npmjs.org", repo) == (
        "allow group:npm/net:registry.npmjs.org"
    )
    assert by("network", "connect", "pypi.org", repo) == "allow project:app/net:pypi.org"
    assert by("filesystem", "write", "workspace", repo) == "allow dispatch/fs:workspace"
    assert by("filesystem", "write", f"{repo.resolve()}/x", repo) == (
        f"allow dispatch/fs:root:{repo.resolve()}"
    )


def test_b34_a_baseline_knob_is_labelled_by_the_baseline(tmp_path: Path) -> None:
    home, repo = build_home(tmp_path, "builder")
    record = _decide(home, scope="shell", action="run", value="npm install", repo=str(repo))
    assert (record["verdict"], record["decided_by"]) == (
        "allow",
        "template:builder/shell:npm install",
    )


def test_a_role_rule_wins_the_tie_against_unmatched_and_never_lifts_it(tmp_path: Path) -> None:
    home, repo = build_home(tmp_path, "legacy")
    asked = _decide(home, scope="shell", action="run", value="rm -rf build", repo=str(repo),
                    role="builder")  # fmt: skip
    assert (asked["verdict"], asked["decided_by"]) == (
        "require_approval",
        "role:builder/shell-learnable",
    )
    config = build_config(home, None)
    store = RunStore(config.db_path)
    try:
        bare = load_stack(ConfigHolder(config, store), store)
        in_repo = load_stack(ConfigHolder(config, store), store, repo=str(repo))
    finally:
        store.close()
    # The builder role allows coding edits; with no other rule that is still
    # unmatched — a role never grants.
    edit = decide(Request("coding", "edit", "workspace"), stack=bare, role=SHIPPED_ROLES["builder"])
    assert (edit.verdict, edit.decided_by) == ("require_approval", UNMATCHED)
    # D3: an ask only the delegator's own ask produced is deferred to a human.
    chain = parse_delegation_chain(["queen@operator"], config.home)
    held = decide(Request("shell", "run", "uv sync"), stack=in_repo, chain=chain)
    assert (held.verdict, held.rule_id, held.deferred) == (
        "require_approval",
        "exceeds_delegator",
        True,
    )
    assert held.layer == "delegation:queen@operator" and held.chain == ("queen@operator",)


def test_the_hook_maps_ask_to_ask_and_never_to_allow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook governs Claude Code's real tool calls: an unmatched command
    is put to the human (`ask`), never let through, never silently refused."""
    home, repo = build_home(tmp_path, "legacy")
    for command in ("rm -rf build", "echo $(cat x)"):
        hook = _hook(home, {"tool_name": "Bash", "tool_input": {"command": command}}, repo,
                     monkeypatch)  # fmt: skip
        assert hook["permissionDecision"] == "ask", command
        assert "default/unmatched" in hook["permissionDecisionReason"]
    pushed = _hook(home, {"tool_name": "Bash", "tool_input": {"command": "git push"}}, repo,
                   monkeypatch)  # fmt: skip
    assert pushed["permissionDecision"] == "deny"
    asked = _hook(home, {"tool_name": "WebFetch", "tool_input": {"url": "https://example.com"}},
                  repo, monkeypatch)  # fmt: skip
    assert asked["permissionDecision"] == "ask"


def test_acp_forwards_an_unmatched_ask_and_the_hard_ring_refuses_it(tmp_path: Path) -> None:
    """The ACP permission hop has a human behind the client: unmatched is
    forwarded to them. The hard ring (terminal/create, fs/write_text_file)
    has none: only allow passes, so an ask is refused, never run."""
    from .test_acp_govern import SEND, _ask, _drive, _frame

    home, repo = build_home(tmp_path, "legacy")
    rm = {"kind": "execute", "rawInput": {"command": "rm -rf build"}}
    to_agent, to_client = _drive(open_governor(home, ["fx"]), repo, [_ask(5, rm, SEND)])
    assert to_agent == []  # nothing answered for the human
    assert to_client[-1]["method"] == "session/request_permission"
    caps = {"terminal": True, "fs": {"writeTextFile": True}}
    terminal = {"sessionId": "s", "command": "rm", "args": ["-rf", "build"]}
    write = {"sessionId": "s", "path": "/etc/x", "content": "x"}
    to_agent, _ = _drive(
        open_governor(home, ["fx"]),
        repo,
        [
            _frame("a2c", id=6, method="terminal/create", params=terminal),
            _frame("a2c", id=7, method="fs/write_text_file", params=write),
        ],
        caps=caps,
    )
    assert [line["id"] for line in to_agent] == [6, 7]
    for line in to_agent:
        assert line["error"]["message"] == (
            "skep: require_approval by default/unmatched — this ring has no human to ask, "
            "so it refuses"
        )


def test_a_repo_that_resolves_to_no_run_policy_says_so(tmp_path: Path) -> None:
    """I8: an ambiguous repo name decides from the operator document and the
    row says why; a run policy that does not resolve is the run face's error
    in ``why`` and the operator lane's note in ``decide``."""
    home, repo = build_home(tmp_path, "legacy")
    config = build_config(home, None)
    (config.home.parent / "repos" / "repo" / ".git").mkdir(parents=True)  # a clone named "repo"
    ambiguous = _decide(home, scope="shell", action="run", value="ls", repo="repo")
    assert ambiguous["source"] == "operator" and "names two repos" in ambiguous["note"]
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="app", name="app", strategy="trusted_local_dev", phase="build",
            policy={"policy_groups": ["missing"]},
        )  # fmt: skip
    finally:
        store.close()
    broken = _decide(home, scope="shell", action="run", value="ls", repo=str(repo))
    assert broken["source"] == "operator"
    assert broken["note"].startswith("run policy did not resolve: ")
    store = RunStore(config.db_path)
    try:
        view = policy_why(ConfigHolder(config, store), store, str(repo), "shell", "run", "ls")
    finally:
        store.close()
    assert "missing" in view["run"]["error"] and view["operator"]["decided_by"] == UNMATCHED


def test_the_decision_log_line_rotates_at_10_mb_and_holds_no_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D1: one JSON line per decide() from a wired face, under
    <audit>/policy/decisions.jsonl; past 10 MB the file becomes ``.1``. A
    path or a host is logged as is; an inline credential never is."""
    from skep.supervisor import policy_v3

    home, repo = build_home(tmp_path, "legacy")
    config = build_config(home, None)
    log = config.audit_dir / "policy" / "decisions.jsonl"
    record = _decide(home, scope="filesystem", action="write", value="/etc/x", repo=str(repo),
                     actor="t", role="builder")  # fmt: skip
    (line,) = [json.loads(raw) for raw in log.read_text().splitlines()]
    assert line == {**record, "value": "/etc/x"}
    assert set(line) >= {"at", "actor", "repo", "scope", "action", "value", "source", "verdict",
                         "rule_id", "decided_by", "layer", "role", "project"}  # fmt: skip
    secrets = (
        "curl -H 'Authorization: Bearer CANARY-1' https://x.example",
        "GITHUB_TOKEN=CANARY-2 gh api user",
        "mysql --password=CANARY-3 db",
        "git clone https://bob:CANARY-4@git.example/r",
        "curl 'https://x.example/a?api_key=CANARY-5&q=1'",
        "echo ghp_CANARY6abcdefghijklmnop",
    )
    for command in secrets:
        returned = _decide(home, scope="shell", action="run", value=command)
        assert returned["value"] == command  # the caller gets its own value back
    text = log.read_text()
    assert "CANARY" not in text and text.count("***") == len(secrets)
    assert "https://x.example" in text and "git.example/r" in text
    keep = "cat ~/skep-workspace-checkout/notes --author=me"
    assert policy_v3.redact(keep) == keep  # a word that only starts like a token stays
    monkeypatch.setattr(policy_v3, "DECISION_LOG_MAX_BYTES", log.stat().st_size)
    _decide(home, scope="network", action="search", value="ddgs")
    assert log.with_name("decisions.jsonl.1").read_text() == text
    assert [json.loads(raw)["value"] for raw in log.read_text().splitlines()] == ["ddgs"]
    store = RunStore(config.db_path)
    try:
        from skep.supervisor.serve.actions import policy_decisions

        assert policy_decisions(config.audit_dir)["total"] == len(secrets) + 2
    finally:
        store.close()


@pytest.mark.parametrize("where", ["repo", "none"])
def test_the_floor_still_denies_when_the_operator_document_will_not_load(
    where: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I4: the floor is consulted before any document is read. Before P3-1 a
    corrupt global document still answered `git push` with the floor's deny;
    loading the stack first turned it into an exception — `ask` on the hook,
    a forward to the human on ACP."""
    home, repo = build_home(tmp_path, "builder")
    store = RunStore(build_config(home, None).db_path)
    store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, "{not json")
    store.close()
    cwd = {"repo": str(repo)} if where == "repo" else {}
    push = _decide(home, scope="shell", action="run", value="git push", **cwd)
    assert (push["verdict"], push["decided_by"]) == ("deny", "floor/floor.worker-git-hard-denies")
    key = str(tmp_path / "fakehome" / ".ssh" / "id_rsa")
    read = _decide(home, scope="filesystem", action="read", value=key, **cwd)
    assert (read["verdict"], read["decided_by"]) == ("deny", "floor/floor.secret-reads")
    hook = _hook(home, {"tool_name": "Bash", "tool_input": {"command": "git push"}}, repo,
                 monkeypatch)  # fmt: skip
    assert hook["permissionDecision"] == "deny"
    with pytest.raises(ValueError):  # off the floor, the broken document still refuses
        _decide(home, scope="shell", action="run", value="ls", **cwd)


# Joined at run time: a literal `curl -u user:pass` trips the release hygiene
# scan (gitleaks curl-auth-user), which cannot tell a canary from a login.
_BASIC_AUTH = ":".join(("bob", "CANARY"))


@pytest.mark.parametrize(
    "command",
    [
        f"curl -u {_BASIC_AUTH} https://x.example",
        f"curl -u{_BASIC_AUTH} https://x.example",
        f"curl --user {_BASIC_AUTH} https://x.example",
        f"curl --user={_BASIC_AUTH} https://x.example",
        "KEY=CANARY ./deploy",
        "mysql --password='CANARY CANARY' db",
        'SECRET_TOKEN="CANARY CANARY" make',
        "curl -H 'X-Api-Key: CANARY' https://x.example",
    ],
)
def test_the_decision_log_masks_basic_auth_bare_keys_quoted_values_and_key_headers(
    command: str,
) -> None:
    """ES-F7's shapes missed curl's ``-u user:pass``, a bare ``KEY=``, the
    rest of a quoted value after its first space, and an API-key header."""
    from skep.supervisor.policy_v3 import redact

    masked = redact(command)
    assert "CANARY" not in masked and "***" in masked, masked


def test_a_writer_that_loses_the_rotation_race_still_logs_its_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Serve, the CLI and every hook process append to one decisions.jsonl.
    Two that both see it past the cap both rename it; the second rename
    found nothing and raised — and a decide() that raises is an `ask` on the
    hook, a 500 on REST and a failed dispatch."""
    from skep.supervisor import policy_v3

    log = tmp_path / "policy" / "decisions.jsonl"
    log.parent.mkdir()
    log.write_text('{"old": 1}\n')
    monkeypatch.setattr(policy_v3, "DECISION_LOG_MAX_BYTES", 1)
    real = Path.replace

    def racing(self: Path, target: Path) -> Path:
        real(self, target)  # the other writer rotates first
        return real(self, target)  # ours finds nothing to rename

    monkeypatch.setattr(Path, "replace", racing)
    policy_v3.log_decision(tmp_path, {"value": "ls"})
    assert log.with_name("decisions.jsonl.1").read_text() == '{"old": 1}\n'
    assert json.loads(log.read_text()) == {"value": "ls"}


@pytest.mark.parametrize("role", ["builder", "operator"])
def test_a_role_dispatch_keeps_the_engine_host_and_asks_for_a_network_host_no_rule_covers(
    role: str, tmp_path: Path
) -> None:
    """The stack `_within_delegation` judges grants on resolves neither the
    engine's API host nor a per-dispatch network list. The engine host rides
    provider_hosts (ADR 0059 D3), which the narrowing never sees, so a role
    dispatch keeps it. A per-dispatch host no rule covers is unmatched there —
    an ask (B77) — so it is the worker's deferred ask where a human answers
    (V7), dropped where none does, and never an allow the role lacks."""
    from skep.supervisor.policy_resolver import resolve_run_policy
    from skep.supervisor.serve.actions import delegate_permissions

    home, repo = build_home(tmp_path, "builder")
    config = build_config(home, None)
    store = RunStore(config.db_path)
    store.add_project_policy(project_id="app", name="app", strategy="trusted_local_dev",
                             phase="build", policy={"verify_command": "uv run pytest"})  # fmt: skip

    def granted(engine: str | None, network: list[str] | None) -> Permissions:
        return resolve_run_policy(
            store=store, config=config, repo=repo, caste="coding", network=network,
            env_allowlist=None, wall_clock_seconds=None, max_iterations=None,
            max_actions=None, max_provider_calls=None, execution_mode=None,
            binding_candidates=[], engine=engine,
        ).permissions  # fmt: skip

    def narrowed(permissions: Permissions, *, deferred_asks: bool) -> Permissions:
        return delegate_permissions(
            ConfigHolder(config, store), store, repo, permissions,
            role=role, delegation_chain=(), deferred_asks=deferred_asks,
        )[0]  # fmt: skip

    try:
        engine_run = granted("claude_code", None)
        assert "api.anthropic.com:443" in engine_run.provider_hosts
        held = narrowed(engine_run, deferred_asks=False)
        assert held.provider_hosts == engine_run.provider_hosts
        assert held.network == engine_run.network

        explicit = granted(None, ["files.example"])
        assert "files.example" in explicit.network
        asked = narrowed(explicit, deferred_asks=True)
        assert asked.network_ask == ["files.example"]
        assert "files.example" not in asked.network
        headless = narrowed(explicit, deferred_asks=False)
        assert "files.example" not in [*headless.network, *headless.network_ask]
    finally:
        store.close()


# --- P3-2: D2's two signed semantics, at decide() ------------------------------------------


def _lock(home: Path, name: str, *extra: dict[str, str]) -> None:
    from skep.supervisor import policy_baseline as pb
    from skep.supervisor.policy_templates import load_policy_template

    layer = load_policy_template(name).model_dump(mode="json")
    layer["rules"] = [*layer["rules"], *extra]
    pb.write_lock(build_config(home, None).home, pb._load_layer(layer), approved_by="op")


def _grant(home: Path, command: str, tier: str = "always") -> None:
    from skep.supervisor.serve.actions import remember_action_for_session

    store = RunStore(build_config(home, None).db_path)
    try:
        assert remember_action_for_session(
            store, tool="run_shell", args={"command": command}, actor="op", tier=tier
        )
    finally:
        store.close()


ASK_MAKE = {"scope": "shell", "verdict": "require_approval", "action": "run",
            "pattern": "make deploy", "rule_id": "ask-make-deploy"}  # fmt: skip
NO_CURL = {"scope": "shell", "verdict": "deny", "action": "run", "pattern": "curl",
           "rule_id": "no-curl"}  # fmt: skip


def test_decision_1_a_grant_answers_a_roles_catch_all_ask_and_a_named_ask_binds(
    tmp_path: Path,
) -> None:
    """ADR 0059 decision #1 (B56): a role's `*` ask is a default — the
    operator's exact-argv grant answers it in every role; a different argv
    still asks; a named ask binds even against a grant that is more specific
    than it (before, the most specific rule won and the grant ran)."""
    home, _repo = build_home(tmp_path, "legacy")
    _lock(home, "builder", ASK_MAKE)
    _grant(home, "make build")  # P3-4: REST has no chat, so an unbound (always) grant
    _grant(home, "make deploy now", tier="always")
    for role in (None, "builder", "assistant", "operator"):
        hit = _decide(home, scope="shell", action="run", value="make build", role=role)
        assert (hit["verdict"], hit["decided_by"]) == (
            "allow",
            "grant:global/shell:run:make build",
        )
        wider = _decide(home, scope="shell", action="run", value="make build --all", role=role)
        assert wider["verdict"] == "require_approval", (role, wider)
        named = _decide(home, scope="shell", action="run", value="make deploy now", role=role)
        assert (named["verdict"], named["decided_by"]) == (
            "require_approval",
            "template:builder/ask-make-deploy",
        )
    # A role's named rule still binds: the assistant role denies coding edits.
    edit = _decide(home, scope="coding", action="edit", value="workspace", role="assistant")
    assert edit["verdict"] == "deny"


@pytest.mark.parametrize("kind", ["legacy", "builder", "assistant"])
def test_a_workers_session_tier_never_answers_a_roles_catch_all_ask(
    kind: str, tmp_path: Path
) -> None:
    """Decision #1 is the Queen's exact-argv grant. A worker session grant (a
    plain approve on a worker card) is a PREFIX: it never answers a role's
    catch-all ask — REST with a repo, and a builder chat's delegated worker
    (dropped headless, a deferred ask where a human answers). P3-4 (B39):
    and it holds only in the repo it was approved in."""
    from skep.supervisor.policy_resolver import resolve_run_policy
    from skep.supervisor.serve.actions import delegate_permissions, remember_commands_for_session

    home, repo_a = build_home(tmp_path, kind)
    repo_b = tmp_path / "repo-b"
    repo_b.mkdir()
    config = build_config(home, None)
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(project_id="b", name="b", strategy="trusted_local_dev",
                                 phase="build", policy={})  # fmt: skip
        store.add_project_binding(project_id="b", binding_kind="repo_path",
                                  binding_value=str(repo_b.resolve()))  # fmt: skip
        remember_commands_for_session(store, [["npm", "run", "x"]], repo=repo_a)
        holder = ConfigHolder(config, store)

        def allowlist(repo: Path) -> list[list[str]]:
            return resolve_run_policy(
                store=store, config=config, repo=repo, caste="coding", network=None,
                env_allowlist=None, wall_clock_seconds=None, max_iterations=None,
                max_actions=None, max_provider_calls=None, execution_mode="sandbox",
                binding_candidates=[], engine=None,
            ).permissions.shell_allowlist  # fmt: skip

        assert ["npm", "run", "x"] not in allowlist(repo_b)  # B39: repo A's grant only
        for value in ("npm run x", "npm run x --evil"):
            other = policy_decide(holder, store, scope="shell", action="run", value=value,
                                  repo=str(repo_b))  # fmt: skip
            assert other["verdict"] == "require_approval", (value, other)
        resolved = resolve_run_policy(
            store=store, config=config, repo=repo_a, caste="coding", network=None,
            env_allowlist=None, wall_clock_seconds=None, max_iterations=None,
            max_actions=None, max_provider_calls=None, execution_mode="sandbox",
            binding_candidates=[], engine=None,
        )  # fmt: skip
        permissions = resolved.permissions
        assert ["npm", "run", "x"] in permissions.shell_allowlist
        assert resolved.shell_grants == {"npm run x": "grant:session"}
        for deferred in (False, True):
            narrowed = delegate_permissions(
                holder, store, repo_a, permissions, role=None,
                delegation_chain=["queen@builder"], deferred_asks=deferred,
            )[0]  # fmt: skip
            assert ["npm", "run", "x"] not in narrowed.shell_allowlist, deferred
            assert (["npm", "run", "x"] in narrowed.shell_ask) is deferred
    finally:
        store.close()


def test_a_fetch_grant_answers_a_roles_ask_on_read_url_and_rest_alike(tmp_path: Path) -> None:
    """P3-3 moved read_url: a fetch grant answers a role's `net-fetch-gated *`
    ask (decision #1, B56) in the Queen's own lane and in REST alike (V1)."""
    from skep.supervisor.serve.actions import remember_action_for_session
    from skep.supervisor.serve.tools import fetch_grant_decision

    home, _repo = build_home(tmp_path, "builder")
    config = build_config(home, None)
    store = RunStore(config.db_path)
    try:
        assert remember_action_for_session(
            store, tool="read_url", args={"url": "https://docs.example.org/x"}, actor="op",
            tier="always",
        )  # fmt: skip
        holder = ConfigHolder(config, store)
        for role in ("builder", "assistant", "operator"):
            # P3-4 (B74): the grant covers a URL directory, so every face asks
            # with the URL (REST/CLI: a URL value decides as its host + URL).
            for url in ("https://docs.example.org/y", "https://docs.example.org/z/w"):
                rest = policy_decide(holder, store, scope="network", action="fetch",
                                     value=url, role=role)  # fmt: skip
                queen = fetch_grant_decision(
                    store, holder, "docs.example.org", role=SHIPPED_ROLES[role], url=url
                )
                assert (rest["verdict"], rest["decided_by"]) == (queen.verdict, queen.decided_by)
                assert rest["decided_by"] == (
                    "grant:global/network:fetch:https://docs.example.org/"
                ), role
            near = policy_decide(holder, store, scope="network", action="fetch",
                                 value="https://docs.example.org/y?d=1", role=role)  # fmt: skip
            assert near["verdict"] == "require_approval"
    finally:
        store.close()


def test_decision_2_the_setup_document_only_tightens_under_a_locked_baseline(
    tmp_path: Path,
) -> None:
    """ADR 0059 decision #2 (B30): a stale `skep setup --template builder`
    document under a locked assistant baseline — its allows stop granting,
    its denies still bind. With no baseline locked it grants as before."""
    from skep.supervisor.policy_schema import PolicyRule
    from skep.supervisor.policy_templates import load_policy_template

    home, _repo = build_home(tmp_path, "legacy")
    outside = tmp_path / "outside"
    (outside / "private").mkdir(parents=True)
    document = load_policy_template("builder").document()
    for scope in document.scopes:
        if scope.scope == "filesystem":
            scope.allow.append(PolicyRule(rule_id="fs-read", action="read", pattern="*"))
            scope.deny.append(
                PolicyRule(rule_id="no-private", action="read", pattern=f"{outside}/private/*")
            )
    store = RunStore(build_config(home, None).db_path)
    store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, document.model_dump_json())
    store.close()
    read = {"scope": "filesystem", "action": "read"}
    before = _decide(home, **read, value=f"{outside}/notes.txt")
    assert (before["verdict"], before["decided_by"]) == ("allow", "global/fs-read")
    _lock(home, "assistant")
    after = _decide(home, **read, value=f"{outside}/notes.txt")
    assert (after["verdict"], after["decided_by"]) == ("require_approval", UNMATCHED)
    denied = _decide(home, **read, value=f"{outside}/private/x.txt")
    assert (denied["verdict"], denied["decided_by"]) == ("deny", "global/no-private")


def test_b31_an_upgraded_home_composes_its_baseline_in_every_scope(tmp_path: Path) -> None:
    """B31: v3 has no v1/v2 split — a home with no policy_engine_v2_scopes
    gets its approved baseline's deny like any other."""
    home, _repo = build_home(tmp_path, "legacy")
    store = RunStore(build_config(home, None).db_path)
    store.set_setting("policy_engine_v2_scopes", [])
    store.close()
    _lock(home, "builder", NO_CURL)
    curl = _decide(home, scope="shell", action="run", value="curl -sI https://example.com")
    assert (curl["verdict"], curl["decided_by"]) == ("deny", "template:builder/no-curl")


def test_b23_a_learned_rule_inside_a_deny_is_ignored_and_logged_not_raised(
    tmp_path: Path,
) -> None:
    """§5.4 #6 (ADR 0059 D4's B23 note): a grant stored before a baseline
    denied its command is ignored at decide time and named in the decision
    log; every other decision keeps working (before: LearnedRuleRejected on
    every Queen-side decision)."""
    from skep.supervisor.policy_v3 import operator_lane

    home, _repo = build_home(tmp_path, "legacy")
    _grant(home, "curl https://x.example/y", tier="always")
    _lock(home, "builder", NO_CURL)
    curl = _decide(home, scope="shell", action="run", value="curl https://x.example/y")
    assert (curl["verdict"], curl["decided_by"]) == ("deny", "template:builder/no-curl")
    assert curl["ignored_grants"] == ["shell:run:curl https://x.example/y (deny/no-curl)"]
    assert _decide(home, scope="shell", action="run", value="ls")["verdict"] == "require_approval"
    store = RunStore(build_config(home, None).db_path)
    try:
        _scopes, ignored = operator_lane(store)
    finally:
        store.close()
    assert ignored == ("shell:run:curl https://x.example/y (deny/no-curl)",)


CURL_SPELLINGS = (
    "/usr/bin/curl https://x.example/y",
    "env curl https://x.example/y",
    "env A=1 /usr/bin/curl https://x.example/y",
    "timeout 5 curl https://x.example/y",
    "command curl https://x.example/y",
    "nice -n 5 curl https://x.example/y",
    "CURL https://x.example/y",
)


@pytest.mark.parametrize("line", CURL_SPELLINGS)
def test_w5_a_program_deny_binds_its_path_and_launcher_spellings(line: str, tmp_path: Path) -> None:
    """W5 (PR #73): a grant stored for `/usr/bin/curl x` (or `env curl x`,
    `timeout 5 curl x`) under a `curl` deny was allowed: the deny matched the
    literal first word only. A deny on a program binds it however the line
    names or launches it, the way the floor already reads it."""
    home, _repo = build_home(tmp_path, "legacy")
    _grant(home, line, tier="always")
    _lock(home, "builder", NO_CURL)
    hit = _decide(home, scope="shell", action="run", value=line)
    assert (hit["verdict"], hit["decided_by"]) == ("deny", "template:builder/no-curl")


def test_w5_a_run_deny_binds_a_background_grant_and_a_repo_run(tmp_path: Path) -> None:
    """W5 (PR #73): a start_process keep (`run_background`) under a shell
    `run` deny was allowed — decide() kept the two actions apart. A deny on
    `run` binds the same program in the background and in a repo; a deny on
    `run_background` alone does not bind a foreground `run`."""
    from skep.supervisor.serve.actions import remember_action_for_session

    home, _repo = build_home(tmp_path, "legacy")
    store = RunStore(build_config(home, None).db_path)
    try:
        assert remember_action_for_session(
            store, tool="start_process", args={"command": "curl https://x.example/y"},
            actor="op", tier="always",
        )  # fmt: skip
    finally:
        store.close()
    _lock(home, "builder", NO_CURL)
    hit = _decide(home, scope="shell", action="run_background", value="curl https://x.example/y")
    assert (hit["verdict"], hit["decided_by"]) == ("deny", "template:builder/no-curl")
    bg_only = {**NO_CURL, "action": "run_background", "rule_id": "no-bg-make"}
    _lock(home, "builder", {**bg_only, "pattern": "make"})
    assert _decide(home, scope="shell", action="run", value="make")["decided_by"] != (
        "template:builder/no-bg-make"
    )


@pytest.mark.parametrize(
    ("tool", "line"),
    [("run_shell", "/usr/bin/curl https://x.example/y"),
     ("run_shell", "timeout 5 curl https://x.example/y"),
     ("start_process", "curl https://x.example/y")],
)  # fmt: skip
def test_w5_the_write_time_check_refuses_those_grants_too(
    tool: str, line: str, tmp_path: Path
) -> None:
    """§5.4 check 6 agrees with decide(): a keep W5's deny binds is refused
    when it is written, naming the deny, and nothing is stored."""
    from skep.supervisor.policy_write import PolicyWriteRejected
    from skep.supervisor.serve.actions import remember_action_for_session

    home, _repo = build_home(tmp_path, "legacy")
    _lock(home, "builder", NO_CURL)
    store = RunStore(build_config(home, None).db_path)
    try:
        with pytest.raises(PolicyWriteRejected, match="no-curl"):
            remember_action_for_session(
                store, tool=tool, args={"command": line}, actor="op", tier="always"
            )
    finally:
        store.close()


ASK_CURL = {"scope": "shell", "verdict": "require_approval", "action": "run",
            "pattern": "curl", "rule_id": "ask-curl"}  # fmt: skip


@pytest.mark.parametrize("line", CURL_SPELLINGS)
def test_w5_a_named_ask_binds_the_same_spellings_a_deny_does(line: str, tmp_path: Path) -> None:
    """ES-F49 review: a named ask binds across layers (decision #1) but was
    matched by literal first word, so an exact grant on `/usr/bin/curl x`
    (or `env curl x`, `CURL x`) answered a `curl` ask with no card. A named
    ask binds a program the way a deny does."""
    home, _repo = build_home(tmp_path, "legacy")
    _grant(home, line, tier="always")
    _lock(home, "builder", ASK_CURL)
    hit = _decide(home, scope="shell", action="run", value=line)
    assert (hit["verdict"], hit["decided_by"]) == ("require_approval", "template:builder/ask-curl")


def test_w5_a_named_run_ask_binds_a_background_grant(tmp_path: Path) -> None:
    from skep.supervisor.serve.actions import remember_action_for_session

    home, _repo = build_home(tmp_path, "legacy")
    store = RunStore(build_config(home, None).db_path)
    try:
        assert remember_action_for_session(
            store, tool="start_process", args={"command": "curl https://x.example/y"},
            actor="op", tier="always",
        )  # fmt: skip
    finally:
        store.close()
    _lock(home, "builder", ASK_CURL)
    hit = _decide(home, scope="shell", action="run_background", value="curl https://x.example/y")
    assert (hit["verdict"], hit["decided_by"]) == ("require_approval", "template:builder/ask-curl")


@pytest.mark.parametrize(
    "line",
    ["{ curl https://x.example/y; }", "! curl https://x.example/y",
     "if curl https://x.example/y; then :; fi", "function f { curl https://x.example/y; }; f",
     "xargs --max-procs 4 curl https://x.example/y", "nohup -- curl https://x.example/y",
     "timeout --signal=KILL 5 curl https://x.example/y"],
)  # fmt: skip
def test_w5_a_deny_binds_through_reserved_words_and_unknown_launcher_options(
    line: str, tmp_path: Path
) -> None:
    """ES-F49 review: `{ curl x; }` was stored as a grant and ran under a curl
    deny — the parser read `{` as the program. Reserved words that open a
    compound command are read past; a launcher option the table does not
    know still falls back to reading every suffix."""
    home, _repo = build_home(tmp_path, "legacy")
    _grant(home, line, tier="always")
    _lock(home, "builder", NO_CURL)
    hit = _decide(home, scope="shell", action="run", value=line)
    assert (hit["verdict"], hit["decided_by"]) == ("deny", "template:builder/no-curl")


@pytest.mark.parametrize(
    "line",
    ["sh -c 'curl https://x.example/y'", "bash -c 'curl https://x.example/y'",
     "zsh -c 'curl https://x.example/y'", "dash -c 'curl https://x.example/y'",
     "ksh -c 'curl https://x.example/y'", "sh -c \"bash -c 'curl https://x.example/y'\"",
     "bash -c \"sh -c \\\"zsh -c 'curl https://x.example/y'\\\"\""],
)  # fmt: skip
def test_l13_a_deny_binds_a_plain_sh_c_line_with_no_grant(line: str, tmp_path: Path) -> None:
    """L13 (field test): `sh -c 'curl x'` is opaque to the segment reader, and
    decide() matched opaque text against nothing, so a curl deny never saw
    it: an ordinary "no rule covers this" card. The deny reads the payload
    (the floor's reading) whenever it can be read statically."""
    home, _repo = build_home(tmp_path, "legacy")
    _lock(home, "builder", NO_CURL)
    hit = _decide(home, scope="shell", action="run", value=line)
    assert (hit["verdict"], hit["decided_by"]) == ("deny", "template:builder/no-curl"), line


@pytest.mark.parametrize("line", ["$(which curl) https://x.example/y", "$CURL https://x.example/y"])
def test_l13_an_unreadable_line_under_a_deny_asks_and_says_so(line: str, tmp_path: Path) -> None:
    """L13: a line whose program cannot be read statically still asks, but
    the decision says a deny exists that it may get around."""
    home, _repo = build_home(tmp_path, "legacy")
    _lock(home, "builder", NO_CURL)
    hit = _decide(home, scope="shell", action="run", value=line)
    assert hit["verdict"] == "require_approval", hit
    assert "curl" in hit["note"] and "deny" in hit["note"], hit
    plain = _decide(home, scope="shell", action="run", value="echo $(date)")
    assert plain["verdict"] == "require_approval"


@pytest.mark.parametrize(
    "line",
    ["sh -c 'ls'", "sh -c 'echo curl'", "bash -c 'grep curl notes.txt'", "echo $(date)",
     "python3 -V", "python3 tools/check.py", "node app.js"],
)  # fmt: skip
def test_l13_a_readable_line_under_a_deny_carries_no_cannot_read_warning(
    line: str, tmp_path: Path
) -> None:
    """ES-F60 review: every `sh -c` line under any deny said "skep cannot read
    which command this line runs" — but `sh -c 'ls'` is read (the floor
    unwraps it) and runs no denied program. Only a line whose program is
    really hidden (`$(…)` as the program, a variable, eval) says so."""
    home, _repo = build_home(tmp_path, "legacy")
    _lock(home, "builder", NO_CURL)
    hit = _decide(home, scope="shell", action="run", value=line)
    assert hit["verdict"] == "require_approval", hit
    assert hit.get("note") is None, hit


@pytest.mark.parametrize(
    "line",
    ["timeout 60 grep curl README.md", "time grep -r curl src", "nohup grep curl notes.txt",
     "xargs grep -l curl", "nice -n 5 echo curl", "grep curl README.md", "ls /usr/bin/curl"],
)  # fmt: skip
def test_w5_a_deny_does_not_fire_on_an_argument_of_a_launched_command(
    line: str, tmp_path: Path
) -> None:
    """ES-F49 review: the floor reads every suffix behind a launcher, so a
    `curl` deny refused `timeout 60 grep curl README.md` outright — no card
    to approve. When every launcher option is known, the deny reads only the
    command the launcher runs; the line asks like any other."""
    home, _repo = build_home(tmp_path, "legacy")
    _lock(home, "builder", NO_CURL)
    hit = _decide(home, scope="shell", action="run", value=line)
    assert hit["verdict"] == "require_approval", (line, hit)


def test_w5_a_worker_grant_stored_before_the_deny_is_dropped_however_spelled(
    tmp_path: Path,
) -> None:
    """ES-F49 review: worker grant resolution (B23's decide-time half) still
    compared a deny by literal first word, so a remembered `/usr/bin/curl x`
    stayed in a run's allowlist under a later `curl` deny and the worker ran
    it with no card. It uses the deny reading decide() uses."""
    from skep.supervisor.policy_resolver import worker_shell_grants
    from skep.supervisor.serve.actions import remember_commands_for_session

    home, repo = build_home(tmp_path, "legacy")
    store = RunStore(build_config(home, None).db_path)
    try:
        remember_commands_for_session(
            store,
            [["curl", "x"], ["/usr/bin/curl", "x"], ["env", "curl", "x"], ["make", "check"]],
            repo=repo,
        )
    finally:
        store.close()
    _lock(home, "builder", NO_CURL)
    store = RunStore(build_config(home, None).db_path)
    try:
        grants = worker_shell_grants(store, build_config(home, None).home, repo, "app")
    finally:
        store.close()
    assert "make check" in grants
    assert "/usr/bin/curl x" not in grants and "env curl x" not in grants, grants


HIDDEN_LINES = [
    "printf 'curl x' | sh",
    "echo 'curl x' | bash -s",
    "sh <<< 'curl x'",
    "bash -s < script.txt",
    "cat cmds | timeout 5 sh -",
    "python3 -c 'import os; os.system(\"curl x\")'",
    'node -e \'require("child_process").execSync("curl x")\'',
    "perl -e 'system(\"curl x\")'",
    "ruby -e 'system(\"curl x\")'",
    "sh -c 'python3 -c 1'",
    # ES-F81 review: what the first reading missed
    "bash /dev/stdin <<< 'curl x'",
    "cat cmds | python3",
    "python3 - < script.py",
    'node --eval \'require("child_process").execSync("curl x")\'',
    "python3.12 -c 'import os; os.system(\"curl x\")'",
    "lua -e 'os.execute(\"curl x\")'",
    "osascript -e 'do shell script \"curl x\"'",
]


@pytest.mark.parametrize("rule", [NO_CURL, ASK_CURL])
@pytest.mark.parametrize("line", HIDDEN_LINES)
def test_d12_a_shell_fed_on_stdin_or_inline_code_is_unreadable_under_a_deny_or_named_ask(
    line: str, rule: dict[str, str], tmp_path: Path
) -> None:
    """D12 (operator decision 2026-11-13): a shell reading its commands on
    stdin and inline interpreter code name no program a rule can read, so
    under a deny or a named ask they get what `$(which curl)` gets since
    ES-F60 — an ask whose note names the rules it may be getting around."""
    home, _repo = build_home(tmp_path, "legacy")
    _lock(home, "builder", rule)
    hit = _decide(home, scope="shell", action="run", value=line)
    assert hit["verdict"] == "require_approval", (line, hit)
    assert "`curl`" in (hit.get("note") or ""), (line, hit)


@pytest.mark.parametrize("line", ["eval curl x", "eval 'curl -sI https://x.example'"])
def test_d12_a_literal_eval_is_readable_so_a_deny_refuses_it(line: str, tmp_path: Path) -> None:
    home, _repo = build_home(tmp_path, "legacy")
    _lock(home, "builder", NO_CURL)
    hit = _decide(home, scope="shell", action="run", value=line)
    assert (hit["verdict"], hit["decided_by"]) == ("deny", "template:builder/no-curl"), line
    plain = _decide(home, scope="shell", action="run", value="eval ls -la")
    assert plain["verdict"] == "require_approval" and "cannot read" not in (plain.get("note") or "")


@pytest.mark.parametrize("line", HIDDEN_LINES[:3])
def test_d12_with_no_shell_deny_or_named_ask_nothing_changes(line: str, tmp_path: Path) -> None:
    home, _repo = build_home(tmp_path, "zero")
    hit = _decide(home, scope="shell", action="run", value=line)
    assert "cannot read" not in (hit.get("note") or ""), (line, hit)
