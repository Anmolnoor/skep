"""first-run R2 (ADR 0055 D4): delegation is subset-only, decided in
``decide()``. The harness: delegator role x delegatee role x action, with the
escalation attempts — a delegatee asking for what its delegator lacks, chains
of length 2, a custom role trying to widen, ``unset`` in the delegator
(decision #7), the floor. Temp homes, the builder baseline approved."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from skep.cli import main
from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.policy_schema import _VERDICT_RANK
from skep.supervisor.roles import (
    EXCEEDS_DELEGATOR,
    SHIPPED_ROLES,
    resolve_role,
    role_covers,
    roles_dir,
)
from skep.supervisor.serve import actions
from skep.supervisor.serve.actions import submit_run
from skep.supervisor.serve.chat import COMMAND_ACTOR
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import execute_mutation, execute_read_tool

from .conftest import serve_client
from .test_role_policy import _decide, _home

ROLES = ("builder", "operator", "assistant")
DELEGATEES = (None, *ROLES)

# The matrix. Per action, per delegator: the verdict for each delegatee
# (no role, builder, operator, assistant). A = allow, Q = require_approval,
# D = deny; a trailing x = the delegator is what decided (exceeds_delegator).
MATRIX: dict[tuple[str, str, str], dict[str, str]] = {
    ("shell", "run", "uv sync"): {
        "builder": "A A Q Q",
        "operator": "Qx Qx Q Q",  # operator only asks for it — so its worker asks
        "assistant": "Qx Qx Q Q",
    },
    ("shell", "run", "git status"): {
        "builder": "A A A Q",
        "operator": "A A A Q",
        "assistant": "Qx Qx Qx Q",
    },
    ("coding", "edit", "workspace"): {
        "builder": "A A A D",
        "operator": "A A A D",
        "assistant": "Dx Dx Dx D",  # assistant cannot hand out repo edits
    },
    ("filesystem", "write", "workspace"): {
        "builder": "A A A D",
        "operator": "A A A D",
        "assistant": "Dx Dx Dx D",
    },
    ("network", "connect", "pypi.org"): {
        "builder": "A A A Q",
        "operator": "A A A Q",
        "assistant": "Qx Qx Qx Q",
    },
    ("email", "read", "inbox"): {  # the baseline denies it: no chain changes that
        role: "D D D D" for role in ROLES
    },
    ("shell", "run", "git push"): {role: "D D D D" for role in ROLES},  # the floor
}

_CODES = {"A": "allow", "Q": "require_approval", "D": "deny"}


def _verdict(row: dict[str, Any]) -> str:
    code = next(k for k, v in _CODES.items() if v == row["verdict"])
    return code + ("x" if row["rule_id"] == EXCEEDS_DELEGATOR else "")


@pytest.fixture
def config(tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> SupervisorConfig:
    return _home(tmp_path, repo, capsys)


def _custom_role(config: SupervisorConfig, name: str, **fields: Any) -> None:
    roles_dir(config.home).mkdir(parents=True, exist_ok=True)
    (roles_dir(config.home) / f"{name}.json").write_text(json.dumps({"name": name, **fields}))


@pytest.mark.parametrize("subject", list(MATRIX), ids=lambda s: f"{s[0]}.{s[1]}:{s[2]}")
def test_the_delegation_matrix(config: SupervisorConfig, repo: Path, subject: Any) -> None:
    scope, action, value = subject
    ask = {"scope": scope, "action": action, "value": value, "repo": str(repo)}
    alone = {role: _decide(config, role=role, **ask) for role in DELEGATEES}
    for delegator, expected in MATRIX[subject].items():
        got = []
        for delegatee in DELEGATEES:
            row = _decide(config, role=delegatee, delegation_chain=[f"queen@{delegator}"], **ask)
            assert row["delegation_chain"] == [f"queen@{delegator}"]
            # ⊆: never looser than the delegatee alone, nor than the delegator.
            for bound in (alone[delegatee], alone[delegator]):
                assert _VERDICT_RANK[row["verdict"]] >= _VERDICT_RANK[bound["verdict"]]
            got.append(_verdict(row))
        assert " ".join(got) == expected, (delegator, subject)


def test_done_criterion_5_an_operator_queen_cannot_grant_git_push(
    config: SupervisorConfig, repo: Path
) -> None:
    """Plan §1 done-criterion 5: a Queen in `operator` delegating to a worker
    cannot grant `shell.run git push` — the attempt is a deny row with the
    chain populated. The floor decides it, above every chain (I4)."""
    for delegatee in DELEGATEES:
        row = _decide(
            config,
            scope="shell",
            action="run",
            value="git push origin main",
            repo=str(repo),
            role=delegatee,
            delegation_chain=["queen@operator"],
        )
        assert (row["verdict"], row["source"]) == ("deny", "floor")
        assert row["delegation_chain"] == ["queen@operator"]
    ledger = (config.audit_dir / "policy" / "decisions.jsonl").read_text().splitlines()
    assert all(json.loads(line)["delegation_chain"] == ["queen@operator"] for line in ledger)


def test_the_row_names_the_hop_and_the_delegators_own_reason(
    config: SupervisorConfig, repo: Path
) -> None:
    row = _decide(
        config,
        scope="coding",
        action="edit",
        value="workspace",
        repo=str(repo),
        role="builder",
        delegation_chain=["queen@assistant"],
    )
    assert (row["verdict"], row["rule_id"], row["decided_by"], row["role"]) == (
        "deny",
        "exceeds_delegator",
        "delegation:queen@assistant/role:assistant/coding-denied",
        "builder",
    )


def test_a_chain_of_two_narrows_at_every_hop(config: SupervisorConfig, repo: Path) -> None:
    """queen@assistant → manager@builder → worker: the manager holds only
    what the assistant Queen gave it, so it cannot pass on repo edits."""
    ask = {"scope": "coding", "action": "edit", "value": "workspace", "repo": str(repo)}
    row = _decide(
        config, role="builder", delegation_chain=["queen@assistant", "manager@builder"], **ask
    )
    assert (row["verdict"], row["decided_by"]) == (
        "deny",
        "delegation:manager@builder/delegation:queen@assistant/role:assistant/coding-denied",
    )
    assert row["delegation_chain"] == ["queen@assistant", "manager@builder"]
    # Order matters: the inner hop is the stricter role, and it decides.
    row = _decide(
        config, role="builder", delegation_chain=["queen@builder", "manager@assistant"], **ask
    )
    assert row["decided_by"] == "delegation:manager@assistant/role:assistant/coding-denied"
    # A two-hop chain of roles that both hold it passes it on untouched.
    row = _decide(config, delegation_chain=["queen@builder", "manager@operator"], **ask)
    assert (row["verdict"], row["rule_id"]) == ("allow", "coding:workspace-edit")


def test_a_custom_role_cannot_widen_past_its_delegator(
    config: SupervisorConfig, repo: Path
) -> None:
    """A custom delegatee bound to the widest template still gets only what
    an `operator` delegator holds."""
    _custom_role(config, "widener", policy_layer="builder")
    ask = {"scope": "shell", "action": "run", "value": "uv sync", "repo": str(repo)}
    assert _decide(config, role="widener", **ask)["verdict"] == "allow"
    row = _decide(config, role="widener", delegation_chain=["queen@operator"], **ask)
    assert (row["verdict"], row["rule_id"]) == ("require_approval", "exceeds_delegator")


def test_unset_in_the_delegator_is_not_granted(config: SupervisorConfig, repo: Path) -> None:
    """Decision #7: a delegator whose role layer has no rule for the action
    holds nothing to pass on — even where the baseline beneath allows it."""
    _custom_role(config, "hollow")  # no policy layer at all
    ask = {"scope": "shell", "action": "run", "value": "uv sync", "repo": str(repo)}
    assert _decide(config, role="hollow", **ask)["verdict"] == "allow"  # unset passes through
    row = _decide(config, delegation_chain=["queen@hollow"], **ask)
    assert (row["verdict"], row["rule_id"], row["decided_by"]) == (
        "deny",
        "exceeds_delegator",
        "delegation:queen@hollow/unset",
    )
    # Covered means a rule for every part: per simple command of a shell line,
    # and per action — operator has no shell.run_background rule at all.
    operator = SHIPPED_ROLES["operator"]
    assert role_covers(operator, "shell", "run", "git status && uv sync")
    assert not role_covers(operator, "shell", "run_background", "git status")
    assert not role_covers(resolve_role("hollow", config.home), "shell", "run", "ls")


def test_an_empty_chain_is_todays_row_byte_for_byte(config: SupervisorConfig, repo: Path) -> None:
    ask = {"scope": "shell", "action": "run", "value": "uv sync", "repo": str(repo)}
    plain = _decide(config, role="operator", **ask)
    empty = _decide(config, role="operator", delegation_chain=[], **ask)
    assert {**empty, "at": None} == {**plain, "at": None}
    assert "delegation_chain" not in empty


def test_a_bad_hop_refuses_naming_what_acceptable_looks_like(
    config: SupervisorConfig, repo: Path
) -> None:
    ask = {"scope": "shell", "action": "run", "value": "ls"}
    with pytest.raises(ValueError, match=r"must be actor@role, e\.g\. queen@operator"):
        _decide(config, delegation_chain=["queen"], **ask)
    with pytest.raises(ValueError, match="no role 'warp'; known: builder, operator, assistant"):
        _decide(config, delegation_chain=["queen@warp"], **ask)


def test_why_shows_the_chain(
    config: SupervisorConfig, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`skep policy why --delegated-by` names the chain, what the delegator
    holds on each face, and holds both faces to it — the same words as the
    deny row."""
    why = ["--home", str(config.home.parent), "policy", "why", str(repo)]
    assert main([*why, "coding", "edit", "workspace", "--delegated-by", "queen@assistant"]) == 0
    view = json.loads(capsys.readouterr().out)
    assert view["delegation"]["chain"] == ["queen@assistant"]
    assert view["delegation"]["held"]["run"] == {
        "verdict": "deny",
        "decided_by": "role:assistant/coding-denied",
    }
    assert view["run"]["rule_id"] == "exceeds_delegator"
    assert "the delegatee holds no more than queen@assistant does" in view["delegation"]["note"]
    assert main([*why, "shell", "run", "uv sync", "--delegated-by", "queen@hollow"]) != 0
    assert "no role 'hollow'" in capsys.readouterr().err
    _custom_role(config, "hollow")
    assert main([*why, "shell", "run", "uv sync", "--delegated-by", "queen@hollow"]) == 0
    view = json.loads(capsys.readouterr().out)
    assert view["delegation"]["held"] == {"run": None, "operator": None}
    assert "unset is not granted" in view["delegation"]["note"]
    assert view["run"]["decided_by"] == "delegation:queen@hollow/unset"
    assert main([*why, "shell", "run", "uv sync"]) == 0
    assert "delegation" not in json.loads(capsys.readouterr().out)  # no chain, no block


