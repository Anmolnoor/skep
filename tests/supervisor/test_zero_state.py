"""first-run O5 (ADR 0056 D2): the ``no_baseline`` zero state. A home the
store created fresh answers every decision in an engine-v2 scope ``ask`` /
``no_baseline`` on all four entry paths — ``policy_decide``, the Claude Code
hook, ``skep acp govern`` and the Queen's standing policy (skepd) — until a
baseline is approved; a home that predates O5 keeps today's behaviour and is
told how to approve one."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from skep.cli import main
from skep.status import NO_BASELINE_WARNING, build_status
from skep.supervisor import RunStore
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.onboard import STATE_FILE, onboard_check
from skep.supervisor.policy_layers import zero_state_home
from skep.supervisor.policy_resolver import resolve_operator_policy
from skep.supervisor.serve.actions import policy_decide, policy_why
from skep.supervisor.serve.settings import ConfigHolder

from ..fixtures.acp import ACP_DIR, envelope_frames
from .test_acp_govern import FAKE, _demo_home, _ledger

ASK_FIXTURE = ACP_DIR / "claude-agent-acp" / "live-v1-ask.ndjson"


def _decide(home: Path, value: str, repo: Path | None = None) -> dict[str, Any]:
    config = build_config(home, None)
    store = RunStore(config.db_path)
    try:
        return policy_decide(
            ConfigHolder(config, store), store, scope="shell", action="run", value=value,
            repo=str(repo) if repo else None,
        )  # fmt: skip
    finally:
        store.close()


def _hook(home: Path, repo: Path, command: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo)}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    assert main(["--home", str(home), "policy", "hook"]) == 0
    monkeypatch.undo()
    return json.loads(out.getvalue())["hookSpecificOutput"]


def _govern(home: Path, repo: Path) -> bytes:
    """`skep acp govern` over the fake agent replaying a live permission ask
    (a workspace write); returns what reached the client."""
    frames = envelope_frames(ASK_FIXTURE, str(repo))
    done = subprocess.run(
        [sys.executable, "-m", "skep", "--home", str(home), "acp", "govern", "--",
         sys.executable, FAKE, str(ASK_FIXTURE), str(repo)],
        input=b"".join(raw for d, raw in frames if d == "c2a"),
        capture_output=True,
        timeout=60,
        env=dict(os.environ),
    )  # fmt: skip
    assert done.returncode == 3, done.stderr
    return done.stdout


def _operator(home: Path, scope: str, action: str, value: str) -> str | None:
    store = RunStore(build_config(home, None).db_path)
    try:
        return resolve_operator_policy(store).decision(scope, action, value).rule_id
    finally:
        store.close()


def test_a_fresh_home_asks_on_every_path_until_a_baseline_is_approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home, repo = _demo_home(tmp_path, fresh=True)
    # 1. policy_decide — with a repo (the run policy would allow) and without.
    for record in (_decide(home, "pnpm test", repo), _decide(home, "pnpm test")):
        assert (record["verdict"], record["rule_id"]) == ("require_approval", "no_baseline")
        assert record["source"] == "baseline"
    # The floor stays above the zero state.
    assert _decide(home, "git push origin main")["rule_id"] == "floor.worker-git-hard-denies"
    # 2. the hook
    hook = _hook(home, repo, "pnpm test", monkeypatch)
    assert hook["permissionDecision"] == "ask"
    assert "baseline/no_baseline" in hook["permissionDecisionReason"]
    # 3. acp govern: the permission hop is forwarded to the human, never allowed.
    assert b"session/request_permission" in _govern(home, repo)
    asked = [r for r in _ledger(home) if r["actor"].startswith("acp:") and r.get("verdict")]
    assert [(r["scope"], r["verdict"], r["rule_id"]) for r in asked] == [
        ("filesystem", "require_approval", "no_baseline")
    ]
    # 4. the Queen's standing policy (what skepd loads)
    assert _operator(home, "network", "search", "ddgs") == "no_baseline"
    assert _operator(home, "filesystem", "read", str(tmp_path)) == "no_baseline"
    # `why` names the reason.
    config = build_config(home, None)
    store = RunStore(config.db_path)
    try:
        why = policy_why(ConfigHolder(config, store), store, str(repo), "shell", "run", "ls")
        assert not store.list_chats()  # the zero state is not a tamper event: no card
    finally:
        store.close()
    assert "no_baseline" in why["baseline"]
    assert (why["run"]["rule_id"], why["operator"]["rule_id"]) == ("no_baseline", "no_baseline")

    assert main(["--home", str(home), "policy", "approve", "--template", "builder", "--yes"]) == 0
    capsys.readouterr()
    # The B3 demo sequence, by rule now: pnpm test allow · rm ask · git push deny (floor).
    ran, rm, push = (
        _decide(home, v) for v in ("pnpm test", "rm -rf build", "git push origin main")
    )
    assert (ran["verdict"], ran["rule_id"]) == ("allow", "shell-pnpm-test")
    assert (rm["verdict"], rm["rule_id"]) == ("require_approval", "shell-learnable")
    assert (push["verdict"], push["rule_id"]) == ("deny", "floor.worker-git-hard-denies")
    assert _decide(home, "pnpm test", repo)["verdict"] == "allow"
    assert _hook(home, repo, "pnpm test", monkeypatch)["permissionDecision"] == "allow"
    before = len(_ledger(home))
    assert b"session/request_permission" not in _govern(home, repo)  # skep answered it
    (write,) = [r for r in _ledger(home)[before:] if r.get("verdict")]
    assert (write["verdict"], write["rule_id"]) == ("allow", "fs:workspace")
    assert _operator(home, "network", "search", "ddgs") != "no_baseline"


def test_a_home_that_predates_o5_keeps_deciding_and_is_told_how_to_approve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, repo = _demo_home(tmp_path)  # no fresh-seed stamp, no baseline
    record = _decide(home, "pnpm test", repo)
    assert (record["verdict"], record["source"]) == ("allow", "run")
    assert "baseline" not in record.get("note", "")
    assert _hook(home, repo, "pnpm test", monkeypatch)["permissionDecision"] == "allow"
    assert _operator(home, "network", "search", "ddgs") != "no_baseline"
    assert NO_BASELINE_WARNING in build_status(home, provider_timeout=0.01)["advisories"]
    supervisor = build_config(home, None).home
    assert NO_BASELINE_WARNING in onboard_check(supervisor)["report"]


def test_the_rule_is_born_here_or_opted_in_and_binds_v2_scopes_only(tmp_path: Path) -> None:
    home, _ = _demo_home(tmp_path, fresh=True)
    assert NO_BASELINE_WARNING not in build_status(home, provider_timeout=0.01)["advisories"]
    db_path = build_config(home, None).db_path
    store = RunStore(db_path)
    try:
        assert zero_state_home(store)
        store.set_setting("policy_engine_v2_scopes", ["shell"])
    finally:
        store.close()
    assert _operator(home, "shell", "run", "ls") == "no_baseline"
    assert _operator(home, "network", "search", "ddgs") != "no_baseline"  # routed to v1

    (tmp_path / "old").mkdir()
    old, _ = _demo_home(tmp_path / "old")
    store = RunStore(build_config(old, None).db_path)
    try:
        assert not zero_state_home(store)
        (old / STATE_FILE).write_text("{}")  # `skep onboard` has started here
        assert zero_state_home(store)
    finally:
        store.close()
