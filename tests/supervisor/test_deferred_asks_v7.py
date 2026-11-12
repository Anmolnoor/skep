"""ADR 0059 D3 (V7): a grant the delegation chain makes an ask is a deferred
approval, not a drop. ``Permissions.network_ask`` / ``shell_ask`` carry it;
the worker cards it naming the chain; the approval holds for that run only;
a headless face drops it (deny); a deny never becomes an ask (B12 residue,
B22 card half, B147)."""

from __future__ import annotations

import json
import shlex
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from skep.cli import main
from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.autonomy import AutonomyDecision
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.dispatch import run_task
from skep.supervisor.policy_schema import SCOPES
from skep.supervisor.roles import parse_delegation_chain, resolve_role, role_covers
from skep.supervisor.scheduler import make_schedule, run_due
from skep.supervisor.serve import actions
from skep.supervisor.serve.actions import (
    _within_delegation,
    allow_network_host_and_resume,
    allow_shell_command_and_resume,
    pending_approval_or_409,
    policy_decide,
    require_run,
    resume_crashed_run,
    resume_past_gate,
    submit_run,
)
from skep.supervisor.serve.channels import ChannelConfig
from skep.supervisor.serve.chat import CHAT_ACTOR
from skep.supervisor.serve.jobs import Dispatcher
from skep.supervisor.serve.settings import (
    FRESH_HOME_SEEDED_AT,
    SESSION_ALLOWED_SHELL_COMMANDS,
    ConfigHolder,
)
from skep.supervisor.serve.tools import (
    _human_answers,
    execute_mutation,
    mutation_execution_decision,
)
from skep.supervisor.spawner import effective_network_domains
from skep.worker_contract import CodingWorkerTask, Permissions
from skep.workers.capabilities import (
    DELEGATED_ASK_REASON,
    CapabilityApprovalRequired,
    CapabilityRegistry,
)

from .conftest import FAKE_WORKER, predating_home
from .test_queen_delegates import _wait_run as wait_run
from .test_role_policy import _home


def _perms(**kw: object) -> Permissions:
    return Permissions.model_validate({"read": [], "write": [], "env_allowlist": [], **kw})


# --- the contract ---------------------------------------------------------


def test_asks_default_empty_and_an_old_task_still_loads(task_payload: dict[str, object]) -> None:
    """A task.json persisted before contract 0.3.7 has neither field."""
    task = CodingWorkerTask.model_validate(task_payload)
    assert task.permissions.network_ask == []
    assert task.permissions.shell_ask == []


@pytest.mark.parametrize("entry", ["*", "*.example.com", "https://example.com", "a/b", "", "x:1"])
def test_network_ask_is_bare_hosts_only(entry: str) -> None:
    """A wildcard is never askable (ADR 0059 D4: never offered `*`)."""
    with pytest.raises(ValueError, match="network_ask"):
        _perms(network_ask=[entry])


@pytest.mark.parametrize("entry", [[], [""]])
def test_shell_ask_entries_are_non_empty_argvs(entry: list[str]) -> None:
    with pytest.raises(ValueError, match="shell_ask"):
        _perms(shell_ask=[entry])


def test_asks_are_not_egress() -> None:
    """An ask opens nothing until a human approves it for that run."""
    perms = _perms(network=["pypi.org"], network_ask=["example.com"])
    assert perms.egress() == ["pypi.org"]


@pytest.fixture
def task_payload(tmp_path: Path) -> dict[str, object]:
    return {
        "contract_version": "0.3.6",
        "task_id": "t-old",
        "trace_id": "tr-old",
        "worker_kind": "coding",
        "workspace": str(tmp_path),
        "instructions": "x",
        "permissions": {
            "read": ["workspace"],
            "write": ["workspace"],
            "network": ["pypi.org"],
            "env_allowlist": [],
        },
        "budget": {
            "wall_clock_seconds": 60,
            "max_iterations": 1,
            "max_actions": 1,
            "max_provider_calls": 0,
        },
    }


