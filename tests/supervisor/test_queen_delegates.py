"""first-run R3 (ADR 0055 D5): the Queen delegates. A worker dispatched with
an explicit role gets that role's layer AND its delegator's bound; every hop
is a ``delegated`` ledger row with its role; the run cards read the chain off
the dispatch decision. Temp homes, the builder baseline approved."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from skep.cli import main
from skep.cli_chat import _tool_summary
from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.roles import EXCEEDS_DELEGATOR, delegation_line
from skep.supervisor.serve.actions import (
    created_transition_views_for_task,
    decision_detail_view,
    policy_decisions,
    submit_run,
    submit_team,
)
from skep.supervisor.serve.app import STATIC_DIR
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import execute_mutation

from .conftest import FAKE_WORKER, serve_client
from .test_role_policy import _home

APP_POLICY = {
    "allowed_shell_commands": [["uv", "sync"], ["git", "status"]],
    "default_network": ["pypi.org"],
}


@pytest.fixture
def config(tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> SupervisorConfig:
    config = _home(tmp_path, repo, capsys)
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="app",
            name="app",
            strategy="trusted_local_dev",
            phase="build",
            policy=APP_POLICY,
        )
    finally:
        store.close()
    return config


class _Runner:
    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []

    def submit(self, repo: Path, instructions: str, **kwargs: Any) -> str:
        self.submitted.append({"instructions": instructions, **kwargs})
        return f"task-{len(self.submitted)}"

    def shutdown(self, **_: Any) -> None:
        pass


def _dispatch(config: SupervisorConfig, repo: Path, **kwargs: Any) -> dict[str, Any]:
    store = RunStore(config.db_path)
    runner = _Runner()
    try:
        submit_run(
            ConfigHolder(config, store),
            runner,  # type: ignore[arg-type]
            store,
            repo=str(repo),
            instructions="x",
            **kwargs,
        )
    finally:
        store.close()
    return runner.submitted[0]


def _worker_cmd() -> str:
    return shlex.join([sys.executable, str(FAKE_WORKER)])


def _ledger(config: SupervisorConfig) -> list[dict[str, Any]]:
    path = config.audit_dir / "policy" / "decisions.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_a_worker_role_is_held_to_its_delegator(config: SupervisorConfig, repo: Path) -> None:
    """Queen `operator` → worker `builder`: builder alone would keep `uv sync`,
    but the operator Queen only asks for it, so the worker loses it (and
    cards for it); `git status` both hold, so it stays. The decision carries
    the chain and the worker's role; the hop is one `delegated` row."""
    alone = _dispatch(config, repo, role="builder")
    assert ["uv", "sync"] in alone["permissions"].shell_allowlist
    sent = _dispatch(config, repo, role="builder", delegation_chain=["queen@operator"])
    allowlist = sent["permissions"].shell_allowlist
    assert ["uv", "sync"] not in allowlist and ["git", "status"] in allowlist
    decision = sent["dispatch_decision"]
    assert decision.delegation == {"chain": ["queen@operator"], "role": "builder", "team": None}
    assert "delegated by queen@operator · role builder; beyond the delegator, dropped: " in (
        decision.detail
    )
    *drops, hop = _ledger(config)[1:]  # [0] is the builder-alone dispatch's hop
    # The operator Queen asks for installs and test runners too: each drops,
    # each recorded under the worker's role (G10 still re-verifies, I2).
    assert {(d["rule_id"], d["role"]) for d in drops} == {(EXCEEDS_DELEGATOR, "builder")}
    assert {"uv sync", "uv run pytest"} <= {d["value"] for d in drops}
    assert "git status" not in {d["value"] for d in drops}
    assert {k: hop[k] for k in ("source", "actor", "value", "role", "delegation_chain")} == {
        "source": "delegated",
        "actor": "queen@operator",
        "value": "worker@builder",
        "role": "builder",
        "delegation_chain": ["queen@operator"],
    }
    # The payload the store keeps, and the view every card reads, carry it.
    view = decision_detail_view(decision.to_payload().model_dump(mode="json"))
    assert view is not None and view["delegation"]["chain"] == ["queen@operator"]


