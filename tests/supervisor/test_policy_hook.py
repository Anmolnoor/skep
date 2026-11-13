"""P3 (ADR 0053): decide() reachable from outside and recorded — the verb,
its route, the Claude Code hook's mapping, and the two CLI faces."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.policy_hook import hook_output, hook_probe
from skep.supervisor.serve.actions import policy_decide
from skep.supervisor.serve.settings import ConfigHolder

from .conftest import predating_home, serve_client


def _seed(config: SupervisorConfig, repo: Path) -> None:
    store = RunStore(predating_home(config.db_path))
    try:
        store.set_setting("default_execution_mode", "workspace")
        store.set_setting("allowed_shell_commands", [["uv", "sync"]])
        store.add_project_policy(
            project_id="app",
            name="app",
            strategy="trusted_local_dev",
            phase="build",
            policy={"verify_command": "pytest -q"},
        )
        store.add_project_binding(
            project_id="app", binding_kind="repo_path", binding_value=str(repo)
        )
    finally:
        store.close()


def _ledger(config: SupervisorConfig) -> list[dict[str, object]]:
    path = config.audit_dir / "policy" / "decisions.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_decide_answers_from_the_run_policy_in_a_repo_and_the_operator_otherwise(
    config: SupervisorConfig, repo: Path, tmp_path: Path
) -> None:
    _seed(config, repo)
    store = RunStore(config.db_path)
    holder = ConfigHolder(config, store)
    try:
        allowed = policy_decide(
            holder, store, scope="shell", action="run", value="uv sync", repo=str(repo), actor="t"
        )
        unlisted = policy_decide(
            holder, store, scope="shell", action="run", value="rm -rf x", repo=str(repo)
        )
        inside = policy_decide(
            holder, store, scope="filesystem", action="write", value="workspace", repo=str(repo)
        )
        search_in_repo = policy_decide(
            holder, store, scope="network", action="search", value="ddgs", repo=str(repo)
        )
        no_repo = policy_decide(holder, store, scope="network", action="search", value="ddgs")
        pushed = policy_decide(
            holder, store, scope="shell", action="run", value="git push --force", repo=str(repo)
        )
        wrecker = policy_decide(holder, store, scope="shell", action="run", value="sudo rm -rf /")
        unknown = policy_decide(
            holder, store, scope="shell", action="run", value="ls", repo=str(tmp_path / "nowhere")
        )
        with pytest.raises(ValueError, match="run, run_background, run_repo"):
            policy_decide(holder, store, scope="shell", action="execute", value="ls")
    finally:
        store.close()
    assert allowed["verdict"] == "allow" and allowed["rule_id"] == "shell:uv sync"
    assert allowed["source"] == "run" and allowed["project"]["project_id"] == "app"
    assert allowed["actor"] == "t" and allowed["at"].endswith("Z")
    # ADR 0059 B77: no rule matched is an ask on every face, as the Queen cards.
    assert (unlisted["verdict"], unlisted["decided_by"]) == (
        "require_approval",
        "default/unmatched",
    )
    assert inside["verdict"] == "allow" and inside["rule_id"] == "fs:workspace"
    # The Queen-side lanes answer from the operator document even in a repo.
    assert search_in_repo["source"] == "operator" and search_in_repo["rule_id"] == "net:search"
    assert no_repo["source"] == "operator" and no_repo["verdict"] == "allow"
    # A path nobody bound still answers — from the global run policy — and
    # the record says so rather than inventing a project (I8).
    assert unknown["source"] == "run" and unknown["project"] is None
    assert "not bound" in unknown["note"]
    # F1 (I4): the floor is consulted first, repo or not — deny, never ask.
    assert pushed["verdict"] == "deny" and pushed["source"] == "floor"
    assert pushed["rule_id"] == "floor.worker-git-hard-denies"
    assert wrecker["rule_id"] == "floor.never-grantable-prefixes" and wrecker["source"] == "floor"
    # Every call is a line in the ledger, the refused one is not.
    actors = [line["actor"] for line in _ledger(config)]
    assert actors == ["t"] + ["api"] * 7


def test_the_decide_route_records_and_refuses_unknown_actions(
    config: SupervisorConfig, repo: Path
) -> None:
    _seed(config, repo)
    client = serve_client(config)
    ok = client.post(
        "/api/policy/decide",
        json={
            "scope": "shell",
            "action": "run",
            "value": "uv sync",
            "repo": str(repo),
            "actor": "x",
        },
    )
    assert ok.status_code == 200 and ok.json()["verdict"] == "allow"
    bad = client.post(
        "/api/policy/decide", json={"scope": "kitchen", "action": "run", "value": "ls"}
    )
    assert bad.status_code == 400 and "known scopes" in bad.json()["detail"]
    assert [line["actor"] for line in _ledger(config)] == ["x"]


def test_hook_probe_maps_claude_code_tools_into_the_vocabulary(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    assert hook_probe({"tool_name": "Bash", "tool_input": {"command": "uv sync"}, "cwd": cwd}) == (
        "shell",
        "run",
        "uv sync",
    )
    assert hook_probe(
        {"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / "a.py")}, "cwd": cwd}
    ) == ("filesystem", "write", "workspace")
    assert hook_probe(
        {"tool_name": "Write", "tool_input": {"file_path": "/etc/hosts"}, "cwd": cwd}
    ) == ("filesystem", "write", "/etc/hosts")
    assert hook_probe(
        {
            "tool_name": "NotebookEdit",
            "tool_input": {"notebook_path": str(tmp_path / "n.ipynb")},
            "cwd": cwd,
        }
    ) == ("filesystem", "write", "workspace")
    assert hook_probe(
        {"tool_name": "WebFetch", "tool_input": {"url": "https://docs.example/x?y=1"}, "cwd": cwd}
    ) == ("network", "fetch", "docs.example")
    assert hook_probe({"tool_name": "WebSearch", "tool_input": {"query": "skep"}}) == (
        "network",
        "search",
        "skep",
    )
    for silent in ("Read", "Glob", "Grep", "Task", ""):
        assert hook_probe({"tool_name": silent, "tool_input": {"file_path": "x"}}) is None


def test_hook_output_maps_verdicts_and_never_fails_open() -> None:
    def out(record: dict[str, object] | None, **kw: str) -> tuple[str, str]:
        payload = hook_output(record, **kw)["hookSpecificOutput"]
        assert payload["hookEventName"] == "PreToolUse"
        return payload["permissionDecision"], payload["permissionDecisionReason"]

    assert (
        out(
            {
                "verdict": "allow",
                "rule_id": "shell:uv sync",
                "decided_by": "t/shell:uv sync",
                "source": "run",
            }
        )[0]
        == "allow"
    )
    assert (
        out(
            {"verdict": "deny", "rule_id": "shell:rm", "decided_by": "t/shell:rm", "source": "run"}
        )[0]
        == "deny"
    )
    assert (
        out({"verdict": "require_approval", "rule_id": "r", "decided_by": "t/r", "source": "run"})[
            0
        ]
        == "ask"
    )
    unmatched = {"verdict": "require_approval", "rule_id": "unmatched", "layer": "default"}
    decision, reason = out({**unmatched, "decided_by": "default/unmatched", "source": "run"})
    assert decision == "ask" and reason.endswith("no rule matches, so a human answers")
    # ADR 0059 P3-1: decide() names unmatched itself; a deny is a deny.
    deny = {"verdict": "deny", "rule_id": "default-deny", "decided_by": "t/default-deny"}
    assert out({**deny, "source": "run"})[0] == "deny"
    assert out({"verdict": "maybe", "source": "run"})[0] == "ask"  # never a silent allow
    decision, reason = out(None, error="store is gone")
    assert decision == "ask" and reason == "skep could not decide: store is gone"


def test_the_hook_and_decide_commands_answer_from_the_home(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.cli import main
    from skep.supervisor.cli_cmds import build_config

    home = tmp_path / "home"
    config = build_config(home, None)
    _seed(config, repo)

    def run_hook(payload: dict[str, Any]) -> dict[str, Any] | None:
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        assert main(["--home", str(home), "policy", "hook"]) == 0
        out = capsys.readouterr().out.strip()
        return json.loads(out)["hookSpecificOutput"] if out else None

    allowed = run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "uv sync"}, "cwd": str(repo)}
    )
    assert allowed is not None and allowed["permissionDecision"] == "allow"
    assert "shell:uv sync" in allowed["permissionDecisionReason"]
    unlisted = run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "npm install"}, "cwd": str(repo)}
    )
    assert unlisted is not None and unlisted["permissionDecision"] == "ask"
    assert "no rule matches" in unlisted["permissionDecisionReason"]
    # F1 (I4): the floor answers deny, never ask — the hook's reason for being.
    floored = run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "git push"}, "cwd": str(repo)}
    )
    assert floored is not None and floored["permissionDecision"] == "deny"
    assert "floor/floor.worker-git-hard-denies" in floored["permissionDecisionReason"]
    edit = run_hook(
        {"tool_name": "Edit", "tool_input": {"file_path": str(repo / "x.py")}, "cwd": str(repo)}
    )
    assert edit is not None and edit["permissionDecision"] == "allow"
    assert (
        run_hook({"tool_name": "Read", "tool_input": {"file_path": "x"}, "cwd": str(repo)}) is None
    )
    assert [line["actor"] for line in _ledger(config)] == ["claude-code"] * 4

    # The hook must answer even when skep cannot decide — ask, with the reason.
    monkeypatch.setattr(
        "skep.supervisor.serve.actions.policy_decide",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("store is gone")),
    )
    broken = run_hook({"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": str(repo)})
    assert broken is not None and broken["permissionDecision"] == "ask"
    assert "store is gone" in broken["permissionDecisionReason"]
    monkeypatch.undo()

    assert (
        main(
            [
                "--home",
                str(home),
                "policy",
                "decide",
                "shell",
                "run",
                "uv sync",
                "--repo",
                str(repo),
            ]
        )
        == 0
    )
    record = json.loads(capsys.readouterr().out)
    assert record["verdict"] == "allow" and record["actor"] == "cli"
    assert main(["--home", str(home), "policy", "decide", "shell", "execute", "ls"]) != 0
    assert "run_repo" in capsys.readouterr().err


def test_policy_decisions_reads_the_ledger_back_with_counts_and_three_faces(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ADR 0054 A3/A9 (B1-4): one reader, three faces; notes never count as
    decisions (I8)."""
    from skep.cli import main
    from skep.supervisor.cli_cmds import build_config
    from skep.supervisor.serve.actions import policy_decisions, policy_note
    from skep.supervisor.serve.tools import execute_read_tool

    home = tmp_path / "home"
    config = build_config(home, None)
    zeros = {"allow": 0, "deny": 0, "require_approval": 0, "observed": 0, "forwarded": 0}
    zeros["delegated"] = 0
    assert policy_decisions(config.audit_dir) == {"rows": [], "total": 0, "summary": zeros}

    store = RunStore(config.db_path)
    holder = ConfigHolder(config, store)
    try:
        policy_decide(holder, store, scope="shell", action="run", value="git push", actor="cli")
        policy_decide(
            holder, store, scope="shell", action="run", value="git push", actor="acp:codex-acp"
        )
        for source, actor in (("observed", "acp:codex-acp"), ("forwarded", "human@t3")):
            policy_note(
                config.audit_dir,
                source=source,  # type: ignore[arg-type]
                actor=actor,
                repo=None,
                scope="shell",
                action="run",
                value="ls",
            )
    finally:
        store.close()
    path = config.audit_dir / "policy" / "decisions.jsonl"
    rows = _ledger(config)
    # Distinct times so order and `since` are deterministic.
    for i, row in enumerate(rows):
        row["at"] = f"2026-11-0{i + 4}T10:00:00Z"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")

    everything = policy_decisions(config.audit_dir)
    assert [r["actor"] for r in everything["rows"]] == [
        "human@t3",
        "acp:codex-acp",
        "acp:codex-acp",
        "cli",
    ]
    assert everything["total"] == 4  # the malformed line is skipped
    assert everything["summary"] == {**zeros, "deny": 2, "observed": 1, "forwarded": 1}
    acp = policy_decisions(config.audit_dir, actor="acp:*")
    assert acp["total"] == 2 and acp["summary"] == {**zeros, "deny": 1, "observed": 1}
    assert policy_decisions(config.audit_dir, actor="human@*")["total"] == 1
    assert policy_decisions(config.audit_dir, since="2026-11-06")["total"] == 2
    assert policy_decisions(config.audit_dir, since="2026-11-05T10:00:00Z")["total"] == 3
    sliced = policy_decisions(config.audit_dir, limit=1)
    assert len(sliced["rows"]) == 1 and sliced["total"] == 4
    assert sliced["rows"][0]["actor"] == "human@t3"
    with pytest.raises(ValueError, match="limit"):
        policy_decisions(config.audit_dir, limit=0)

    # The three faces return the one function's answer.
    assert main(["--home", str(home), "policy", "decisions", "--actor", "acp:*"]) == 0
    assert json.loads(capsys.readouterr().out) == acp
    assert main(["--home", str(home), "policy", "decisions", "--limit", "0"]) != 0
    assert "limit" in capsys.readouterr().err
    client = serve_client(config)
    rest = client.get("/api/policy/decisions", params={"actor": "acp:*"})
    assert rest.status_code == 200 and rest.json() == acp
    assert client.get("/api/policy/decisions", params={"limit": 0}).status_code == 400
    store = RunStore(config.db_path)
    try:
        chat = execute_read_tool(
            "policy_decisions", {"actor": "acp:*"}, store=store, holder=ConfigHolder(config, store)
        )
    finally:
        store.close()
    assert chat == acp


