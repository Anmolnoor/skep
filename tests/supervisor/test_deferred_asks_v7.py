"""ADR 0059 D3 (V7): a grant the delegation chain makes an ask is a deferred
approval, not a drop. ``Permissions.network_ask`` / ``shell_ask`` carry it;
the worker cards it naming the chain; the approval holds for that run only;
a headless face drops it (deny); a deny never becomes an ask (B12 residue,
B22 card half, B147)."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.autonomy import AutonomyDecision
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.dispatch import run_task
from skep.supervisor.policy_schema import SCOPES
from skep.supervisor.roles import parse_delegation_chain, resolve_role, role_covers
from skep.supervisor.serve.actions import _within_delegation, policy_decide, submit_run
from skep.supervisor.serve.channels import ChannelConfig
from skep.supervisor.serve.chat import CHAT_ACTOR
from skep.supervisor.serve.settings import FRESH_HOME_SEEDED_AT, ConfigHolder
from skep.supervisor.serve.tools import execute_mutation
from skep.worker_contract import CodingWorkerTask, Permissions

from .conftest import predating_home
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
