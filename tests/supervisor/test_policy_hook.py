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

from .conftest import serve_client


def _seed(config: SupervisorConfig, repo: Path) -> None:
    store = RunStore(config.db_path)
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
    assert unlisted["verdict"] == "deny" and unlisted["rule_id"] == "default-deny"
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
    assert (
        out(
            {
                "verdict": "deny",
                "rule_id": "default-deny",
                "decided_by": "t/default-deny",
                "source": "run",
            }
        )[0]
        == "ask"
    )
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