def test_the_role_alone_narrows_an_operator_dispatch(config: SupervisorConfig, repo: Path) -> None:
    """No chain, a role: the operator dispatching a worker `operator` gets
    the role's layer — `uv sync` drops under role:operator, recorded under it,
    and the hop row's actor is the operator."""
    sent = _dispatch(config, repo, role="operator")
    assert ["uv", "sync"] not in sent["permissions"].shell_allowlist
    assert sent["dispatch_decision"].delegation == {"chain": [], "role": "operator", "team": None}
    *drops, hop = _ledger(config)
    assert {d["decided_by"] for d in drops} == {"role:operator/shell-gated"}
    assert (hop["actor"], hop["value"]) == ("operator", "worker@operator")


def test_a_role_denied_writes_cannot_be_dispatched_as_a_writer(
    config: SupervisorConfig, repo: Path
) -> None:
    with pytest.raises(HTTPException) as refused:
        _dispatch(config, repo, role="assistant")
    assert refused.value.status_code == 403
    assert "role assistant: filesystem.write workspace exceeds the role" in refused.value.detail
    assert not [row for row in _ledger(config) if row.get("source") == "delegated"]


def test_no_role_no_chain_is_today(config: SupervisorConfig, repo: Path) -> None:
    sent = _dispatch(config, repo)
    assert sent["dispatch_decision"].delegation is None
    assert _ledger(config) == []


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"role": "warp"}, "no role 'warp'"),
        ({"delegation_chain": ["worker@builder"]}, "a worker never dispatches"),
        ({"delegation_chain": ["queen@operator", "worker@builder"]}, "a worker never dispatches"),
    ],
)
def test_bad_roles_and_worker_hops_refuse(
    config: SupervisorConfig, repo: Path, kwargs: dict[str, Any], message: str
) -> None:
    with pytest.raises(HTTPException) as refused:
        _dispatch(config, repo, **kwargs)
    assert refused.value.status_code == 400 and message in refused.value.detail


# --- R3-2: the faces (ADR 0050) — REST, `skep run`, the chat tools ----------


def test_rest_post_runs_takes_the_role_and_the_chain(config: SupervisorConfig, repo: Path) -> None:
    runner = _Runner()
    with serve_client(config, dispatcher=runner) as client:
        body = {"repo": str(repo), "instructions": "x", "role": "builder"}
        sent = client.post("/api/runs", json=body | {"delegation_chain": ["queen@operator"]})
        assert sent.status_code == 202, sent.text
        refused = client.post("/api/runs", json=body | {"delegation_chain": ["worker@builder"]})
        assert refused.status_code == 400 and "a worker never dispatches" in refused.text
    decision = runner.submitted[0]["dispatch_decision"]
    assert decision.delegation == {"chain": ["queen@operator"], "role": "builder", "team": None}
    assert _ledger(config)[-1]["task_id"] == "task-1"