def test_every_face_takes_the_chain(
    config: SupervisorConfig, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ADR 0050: policy_decide and policy_why take delegation_chain on the
    CLI, REST and (why) the chat tool."""
    ask = ["shell", "run", "uv sync"]
    decide = ["--home", str(config.home.parent), "policy", "decide", *ask]
    assert main([*decide, "--delegated-by", "queen@operator", "--delegated-by", "m@builder"]) == 0
    row = json.loads(capsys.readouterr().out)
    assert row["delegation_chain"] == ["queen@operator", "m@builder"]
    assert row["rule_id"] == "exceeds_delegator"
    with serve_client(config) as client:
        decided = client.post(
            "/api/policy/decide",
            json={"scope": "shell", "action": "run", "value": "uv sync"}
            | {"delegation_chain": ["queen@operator"]},
        ).json()
        assert (decided["delegation_chain"], decided["rule_id"]) == (
            ["queen@operator"],
            "exceeds_delegator",
        )
        params = {"repo": str(repo), "scope": "shell", "action": "run", "value": "uv sync"}
        why = client.get(
            "/api/policy/why", params={**params, "delegation_chain": ["queen@operator"]}
        ).json()
        assert why["delegation"]["chain"] == ["queen@operator"]
        refused = client.get("/api/policy/why", params={**params, "delegation_chain": ["q"]})
        assert refused.status_code == 400 and "actor@role" in refused.json()["detail"]
    store = RunStore(config.db_path)
    try:
        view = execute_read_tool(
            "policy_why",
            {**params, "delegation_chain": ["queen@operator"]},
            store=store,
            holder=ConfigHolder(config, store),
        )
        assert view["run"]["decided_by"] == "delegation:queen@operator/role:operator/shell-gated"
    finally:
        store.close()


class _Runner:
    def __init__(self) -> None:
        self.submitted: dict[str, Any] = {}

    def submit(self, repo: Path, instructions: str, **kwargs: Any) -> str:
        self.submitted = kwargs
        return "task-1"


def _dispatch(config: SupervisorConfig, repo: Path, chain: list[str]) -> dict[str, Any]:
    store = RunStore(config.db_path)
    runner = _Runner()
    try:
        store.add_project_policy(
            project_id="app",
            name="app",
            strategy="trusted_local_dev",
            phase="build",
            policy={
                "allowed_shell_commands": [["uv", "sync"], ["git", "status"]],
                "default_network": ["pypi.org", "example.com"],
            },
        )
        submit_run(
            ConfigHolder(config, store),
            runner,  # type: ignore[arg-type]
            store,
            repo=str(repo),
            instructions="x",
            delegation_chain=chain,
        )
    finally:
        store.close()
    return runner.submitted


def test_a_delegated_dispatch_drops_the_grants_beyond_the_delegator(
    config: SupervisorConfig, repo: Path
) -> None:
    """The worker an `operator` Queen dispatches loses `uv sync` from its
    allowlist (operator only asks for it — the worker cards instead, a human
    answers) and keeps `git status`; the drop is recorded with the chain."""
    plain = _dispatch(config, repo, [])
    assert ["uv", "sync"] in plain["permissions"].shell_allowlist
    delegated = _dispatch(config, repo, ["queen@operator"])
    allowlist = delegated["permissions"].shell_allowlist
    assert ["uv", "sync"] not in allowlist and ["git", "status"] in allowlist
    assert delegated["permissions"].network == plain["permissions"].network  # pypi.org held
    detail = delegated["dispatch_decision"].detail
    assert "delegated by queen@operator; beyond the delegator, dropped: " in detail
    assert "shell.run uv sync" in detail and "git status" not in detail
    ledger = [
        json.loads(line)
        for line in (config.audit_dir / "policy" / "decisions.jsonl").read_text().splitlines()
    ]
    # R3: the hop itself is one `delegated` row after the drops.
    *rows, hop = ledger
    assert (hop["source"], hop["actor"], hop["task_id"]) == (
        "delegated",
        "queen@operator",
        "task-1",
    )
    assert {(r["actor"], tuple(r["delegation_chain"]), r["rule_id"]) for r in rows} == {
        ("dispatch", ("queen@operator",), "exceeds_delegator")
    }
    assert "uv sync" in {r["value"] for r in rows}
    assert len(rows) == len(plain["permissions"].shell_allowlist) - len(allowlist)
    # Everything else about the dispatch is today's.
    rest = {k: v for k, v in delegated.items() if k not in ("permissions", "dispatch_decision")}
    assert rest == {k: v for k, v in plain.items() if k not in ("permissions", "dispatch_decision")}


def test_a_delegator_denied_workspace_writes_cannot_dispatch_a_writer(
    config: SupervisorConfig, repo: Path
) -> None:
    with pytest.raises(HTTPException) as refused:
        _dispatch(config, repo, ["queen@assistant"])
    assert refused.value.status_code == 403
    assert "filesystem.write workspace exceeds the delegator" in str(refused.value.detail)
    assert "role:assistant/fs-workspace-denied" in str(refused.value.detail)


def test_dispatch_refuses_a_chain_deeper_than_one(config: SupervisorConfig, repo: Path) -> None:
    """ADR 0055 D4: depth 1 outside a team (R3 lifts it to 2 for a team's
    manager hop only — test_queen_delegates.py)."""
    with pytest.raises(HTTPException) as refused:
        _dispatch(config, repo, ["queen@builder", "manager@builder"])
    assert refused.value.status_code == 400
    assert "a delegatee may not delegate further" in str(refused.value.detail)


def test_the_queen_dispatches_with_its_role(
    config: SupervisorConfig, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0055 D5: a chat in a role dispatches as queen@<role>; a chat with
    no role, and the operator's own typed command, pass no chain."""
    seen: list[Any] = []

    def submit(*args: Any, **kwargs: Any) -> str:
        seen.append(kwargs["delegation_chain"])
        return "t"

    monkeypatch.setattr(actions, "submit_run", submit)
    monkeypatch.setattr(actions, "preserved_resumable_hint", lambda *a, **kw: None)
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        in_role = store.create_chat(title="r", model=None).chat_id
        store.set_chat_role(in_role, "operator")
        bare = store.create_chat(title="b", model=None).chat_id
        for chat_id, actor in (
            (in_role, "chat-user"),
            (bare, "chat-user"),
            (in_role, COMMAND_ACTOR),
        ):
            execute_mutation(
                "dispatch_run",
                {"repo": str(repo), "instructions": "x"},
                store=store,
                holder=holder,
                runner=_Runner(),  # type: ignore[arg-type]
                actor=actor,
                chat_id=chat_id,
            )
    finally:
        store.close()
    assert seen == [["queen@operator"], [], []]
