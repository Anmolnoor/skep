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
    a floor denial stays the same floor denial."""
    actions = {probe_id: (scope, action) for probe_id, scope, action, _ in PROBES}
    flipped = 0
    for row in rows(tmp_path, kind):
        old_verdict, old_rule, old_by = FROZEN[row["key"]].split(" ", 2)
        new_verdict = row["verdict"]
        assert not row["decided_by"].startswith(LEGACY_LABELS), row
        if old_rule == "default-deny":
            floor = UNMATCHED_VERDICT[actions[row["key"].split("|")[1]]]
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