def test_skep_run_takes_the_role_and_the_chain(
    config: SupervisorConfig, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`skep run --role --delegated-by`: the in-process dispatch narrows the
    same way, the hop row names the real task, and the run's dispatch
    decision carries the chain on the store's record."""
    run = ["--home", str(config.home.parent), "run", str(repo), "Fix. MODE:happy", "--quiet"]
    run += ["--execution-mode", "workspace", "--worker-cmd", _worker_cmd()]
    assert main([*run, "--role", "builder", "--delegated-by", "queen@operator"]) == 0
    capsys.readouterr()
    hop = _ledger(config)[-1]
    store = RunStore(config.db_path)
    try:
        (record,) = store.recent_runs(5)
        created = created_transition_views_for_task(store, record.task_id)
    finally:
        store.close()
    assert (hop["source"], hop["task_id"], hop["role"]) == ("delegated", record.task_id, "builder")
    assert created["dispatch_decision"]["delegation"]["chain"] == ["queen@operator"]
    assert main([*run, "--role", "warp"]) != 0
    assert "no role 'warp'" in capsys.readouterr().err


def test_the_queen_dispatches_a_worker_with_a_role(config: SupervisorConfig, repo: Path) -> None:
    """dispatch_run role=: the worker's role rides to submit_run beside the
    Queen's chain, and the result the chat card renders names both."""
    store = RunStore(config.db_path)
    runner = _Runner()
    try:
        chat_id = store.create_chat(title="r", model=None).chat_id
        store.set_chat_role(chat_id, "operator")
        result = execute_mutation(
            "dispatch_run",
            {"repo": str(repo), "instructions": "x", "role": "builder"},
            store=store,
            holder=ConfigHolder(config, store),
            runner=runner,  # type: ignore[arg-type]
            actor="chat-user",
            chat_id=chat_id,
        )
    finally:
        store.close()
    assert result["delegation"] == {"chain": ["queen@operator"], "role": "builder"}
    assert runner.submitted[0]["dispatch_decision"].delegation["role"] == "builder"


# --- R3-3: the team run — Queen → manager → workers, one ledger thread --------


def _team(config: SupervisorConfig, repo: Path, **kwargs: Any) -> tuple[dict[str, Any], _Runner]:
    store = RunStore(config.db_path)
    runner = _Runner()
    kwargs.setdefault("tasks", [{"repo": str(repo), "instructions": f"part {n}"} for n in (1, 2)])
    try:
        team = submit_team(
            ConfigHolder(config, store),
            runner,  # type: ignore[arg-type]
            store,
            **kwargs,
        )
    finally:
        store.close()
    return team, runner


def test_a_team_is_one_ledger_thread_with_every_hop_and_its_role(
    config: SupervisorConfig, repo: Path
) -> None:
    """The gate: Queen `operator` → manager `builder` → two workers. Every
    member runs under the two-hop chain; the thread (one team id) holds the
    manager's hop, each worker's hop and each drop, in order, each with its
    role — and nothing else in the ledger is in it."""
    _dispatch(config, repo, role="builder")  # an unrelated row outside the thread
    team, runner = _team(config, repo, manager="builder", delegation_chain=["queen@operator"])
    chain = ["queen@operator", "manager@builder"]
    assert (team["manager"], team["delegation_chain"], team["dispatched"]) == (
        "manager@builder",
        chain,
        ["task-1", "task-2"],
    )
    for sent in runner.submitted:
        assert sent["dispatch_decision"].delegation == {
            "chain": chain,
            "role": "builder",
            "team": team["team"],
        }
        assert ["uv", "sync"] not in sent["permissions"].shell_allowlist  # the Queen only asks
    thread = policy_decisions(config.audit_dir, team=team["team"])["rows"][::-1]
    assert all(row["team"] == team["team"] for row in thread)
    hops = [row for row in thread if row.get("source") == "delegated"]
    assert [(h["actor"], h["value"], h["role"], h.get("task_id")) for h in hops] == [
        ("queen@operator", "manager@builder", "builder", None),
        ("manager@builder", "worker@builder", "builder", "task-1"),
        ("manager@builder", "worker@builder", "builder", "task-2"),
    ]
    assert hops[0]["delegation_chain"] == ["queen@operator"]
    assert all(h["delegation_chain"] == chain for h in hops[1:])
    drops = [row for row in thread if row.get("source") != "delegated"]
    assert drops and all(
        (d["rule_id"], d["role"], d["delegation_chain"]) == (EXCEEDS_DELEGATOR, "builder", chain)
        for d in drops
    )
    assert len(thread) < len(_ledger(config))  # the unrelated dispatch is not in the thread


def test_a_manager_cannot_give_a_worker_more_than_it_holds(
    config: SupervisorConfig, repo: Path
) -> None:
    """Manager `operator`, worker `builder`, dispatched by the operator: the
    worker's role would hold `uv sync`; the manager only asks — dropped, and
    the drop names the manager's hop."""
    team, runner = _team(
        config,
        repo,
        manager="operator",
        tasks=[{"repo": str(repo), "instructions": "x", "role": "builder"}],
    )
    assert ["uv", "sync"] not in runner.submitted[0]["permissions"].shell_allowlist
    drop = next(
        row
        for row in policy_decisions(config.audit_dir, team=team["team"])["rows"]
        if row.get("value") == "uv sync"
    )
    assert drop["decided_by"] == "delegation:manager@operator/role:operator/shell-gated"
    assert drop["delegation_chain"] == ["manager@operator"]


def test_a_manager_wider_than_its_queen_holds_only_what_the_queen_does(
    config: SupervisorConfig, repo: Path
) -> None:
    """Queen `assistant` → manager `builder`: the manager's role would write,
    the Queen cannot — so the manager cannot pass writes on and the member
    refuses (403), naming both hops."""
    with pytest.raises(HTTPException) as refused:
        _team(config, repo, manager="builder", delegation_chain=["queen@assistant"])
    assert refused.value.status_code == 403
    assert "delegation:manager@builder/delegation:queen@assistant/" in refused.value.detail


@pytest.mark.parametrize(
    ("chain", "message"),
    [
        # A team's member trying to dispatch a team of its own: three hops.
        (["queen@operator", "manager@builder"], "a delegatee may not delegate further"),
        # A worker in the chain at all.
        (["worker@builder"], "a worker never dispatches"),
    ],
)
def test_nothing_deeper_than_a_team_dispatches(
    config: SupervisorConfig, repo: Path, chain: list[str], message: str
) -> None:
    with pytest.raises(HTTPException) as refused:
        _team(config, repo, manager="builder", delegation_chain=chain)
    assert refused.value.status_code == 400 and message in refused.value.detail
    assert _ledger(config) == []  # refused before anything — not even the manager's hop


@pytest.mark.parametrize(
    "chain",
    [
        ["queen@operator", "manager@builder"],  # a manager hop, but no team
        ["manager@builder", "queen@operator"],  # the manager not innermost
        ["queen@operator", "manager@builder", "worker@builder"],  # a worker dispatching
    ],
)
def test_the_two_hop_limit_is_the_teams_manager_hop_only(
    config: SupervisorConfig, repo: Path, chain: list[str]
) -> None:
    for team in (None, "t"):
        if team == "t" and chain[-1] == "manager@builder":
            continue  # that one IS the team shape
        with pytest.raises(HTTPException) as refused:
            _dispatch(config, repo, delegation_chain=chain, team=team)
        assert refused.value.status_code == 400


def test_a_bad_team_refuses_before_anything_dispatches(
    config: SupervisorConfig, repo: Path
) -> None:
    for kwargs, message in (
        ({"manager": "warp"}, "no role 'warp'"),
        ({"manager": "builder", "tasks": []}, "a team is 1-3 tasks"),
        ({"manager": "builder", "tasks": [{"repo": str(repo)}]}, "repo and instructions"),
    ):
        with pytest.raises(HTTPException) as refused:
            _team(config, repo, **kwargs)
        assert message in refused.value.detail
    assert _ledger(config) == []


def test_every_face_dispatches_a_team(
    config: SupervisorConfig, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ADR 0050: the chat's batch_dispatch manager= (as queen@<chat role>),
    POST /api/teams and `skep team` (real fake-worker runs) — one verb."""
    store = RunStore(config.db_path)
    runner = _Runner()
    try:
        chat_id = store.create_chat(title="r", model=None).chat_id
        store.set_chat_role(chat_id, "operator")
        team = execute_mutation(
            "batch_dispatch",
            {"tasks": [{"repo": str(repo), "instructions": "x"}], "manager": "builder"},
            store=store,
            holder=ConfigHolder(config, store),
            runner=runner,  # type: ignore[arg-type]
            actor="chat-user",
            chat_id=chat_id,
        )
    finally:
        store.close()
    assert team["delegation_chain"] == ["queen@operator", "manager@builder"]
    rest = _Runner()
    with serve_client(config, dispatcher=rest) as client:
        task = {"repo": str(repo), "instructions": "x", "role": "operator"}
        made = client.post("/api/teams", json={"manager": "builder", "tasks": [task]})
        assert made.status_code == 202, made.text
        assert made.json()["delegation_chain"] == ["manager@builder"]
    assert rest.submitted[0]["dispatch_decision"].delegation["role"] == "operator"
    cli = ["--home", str(config.home.parent), "team", "--manager", "builder"]
    cli += ["--task", str(repo), "Fix. MODE:happy", "--delegated-by", "queen@operator"]
    cli += ["--execution-mode", "workspace", "--worker-cmd", _worker_cmd()]
    assert main(cli) == 0
    out = capsys.readouterr().out
    assert "(queen@operator → manager@builder → workers)" in out and "completed" in out
    team_id = out.split()[1]
    decisions = ["--home", str(config.home.parent), "policy", "decisions", "--team", team_id]
    assert main(decisions) == 0
    view = json.loads(capsys.readouterr().out)
    assert view["summary"]["delegated"] == 2  # the manager's hop and the worker's


# --- R3-4: the cards show the chain — web, chat, CLI ------------------------


def test_delegation_line_reads_the_chain_down_to_the_worker() -> None:
    assert delegation_line(None) == ""
    assert delegation_line({"chain": [], "role": "operator", "team": None}) == (
        "operator → worker@operator"
    )
    assert delegation_line(
        {"chain": ["queen@operator", "manager@builder"], "role": "builder", "team": "T1"}
    ) == ("queen@operator → manager@builder → worker@builder · team T1")
    assert delegation_line({"chain": ["queen@operator"], "role": None}) == "queen@operator → worker"


def test_every_run_view_shows_the_chain(
    config: SupervisorConfig, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A real (fake-worker) delegated run: `skep review` prints the chain,
    GET /api/runs and /api/runs/{id} carry it for the web cards, and the
    chat client's tool summary and /runs line render it."""
    home = ["--home", str(config.home.parent)]
    run = [*home, "run", str(repo), "Fix. MODE:happy", "--quiet", "--execution-mode", "workspace"]
    assert main([*run, "--worker-cmd", _worker_cmd(), "--role", "builder"]) == 0
    assert main([*run, "--worker-cmd", _worker_cmd(), "--delegated-by", "queen@operator"]) == 0
    capsys.readouterr()
    store = RunStore(config.db_path)
    try:
        roled, chained = (r.task_id for r in reversed(store.recent_runs(2)))
    finally:
        store.close()
    assert main([*home, "review", chained]) == 0
    assert "delegated:    queen@operator → worker\n" in capsys.readouterr().out
    with serve_client(config) as client:
        detail = client.get(f"/api/runs/{roled}").json()
        listed = {r["task_id"]: r for r in client.get("/api/runs").json()["runs"]}
    assert detail["dispatch_decision"]["delegation"]["role"] == "builder"
    assert listed[chained]["dispatch_decision"]["delegation"]["chain"] == ["queen@operator"]
    summary = _tool_summary(
        "dispatch_run",
        {"task_id": "t1", "delegation": {"chain": ["queen@operator"], "role": "builder"}},
        "",
    )
    assert summary == "✓ run t1  (queen@operator → worker@builder)"
    team = {"team": "T1", "delegation_chain": ["queen@operator", "manager@builder"], "count": 2}
    assert _tool_summary("batch_dispatch", {"ok": True, "result": team}, "") == (
        "✓ team T1: queen@operator → manager@builder → 2 worker(s)"
    )


def test_the_web_face_renders_the_chain_on_every_run_view() -> None:
    """No-build app.js (text pins, the house idiom): delegationText is the
    twin of delegation_line and sits on the run card, the run detail and the
    chat's dispatch_run / team lines."""
    source = (STATIC_DIR / "app.js").read_text()
    card = source.split("function buildRunCard(run)")[1].split("async function viewRuns")[0]
    assert "delegationText(run.dispatch_decision.delegation)" in card
    assert 'kv("delegated"' in source
    tool_line = source.split("const toolLine = (tool, raw = null) => {")[1][:2000]
    assert "delegationText(result.delegation)" in tool_line
    assert 'tool === "batch_dispatch" && result.team' in tool_line