def test_the_hook_asks_with_the_reason_when_the_baseline_is_tampered(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """first-run O2: a hand edit of the approved baseline turns the very next
    hook answer into `ask` naming baseline_tampered — never allow."""
    from skep.cli import main
    from skep.supervisor import policy_baseline as pb
    from skep.supervisor.cli_cmds import build_config
    from skep.supervisor.policy_templates import load_policy_template

    config = build_config(tmp_path, None)
    _seed(config, repo)
    pb.write_lock(config.home, load_policy_template("builder"), approved_by="op")

    def run_hook(payload: dict[str, Any]) -> dict[str, Any]:
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        assert main(["--home", str(tmp_path), "policy", "hook"]) == 0
        return dict(json.loads(capsys.readouterr().out)["hookSpecificOutput"])

    bash = {"tool_name": "Bash", "tool_input": {"command": "uv sync"}, "cwd": str(repo)}
    assert run_hook(bash)["permissionDecision"] == "allow"
    path = pb.baseline_path(config.home)
    path.chmod(0o644)  # 0444 is a courtesy, not the enforcement
    layer = json.loads(path.read_text())
    layer["settings"]["allowed_shell_commands"].append(["make"])
    path.write_text(json.dumps(layer, indent=2))
    out = run_hook(bash)
    assert out["permissionDecision"] == "ask"
    assert "baseline/baseline_tampered" in out["permissionDecisionReason"]
    edit = {"tool_name": "Edit", "tool_input": {"file_path": str(repo / "x")}, "cwd": str(repo)}
    assert run_hook(edit)["permissionDecision"] == "ask"
    assert [line["rule_id"] for line in _ledger(config)][-2:] == ["baseline_tampered"] * 2