@pytest.mark.parametrize("delegated", [False, True])
def test_run_task_keeps_asks_only_on_a_delegated_dispatch(
    config: SupervisorConfig, repo: Path, delegated: bool
) -> None:
    """The chain narrowing is the asks' one writer: an envelope that names
    them with no delegation (a hand-edited task, a stray caller) loses them."""
    decision = AutonomyDecision(
        verdict="allow",
        reason="dispatch.allow.test",
        delegation={"chain": ["queen@builder"], "role": None} if delegated else None,
    )
    outcome = run_task(
        repo,
        "x",
        config=config,
        permissions=_perms(
            read=["workspace"],
            write=["workspace"],
            network_ask=["example.com"],
            shell_ask=[["make"]],
        ),
        dispatch_decision=decision,
        execution_mode="workspace",
    )
    task = CodingWorkerTask.model_validate_json(
        (config.audit_dir / outcome.record.task_id / "task.json").read_text()
    )
    assert task.permissions.network_ask == (["example.com"] if delegated else [])
    assert task.permissions.shell_ask == ([["make"]] if delegated else [])


# --- the narrowing: what was dropped becomes an ask, nothing else ----------


@pytest.fixture
def locked(tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> SupervisorConfig:
    """A bound trusted_local_dev repo with the builder baseline locked."""
    return _home(tmp_path, repo, capsys)


def _zero_state(tmp_path: Path, repo: Path) -> SupervisorConfig:
    """The same home before any baseline is approved: fresh_home_seeded_at
    is set, so every engine-v2 decision answers ask baseline/no_baseline."""
    config = build_config(tmp_path, None)
    store = RunStore(config.db_path)
    try:
        assert store.get_setting(FRESH_HOME_SEEDED_AT) is not None
        store.set_setting("policy_engine_v2_scopes", list(SCOPES))
        store.set_setting("default_execution_mode", "workspace")
        store.add_project_policy(
            project_id="app", name="app", strategy="trusted_local_dev", phase="build", policy={}
        )
        store.add_project_binding(
            project_id="app", binding_kind="repo_path", binding_value=str(repo)
        )
    finally:
        store.close()
    return config


class _Runner:
    def __init__(self) -> None:
        self.submitted: dict[str, Any] = {}

    def submit(self, repo: Path, instructions: str, **kwargs: Any) -> str:
        self.submitted = kwargs
        return "task-1"


def _submit(config: SupervisorConfig, repo: Path, **kwargs: Any) -> dict[str, Any]:
    store = RunStore(config.db_path)
    runner = _Runner()
    try:
        submit_run(
            ConfigHolder(config, store),
            runner,  # type: ignore[arg-type]
            store,
            repo=str(repo),
            instructions="fix hello.py",
            **kwargs,
        )
    finally:
        store.close()
    return runner.submitted


def _narrow(
    config: SupervisorConfig,
    repo: Path,
    perms: Permissions,
    *,
    chain: list[str],
    role: str | None = None,
    deferred_asks: bool = True,
) -> tuple[Permissions, str]:
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        narrowed, _delegation, note = _within_delegation(
            holder,
            store,
            repo,
            parse_delegation_chain(chain, config.home),
            perms,
            None if role is None else resolve_role(role, config.home),
            deferred_asks=deferred_asks,
        )
    finally:
        store.close()
    return narrowed, note


def _upgraded(tmp_path: Path, repo: Path) -> SupervisorConfig:
    """An upgraded home: no baseline, not zero-state (B12's other trigger),
    and the project grants example.com."""
    config = build_config(tmp_path, None)
    predating_home(config.db_path)
    store = RunStore(config.db_path)
    try:
        store.set_setting("policy_engine_v2_scopes", list(SCOPES))
        store.set_setting("default_execution_mode", "workspace")
        store.add_project_policy(
            project_id="app",
            name="app",
            strategy="trusted_local_dev",
            phase="build",
            policy={"default_network": ["pypi.org", "example.com"]},
        )
        store.add_project_binding(
            project_id="app", binding_kind="repo_path", binding_value=str(repo)
        )
    finally:
        store.close()
    return config


def test_a_host_the_delegator_would_ask_about_becomes_a_deferred_ask(
    tmp_path: Path, repo: Path
) -> None:
    """B12 residue: the project grants example.com, queen@builder would ask
    network.connect example.com, so its worker gets an ask — not an allow
    (§5.4 check 5), not a silent drop."""
    config = _upgraded(tmp_path, repo)
    submitted = _submit(config, repo, delegation_chain=["queen@builder"], deferred_asks=True)
    perms = submitted["permissions"]
    assert perms.network_ask == ["example.com"]
    assert "example.com" not in perms.network
    assert "pypi.org" in perms.network
    detail = submitted["dispatch_decision"].detail or ""
    assert "asks a human (this run only): network.connect example.com" in detail
    assert "dropped" not in detail


def test_a_prefix_the_delegator_would_ask_about_becomes_a_shell_ask(
    locked: SupervisorConfig, repo: Path
) -> None:
    """queen@operator allows git status but asks `uv sync`."""
    perms = _submit(locked, repo, delegation_chain=["queen@operator"], deferred_asks=True)[
        "permissions"
    ]
    assert ["uv", "sync"] in perms.shell_ask
    assert ["uv", "sync"] not in perms.shell_allowlist
    assert ["git", "status"] in perms.shell_allowlist
    assert ["git", "status"] not in perms.shell_ask


def test_a_headless_dispatch_drops_the_ask_as_before(tmp_path: Path, repo: Path) -> None:
    """No human to answer a card: the narrowed grant is a deny (I6)."""
    submitted = _submit(_upgraded(tmp_path, repo), repo, delegation_chain=["queen@builder"])
    perms = submitted["permissions"]
    assert perms.network_ask == [] and perms.shell_ask == []
    assert "example.com" not in perms.network
    assert "dropped: network.connect example.com" in (submitted["dispatch_decision"].detail or "")


def test_a_host_the_delegator_denies_stays_dropped(locked: SupervisorConfig, repo: Path) -> None:
    """A delegator whose role holds nothing there is deny (decision #7) —
    and a deny never becomes an ask."""
    roles = locked.home.parent / "roles"
    roles.mkdir(exist_ok=True)
    (roles / "bare.json").write_text(json.dumps({"name": "bare"}))
    narrowed, note = _narrow(
        locked,
        repo,
        _perms(network=["example.com", "pypi.org"], shell_allowlist=[["uv", "sync"]]),
        chain=["queen@bare"],
    )
    assert narrowed.network == [] and narrowed.network_ask == []
    assert narrowed.shell_allowlist == [] and narrowed.shell_ask == []
    assert "dropped: " in note and "asks a human" not in note


def test_a_guarded_class_is_never_offered_as_an_ask(locked: SupervisorConfig, repo: Path) -> None:
    """Wildcard hosts, remote git, outbound content, privilege escalation,
    the bare launcher: none is askable, whatever the delegator says."""
    guarded = [["git", "push"], ["sudo", "ls"], ["himalaya", "message", "send"], ["timeout"]]
    narrowed, _ = _narrow(
        locked,
        repo,
        _perms(network=["*", "*.example.com"], shell_allowlist=guarded),
        chain=["queen@operator"],
    )
    assert narrowed.network_ask == []
    assert narrowed.shell_ask == []
    assert "*" not in narrowed.network and "*.example.com" not in narrowed.network


# Grants the run policy issues on these homes (the narrowing judges what the
# run policy granted; an explicit network host it never compiled is the
# separate hole noted in the ES-F11 commit).
HOSTS = ["pypi.org", "files.pythonhosted.org", "registry.npmjs.org", "example.com"]
PREFIXES = [["git", "status"], ["uv", "sync"], ["pytest"], ["npm", "test"]]


@pytest.mark.parametrize("state", ["locked", "zero", "upgraded"])
@pytest.mark.parametrize(
    ("chain", "role"),
    [
        (["queen@builder"], None),
        (["queen@operator"], None),
        (["queen@assistant"], None),
        ([], "builder"),
        ([], "operator"),
        (["queen@builder"], "operator"),
    ],
)
def test_a_child_never_holds_an_allow_its_delegator_lacks(
    tmp_path: Path,
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    state: str,
    chain: list[str],
    role: str | None,
) -> None:
    """ADR 0059 §5.4 check 5, counting ask as weaker than allow: every grant
    the worker keeps is one each delegator (and its own role) allows; every
    deferred ask is one they would ask about — never one they deny. On a
    zero-state home (B147) the delegator asks everything, so nothing stays."""
    config = {
        "locked": lambda: _home(tmp_path, repo, capsys),
        "zero": lambda: _zero_state(tmp_path, repo),
        "upgraded": lambda: _upgraded(tmp_path, repo),
    }[state]()
    hosts = HOSTS if state == "upgraded" else HOSTS[:-1]
    narrowed, _ = _narrow(
        config, repo, _perms(network=hosts, shell_allowlist=PREFIXES), chain=chain, role=role
    )
    bounds = [entry.partition("@")[2] for entry in chain] + ([role] if role else [])

    def verdicts(scope: str, action: str, value: str) -> set[str]:
        store = RunStore(config.db_path)
        try:
            holder = ConfigHolder(config, store)
            found = set()
            for name in bounds:
                bound = resolve_role(name, config.home)
                covered = role_covers(bound, scope, action, value)
                found.add(
                    policy_decide(
                        holder,
                        store,
                        scope=scope,
                        action=action,
                        value=value,
                        repo=str(repo),
                        role=name,
                    )["verdict"]
                    if covered or name == role
                    else "deny"
                )
            return found
        finally:
            store.close()

    for host in narrowed.network:
        assert verdicts("network", "connect", host) == {"allow"}, host
    for prefix in narrowed.shell_allowlist:
        assert verdicts("shell", "run", shlex.join(prefix)) == {"allow"}, prefix
    for host in narrowed.network_ask:
        assert "deny" not in verdicts("network", "connect", host), host
    for prefix in narrowed.shell_ask:
        assert "deny" not in verdicts("shell", "run", shlex.join(prefix)), prefix
    if state == "zero":
        assert narrowed.network == [] and narrowed.shell_allowlist == []


@pytest.mark.parametrize(
    ("source", "can_confirm", "asked"),
    [("web", None, True), ("telegram", True, True), ("telegram", False, False)],
)
def test_a_chat_dispatch_defers_only_where_a_human_can_answer(
    tmp_path: Path, repo: Path, source: str, can_confirm: bool | None, asked: bool
) -> None:
    """A builder chat's dispatch_run: the web and a confirming channel card
    the ask; a channel that cannot confirm drops it (I6: headless = deny)."""
    config = _upgraded(tmp_path, repo)
    store = RunStore(config.db_path)
    runner = _Runner()
    try:
        if can_confirm is not None:
            store.upsert_channel_config(
                ChannelConfig(channel=source, enabled=True, channel_can_confirm=can_confirm)
            )
        chat = store.create_chat(title="c", model=None, source=source, role="builder")
        execute_mutation(
            "dispatch_run",
            {"repo": str(repo), "instructions": "fix hello.py"},
            store=store,
            holder=ConfigHolder(config, store),
            runner=runner,  # type: ignore[arg-type]
            actor=CHAT_ACTOR,
            chat_id=chat.chat_id,
        )
    finally:
        store.close()
    perms = runner.submitted["permissions"]
    assert perms.network_ask == (["example.com"] if asked else [])
    assert "example.com" not in perms.network


# --- the worker: an ask cards, naming the chain -----------------------------

CHAIN = {"chain": ["queen@builder"], "role": None}


def _registry(tmp_path: Path, **kw: Any) -> CapabilityRegistry:
    return CapabilityRegistry(
        tmp_path,
        emit=lambda _t, _p: None,
        network_allowlist=("pypi.org",),
        provider_hosts=("ollama.com:443",),
        delegation=CHAIN,
        **kw,
    )


def test_the_worker_cards_an_asked_host_naming_the_chain(tmp_path: Path) -> None:
    """Not a flat deny for a host missing from a non-empty list: the same
    require_approval gate network_allowlist_missing uses."""
    registry = _registry(tmp_path, network_ask=("example.com",))
    decision = registry.decision_for("network.fetch", {"url": "https://example.com/x"})
    assert (decision.verdict, decision.reason, decision.detail) == (
        "require_approval",
        DELEGATED_ASK_REASON,
        "example.com",
    )
    with pytest.raises(CapabilityApprovalRequired) as raised:
        registry.invoke("network.fetch", {"url": "https://example.com/x"})
    assert raised.value.reason == (
        "worker asks network.connect example.com — delegated by queen@builder, "
        "which would itself ask; approve once for this run, or deny"
    )
    # a host nobody asked about is still a flat deny
    assert registry.decision_for("network.fetch", {"url": "https://other.org"}).verdict == "deny"


def test_an_approved_ask_reaches_only_that_host(tmp_path: Path) -> None:
    registry = _registry(
        tmp_path, network_ask=("example.com",), approved_network_hosts=("example.com",)
    )
    assert registry.decision_for("network.fetch", {"url": "https://example.com"}).allows_execution()
    assert registry.decision_for("network.fetch", {"url": "https://other.org"}).verdict == "deny"


def test_the_worker_cards_an_asked_prefix_and_the_floor_still_denies(tmp_path: Path) -> None:
    registry = _registry(tmp_path, shell_ask=(["make"],), shell_allowlist=(["git", "status"],))
    decision = registry.decision_for("shell.run", {"argv": ["make", "build"]})
    assert (decision.verdict, decision.reason, decision.detail) == (
        "require_approval",
        DELEGATED_ASK_REASON,
        "make build",
    )
    with pytest.raises(CapabilityApprovalRequired) as raised:
        registry.invoke("shell.run", {"argv": ["make", "build"]})
    assert "worker asks shell.run make build — delegated by queen@builder" in raised.value.reason
    # an ask never lifts a deny: the git floor still refuses
    registry = _registry(tmp_path, shell_ask=(["git"],))
    assert registry.decision_for("shell.run", {"argv": ["git", "push"]}).verdict == "deny"
    # an ordinary unlisted command keeps its ordinary gate
    assert registry.decision_for("shell.run", {"argv": ["ls"]}).reason != DELEGATED_ASK_REASON


def test_the_proxy_admits_an_approved_ask_and_nothing_else(tmp_path: Path) -> None:
    """In the sandbox the filtering proxy is the wall: an approved asked host
    joins the run's egress; an approved host nobody asked about does not."""

    def task(ask: list[str], host: str) -> CodingWorkerTask:
        return CodingWorkerTask.model_validate(
            {
                "contract_version": "0.3.7",
                "task_id": "t",
                "trace_id": "tr",
                "worker_kind": "coding",
                "workspace": str(tmp_path),
                "instructions": "x",
                "permissions": _perms(network=["pypi.org"], network_ask=ask).model_dump(),
                "budget": {
                    "wall_clock_seconds": 60,
                    "max_iterations": 1,
                    "max_actions": 1,
                    "max_provider_calls": 0,
                },
                "approval_verdict": {
                    "approved": True,
                    "actor": "op",
                    "ts": "2026-11-12T00:00:00Z",
                    "action": "network.fetch",
                    "decision": {"verdict": "require_approval", "reason": "r", "detail": host},
                },
            }
        )

    assert effective_network_domains(task(["example.com"], "example.com")) == (
        "pypi.org",
        "example.com",
    )
    assert effective_network_domains(task([], "example.com")) == ("pypi.org",)


# --- the approval: this run only ------------------------------------------


def _gated_delegated_run(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[SupervisorConfig, str]:
    """A queen@builder run on an upgraded home whose project grants `make`
    and example.com — both asks of the chain — stopped at a `make check`
    gate (the fake worker crashes once, then its resume gates)."""
    config = _upgraded(tmp_path, repo)
    store = RunStore(config.db_path)
    try:
        project = store.project_for_binding("repo_path", str(repo))
        assert project is not None
        store.add_project_policy(
            project_id="app",
            name="app",
            strategy="trusted_local_dev",
            phase="build",
            policy={**project.policy, "allowed_shell_commands": [["make"]]},
        )
    finally:
        store.close()
    worker = shlex.join([sys.executable, str(FAKE_WORKER)])
    run = ["--home", str(tmp_path), "run", str(repo), "Go. MODE:crashgate", "--quiet"]
    run += ["--execution-mode", "workspace", "--worker-cmd", worker]
    assert main([*run, "--delegated-by", "queen@builder"]) != 0
    capsys.readouterr()
    config = replace(config, worker_command=(sys.executable, str(FAKE_WORKER)))
    store = RunStore(config.db_path)
    runner = Dispatcher(ConfigHolder(config, store), store)
    try:
        (crashed,) = store.recent_runs(1)
        held = CodingWorkerTask.model_validate_json(
            (config.audit_dir / crashed.task_id / "task.json").read_text()
        ).permissions
        assert held.shell_ask == [["make"]] and held.network_ask == ["example.com"]
        successor = resume_crashed_run(store, config, runner, crashed.task_id, "tester")
        gated = wait_run(store, successor["resumed_as"])
        assert gated.state == "pending_approval"
    finally:
        runner.shutdown(wait=True)
        store.close()
    return config, gated.task_id


def test_an_approved_ask_holds_for_that_run_and_no_other(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Bound to (task, capability, value): approve resumes THAT run with the
    grant in its verdict; nothing reaches the session tier (B39), and a
    second run of the same repo starts with no grant at all."""
    config, task_id = _gated_delegated_run(tmp_path, repo, capsys)
    store = RunStore(config.db_path)
    holder = ConfigHolder(config, store)
    runner = Dispatcher(holder, store)
    try:
        review_id = store.pending_approvals()[0].review_id
        run = require_run(store, task_id)
        resumed_id = resume_past_gate(store, config, runner, run, review_id, "tester")
        assert wait_run(store, resumed_id).state == "completed"
        resumed = CodingWorkerTask.model_validate_json(
            (config.audit_dir / resumed_id / "task.json").read_text()
        )
        assert resumed.approval_verdict is not None
        assert resumed.approval_verdict.commands == [["make", "check"]]
        assert resumed.permissions.shell_ask == [["make"]]  # still an ask, not an allow
        assert store.get_setting(SESSION_ALLOWED_SHELL_COMMANDS) in (None, [])
        second = submit_run(
            holder,
            runner,
            store,
            repo=str(repo),
            instructions="again",
            delegation_chain=["queen@builder"],
            deferred_asks=True,
        )
        wait_run(store, second)
        fresh = CodingWorkerTask.model_validate_json(
            (config.audit_dir / second / "task.json").read_text()
        )
        assert fresh.approval_verdict is None and fresh.worker_state is None
        assert fresh.permissions.shell_ask == [["make"]]
        assert ["make"] not in fresh.permissions.shell_allowlist
    finally:
        runner.shutdown(wait=True)
        store.close()


def test_an_ask_cannot_be_remembered_and_deny_keeps_it_refused(
    tmp_path: Path,
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Allow & remember (allow-command, allow-host) is refused for a deferred
    ask — approve-once is the only keep; deny resolves the gate and the run
    stays refused."""
    config, task_id = _gated_delegated_run(tmp_path, repo, capsys)
    store = RunStore(config.db_path)
    holder = ConfigHolder(config, store)
    runner = Dispatcher(holder, store)
    try:
        run = require_run(store, task_id)
        approval = pending_approval_or_409(store, store.pending_approvals()[0].review_id)
        with pytest.raises(HTTPException) as refused:
            allow_shell_command_and_resume(
                store, holder, runner, run, approval, str(approval["review_id"]), "tester"
            )
        assert refused.value.status_code == 409
        assert "delegated ask" in str(refused.value.detail)
        project = store.project_for_binding("repo_path", str(repo))
        assert project is not None
        assert project.policy["allowed_shell_commands"] == [["make"]]  # nothing persisted
        monkeypatch.setattr(
            actions,
            "approval_decision_for_action",
            lambda **_: AutonomyDecision(
                verdict="require_approval", reason=DELEGATED_ASK_REASON, detail="example.com"
            ),
        )
        with pytest.raises(HTTPException) as refused:
            allow_network_host_and_resume(
                store,
                holder,
                runner,
                run,
                {**approval, "action": "network.fetch"},
                str(approval["review_id"]),
                "tester",
            )
        assert refused.value.status_code == 409
        assert project.policy["default_network"] == ["pypi.org", "example.com"]
        store.resolve_approval(str(approval["review_id"]), approved=False, actor="tester")
        assert store.pending_approvals() == []
        assert require_run(store, task_id)["state"] != "completed"
    finally:
        runner.shutdown(wait=True)
        store.close()


# --- B22: the proposal consults the chain ----------------------------------


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("dispatch_run", {"instructions": "fix hello.py"}),
        ("dispatch_run", {"instructions": "fix", "execution_mode": "sandbox"}),
        ("dispatch_run", {"instructions": "fix", "network": ["pypi.org"]}),
        ("quick_edit", {"file": "hello.py", "instruction": "fix"}),
        ("batch_dispatch", {"tasks": [{"instructions": "fix"}]}),
    ],
)
def test_an_assistant_chat_is_refused_at_proposal_time(
    locked: SupervisorConfig, repo: Path, tool: str, args: dict[str, Any]
) -> None:
    """f1-16: the assistant role denies workspace writes, so a worker it
    dispatches could never write — deny now (no card), saying why, instead
    of a card whose confirm 403s."""
    if tool == "batch_dispatch":
        args = {"tasks": [{**args["tasks"][0], "repo": str(repo)}]}
    else:
        args = {**args, "repo": str(repo)}
    store = RunStore(locked.db_path)
    try:
        chat = store.create_chat(title="c", model=None, role="assistant")
        decision = mutation_execution_decision(
            tool, args, store=store, holder=ConfigHolder(locked, store), chat_id=chat.chat_id
        )
        builder = store.create_chat(title="b", model=None, role="builder")
        allowed = mutation_execution_decision(
            tool, args, store=store, holder=ConfigHolder(locked, store), chat_id=builder.chat_id
        )
    finally:
        store.close()
    assert decision is not None
    assert (decision.verdict, decision.reason) == ("deny", "dispatch.deny.exceeds_delegator")
    assert "delegated by queen@assistant: filesystem.write workspace exceeds the delegator" in (
        decision.detail or ""
    )
    assert allowed is None or allowed.verdict != "deny"


# --- headless faces: no deferred ask exists to hang ------------------------


def test_a_scheduled_tick_dispatches_no_deferred_ask(tmp_path: Path, repo: Path) -> None:
    """A task schedule carries no chain (nobody is watching), so nothing is
    narrowed into an ask and no card can wait on a human; run_task would
    clear one anyway (ES-F10)."""
    config = _upgraded(tmp_path, repo)
    seen: list[dict[str, Any]] = []

    def capture(*_args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs)
        raise RuntimeError("captured")

    store = RunStore(config.db_path)
    try:
        project = store.project_for_binding("repo_path", str(repo))
        assert project is not None
        store.add_project_policy(
            project_id="app",
            name="app",
            strategy="trusted_local_dev",
            phase="build",
            policy={
                **project.policy,
                "auto_dispatch_allowed": True,
                "default_execution_mode": "workspace",
            },
        )
        store.add_schedule(
            make_schedule(
                name="nightly",
                repo=str(repo),
                instructions="tidy",
                interval_seconds=60,
                start_at="2026-06-11T00:00:00Z",
            )
        )
        run_due(store=store, config=config, now="2026-06-11T09:00:00Z", dispatch=capture)
    finally:
        store.close()
    (kwargs,) = seen
    perms = kwargs["permissions"]
    assert perms.network_ask == [] and perms.shell_ask == []
    assert getattr(kwargs["dispatch_decision"], "delegation", None) is None


def test_no_chat_or_a_channel_that_cannot_confirm_gets_no_deferred_ask(
    tmp_path: Path, repo: Path
) -> None:
    """The headless chat faces (I6). A completion turn is unattended and
    cards every mutation (ES-F3, B08 — covered there), so its dispatch only
    happens on a human's confirm; a messenger chat whose channel cannot
    confirm, or a dispatch with no chat, drops the ask instead."""
    config = _upgraded(tmp_path, repo)
    store = RunStore(config.db_path)
    try:
        store.upsert_channel_config(ChannelConfig(channel="slack", channel_can_confirm=False))
        chat = store.create_chat(title="c", model=None, source="slack", role="builder")
        assert _human_answers(store, chat.chat_id) is False
        assert _human_answers(store, None) is False
    finally:
        store.close()


@pytest.mark.parametrize(
    ("delegation", "who"),
    [
        ({"chain": [], "role": "builder"}, "worker@builder asks shell.run make — its role builder"),
        (None, "worker asks shell.run make — its delegation would ask"),
    ],
)
def test_the_card_names_a_role_only_dispatch_truthfully(
    tmp_path: Path, delegation: dict[str, Any] | None, who: str
) -> None:
    registry = CapabilityRegistry(tmp_path, emit=lambda _t, _p: None, delegation=delegation)
    assert registry.delegated_ask_text("shell.run", "make").startswith(who)
