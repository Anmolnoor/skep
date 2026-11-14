"""Unlocked mode on policy v3 (ADR 0061): the per-project zero-ask rung.

The key exists on every operator face, is project-only and never groupable
(I6), and refuses to save without its preconditions — a pinned
verify_command (I2), a usable sandbox (I12) and a baseline that binds
nothing (on a zero-state home the layer could answer nothing, I8).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from skep.cli import main
from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.projects import validate_policy_group, validate_project_policy
from skep.supervisor.sandbox import SandboxAvailability

from .conftest import serve_client


def _sandbox(usable: bool) -> Any:
    def probe(backend: str | None = None) -> SandboxAvailability:
        if usable:
            return SandboxAvailability(usable=True, backend="test")
        return SandboxAvailability(usable=False, reason="probe_failed", detail="forced off by test")

    return probe


def lock_baseline(home: Path, name: str = "builder") -> None:
    """An approved baseline, so the zero-state guard binds nothing."""
    from skep.supervisor import policy_baseline as pb
    from skep.supervisor.policy_templates import load_policy_template

    layer = load_policy_template(name).model_dump(mode="json")
    pb.write_lock(home, pb._load_layer(layer), approved_by="op")


@pytest.fixture
def unlockable(config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch) -> SupervisorConfig:
    import skep.supervisor.serve.registry as registry

    monkeypatch.setattr(registry, "sandbox_availability", _sandbox(True))
    config.home.mkdir(parents=True, exist_ok=True)
    lock_baseline(config.home)
    return config


def _setup(client: TestClient, repo: Path, overrides: dict[str, Any]) -> Any:
    return client.post(
        "/api/projects/setup",
        json={
            "project_id": "unlockable",
            "name": "Unlockable",
            "strategy": "trusted_local_dev",
            "repo_path": str(repo),
            "policy_overrides": overrides,
        },
    )


def test_unlocked_is_a_project_policy_bool() -> None:
    assert validate_project_policy({"unlocked": True})["unlocked"] is True
    assert validate_project_policy({"unlocked": False})["unlocked"] is False
    with pytest.raises(ValueError, match="unlocked must be a boolean"):
        validate_project_policy({"unlocked": "yes"})


def test_unlocked_is_on_the_trust_ramp_project_only() -> None:
    from skep.supervisor.policy_registry import TRUST_RAMP_KEYS, registry_lookup

    key = registry_lookup("unlocked")
    assert key is not None and key.layers == ("project",)
    assert "unlocked" in TRUST_RAMP_KEYS
    with pytest.raises(ValueError, match="cannot ride a policy group"):
        validate_policy_group("zero-ask", {"unlocked": True})


def test_setup_refuses_unlocked_without_a_pinned_verify_command(
    repo: Path, unlockable: SupervisorConfig
) -> None:
    refused = _setup(serve_client(unlockable), repo, {"unlocked": True, "verify_command": ""})
    assert refused.status_code == 400
    assert "verify_command" in refused.json()["detail"]


def test_setup_refuses_unlocked_without_a_usable_sandbox(
    repo: Path, unlockable: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import skep.supervisor.serve.registry as registry

    monkeypatch.setattr(registry, "sandbox_availability", _sandbox(False))
    refused = _setup(serve_client(unlockable), repo, {"unlocked": True, "verify_command": "true"})
    assert refused.status_code == 400
    assert "sandbox" in refused.json()["detail"]
    assert "forced off by test" in refused.json()["detail"]  # the evidence (I9)


def test_setup_refuses_unlocked_on_a_home_with_no_approved_baseline(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import skep.supervisor.serve.registry as registry

    monkeypatch.setattr(registry, "sandbox_availability", _sandbox(True))
    refused = _setup(serve_client(config), repo, {"unlocked": True, "verify_command": "true"})
    assert refused.status_code == 400
    assert "skep policy approve" in refused.json()["detail"]


def test_setup_saves_unlocked_and_turns_it_back_off(
    repo: Path, unlockable: SupervisorConfig
) -> None:
    client = serve_client(unlockable)
    saved = _setup(client, repo, {"unlocked": True, "verify_command": "true"})
    assert saved.status_code == 201, saved.text
    assert saved.json()["policy"]["unlocked"] is True
    preview = client.post(
        "/api/projects/preview",
        json={"project_id": "unlockable", "name": "Unlockable", "strategy": "trusted_local_dev",
              "repo_path": str(repo), "policy_overrides": {"unlocked": True}},
    )  # fmt: skip
    assert "unlocked" in preview.json()["dangerous_grant_warnings"]
    off = _setup(client, repo, {"unlocked": False})
    assert off.status_code == 201
    assert off.json()["policy"]["unlocked"] is False


def test_the_cli_switches_unlocked_on_and_off(
    repo: Path, unlockable: SupervisorConfig, tmp_path: Path
) -> None:
    from skep.supervisor.cli_cmds import build_config

    root = tmp_path / "cli-home"
    cli = build_config(root, None)
    cli.home.mkdir(parents=True)
    lock_baseline(cli.home)
    base = ["--home", str(root), "project", "setup", str(repo), "--strategy", "trusted_local_dev"]
    base += ["--project-id", "unlockable", "--verify-command", "true", "--no-seed-schedules"]
    assert main([*base, "--unlocked"]) == 0
    store = RunStore(cli.db_path)
    try:
        record = store.get_project_policy("unlockable")
        assert record is not None and record.policy["unlocked"] is True
        assert main([*base, "--no-unlocked"]) == 0
        record = store.get_project_policy("unlockable")
        assert record is not None and record.policy["unlocked"] is False
    finally:
        store.close()


def test_copy_project_policy_never_copies_unlocked(
    repo: Path, unlockable: SupervisorConfig, tmp_path: Path
) -> None:
    from skep.supervisor.serve.actions import copy_project_policy

    client = serve_client(unlockable)
    assert _setup(client, repo, {"unlocked": True, "verify_command": "true"}).status_code == 201
    other = tmp_path / "other"
    other.mkdir()
    (other / ".git").mkdir()
    made = client.post(
        "/api/projects/setup",
        json={"project_id": "other", "name": "Other", "strategy": "trusted_local_dev",
              "repo_path": str(other)},
    )  # fmt: skip
    assert made.status_code == 201, made.text
    store = RunStore(unlockable.db_path)
    try:
        copied = copy_project_policy(store, src="unlockable", dst="other")
        assert "unlocked" not in copied["copied_keys"]
        assert "unlocked" not in copied["project"]["policy"]
    finally:
        store.close()


def test_copy_project_policy_never_copies_codex_chatgpt_login(
    repo: Path, unlockable: SupervisorConfig, tmp_path: Path
) -> None:
    # Same shape as unlocked: turning it on is a HIGH card (it spends the
    # operator's ChatGPT plan and sends repo content to chatgpt.com), so a
    # copy must not carry it; loopback_ports still copies.
    from skep.supervisor.serve.actions import copy_project_policy

    client = serve_client(unlockable)
    policy = {"codex_chatgpt_login": True, "loopback_ports": [5432], "verify_command": "true"}
    assert _setup(client, repo, policy).status_code == 201
    other = tmp_path / "other"
    other.mkdir()
    (other / ".git").mkdir()
    made = client.post(
        "/api/projects/setup",
        json={"project_id": "other", "name": "Other", "strategy": "trusted_local_dev",
              "repo_path": str(other)},
    )  # fmt: skip
    assert made.status_code == 201, made.text
    store = RunStore(unlockable.db_path)
    try:
        copied = copy_project_policy(store, src="unlockable", dst="other")
        assert "codex_chatgpt_login" not in copied["copied_keys"]
        assert "codex_chatgpt_login" not in copied["project"]["policy"]
        assert copied["project"]["policy"]["loopback_ports"] == [5432]
    finally:
        store.close()


def test_turning_unlocked_on_from_chat_is_a_high_card_and_off_is_not() -> None:
    from skep.supervisor.serve.cards import high_tier

    for args in ({"unlocked": True}, {"policy_overrides": {"unlocked": True}}):
        why = high_tier("setup_project", {"project_id": "p", **args})
        assert why is not None and "unlocked" in why, args
    assert high_tier("setup_project", {"project_id": "p", "unlocked": False}) is None


# --- ES-F143: what unlocked implies, composed by the resolver -----------------


def _bound(config: SupervisorConfig, repo: Path, policy: dict[str, Any]) -> RunStore:
    store = RunStore(config.db_path)
    store.add_project_policy(
        project_id="unlockable",
        name="Unlockable",
        strategy="trusted_local_dev",
        phase="build",
        policy=policy,
    )
    store.add_project_binding(
        project_id="unlockable", binding_kind="repo_path", binding_value=str(repo.resolve())
    )
    return store


UNLOCKED = {"unlocked": True, "verify_command": "true"}


@pytest.mark.parametrize("v2", [False, True], ids=["engine-v1", "engine-v2"])
def test_unlocked_implies_auto_dispatch_sandbox_and_verified_landing(
    repo: Path, config: SupervisorConfig, v2: bool
) -> None:
    from skep.supervisor.policy_resolver import run_policy_for_repo
    from skep.supervisor.policy_schema import SCOPES
    from skep.supervisor.serve.settings import POLICY_ENGINE_V2_SCOPES

    store = _bound(config, repo, UNLOCKED)
    store.set_setting(POLICY_ENGINE_V2_SCOPES, list(SCOPES) if v2 else [])
    try:
        policy = run_policy_for_repo(store, config, repo.resolve())
        implied = {"auto_dispatch_allowed": True, "default_execution_mode": "sandbox",
                   "auto_apply_verified_patch": True}  # fmt: skip
        for key, value in implied.items():
            assert policy[key] == value, key
            assert policy["_provenance"][key] == "unlocked", key
    finally:
        store.close()


def test_an_unlocked_run_is_sandboxed_whatever_the_request_says(
    repo: Path, config: SupervisorConfig
) -> None:
    from skep.supervisor.policy_resolver import resolve_run_policy

    store = _bound(config, repo, UNLOCKED)
    try:
        resolved = resolve_run_policy(
            store=store, config=config, repo=repo.resolve(), caste="coding", network=None,
            env_allowlist=None, wall_clock_seconds=None, max_iterations=None,
            max_actions=None, max_provider_calls=None, execution_mode="workspace",
        )  # fmt: skip
        assert resolved.execution_mode == "sandbox"
    finally:
        store.close()


def test_an_unlocked_dispatch_runs_in_turn_and_says_why(
    repo: Path, config: SupervisorConfig
) -> None:
    from skep.supervisor.serve.actions import dispatch_run_decision
    from skep.supervisor.serve.settings import ConfigHolder

    store = _bound(config, repo, UNLOCKED)
    try:
        holder = ConfigHolder(config, store)
        decision = dispatch_run_decision(holder, store, repo=str(repo))
        assert (decision.verdict, decision.reason) == ("allow", "dispatch.auto_allowed.unlocked")
        # an explicit run override still cards (v95-F3, I6/I7)
        explicit = dispatch_run_decision(holder, store, repo=str(repo), engine="builtin")
        assert explicit.verdict == "require_approval"
    finally:
        store.close()


def test_a_phase_change_drops_unlocked(repo: Path, config: SupervisorConfig) -> None:
    from skep.supervisor.serve.registry import set_project_phase

    store = _bound(config, repo, UNLOCKED)
    try:
        moved = set_project_phase(store, "unlockable", "maintain")
        assert "unlocked" not in moved["policy"]
        assert moved["policy"]["verify_command"] == "true"
    finally:
        store.close()


# --- ES-F144: the unlocked step inside decide() --------------------------------


def _unlocked_stack(tmp_path: Path, kind: str, *, unlocked: bool = True) -> Any:
    """A grid home (``policy_v3_grid.build_home``) whose project ``app`` is
    unlocked, with a named ask and a deny added to its baseline."""
    from skep.supervisor import policy_baseline as pb
    from skep.supervisor.cli_cmds import build_config
    from skep.supervisor.policy_templates import load_policy_template
    from skep.supervisor.policy_v3 import load_stack
    from skep.supervisor.serve.settings import ConfigHolder

    from .policy_v3_grid import build_home

    home, repo = build_home(tmp_path, kind)
    config = build_config(home, None)
    if kind == "builder":
        layer = load_policy_template("builder").model_dump(mode="json")
        layer["rules"] = [
            *layer["rules"],
            {"scope": "shell", "verdict": "require_approval", "action": "run",
             "pattern": "make deploy", "rule_id": "ask-make-deploy"},
            {"scope": "shell", "verdict": "deny", "action": "run", "pattern": "curl",
             "rule_id": "no-curl"},
            {"scope": "network", "verdict": "deny", "action": "connect",
             "pattern": "evil.example", "rule_id": "no-evil"},
        ]  # fmt: skip
        pb.write_lock(config.home, pb._load_layer(layer), approved_by="op")
    store = RunStore(config.db_path)
    store.add_project_policy(project_id="app", name="app", strategy="trusted_local_dev",
                             phase="build", policy={**UNLOCKED, "unlocked": unlocked})  # fmt: skip
    try:
        return load_stack(ConfigHolder(config, store), store, repo=str(repo)), config
    finally:
        store.close()


UNLOCKED_CELLS = [
    # (scope, action, value, verdict, decided_by) for a sandboxed request
    ("shell", "run", "rm -rf build", "allow", "unlocked:app/zero-ask"),
    ("shell", "run", "pytest -q && rm -rf build", "allow", "unlocked:app/zero-ask"),
    ("network", "connect", "files.example", "allow", "unlocked:app/zero-ask"),
    ("shell", "run", "pytest -q", "allow", "template:builder/shell:pytest"),
    ("shell", "run", "git push origin main", "deny", "floor/floor.worker-git-hard-denies"),
    ("shell", "run", "git checkout other", "deny", "floor/floor.worker-git-hard-denies"),
    ("shell", "run", "cat {home}/.ssh/id_rsa", "deny", "floor/floor.secret-reads"),
    ("shell", "run", "sudo ls", "deny", "floor/floor.never-grantable-prefixes"),
    # the baseline's deny and named ask (operator lane) bind: a human answers
    ("shell", "run", "curl https://x.example", "require_approval", "default/unmatched"),
    ("network", "connect", "evil.example", "require_approval", "default/unmatched"),
    ("shell", "run", "make deploy", "require_approval", "default/unmatched"),
    ("shell", "run", "rm -rf build && make deploy", "require_approval", "default/unmatched"),
    # the auto-answer grants an argv PREFIX: `make` would cover `make deploy`,
    # `env` / `timeout 5` would launch `curl` — each a line the baseline names
    ("shell", "run", "make", "require_approval", "default/unmatched"),
    ("shell", "run", "env", "require_approval", "default/unmatched"),
    ("shell", "run", "timeout 5", "require_approval", "default/unmatched"),
    ("shell", "run", "make lint", "allow", "unlocked:app/zero-ask"),
    ("shell", "run", "echo $(cat x)", "require_approval", "default/unmatched"),
    ("shell", "run", "sleep 9 &", "require_approval", "default/unmatched"),
    ("filesystem", "write", "/etc/x", None, None),  # not the layer's: as before
]  # fmt: skip


@pytest.mark.parametrize(("scope", "action", "value", "verdict", "decided_by"), UNLOCKED_CELLS)
def test_decide_answers_only_the_default_asks_of_a_sandboxed_worker(
    tmp_path: Path, scope: str, action: str, value: str, verdict: str | None,
    decided_by: str | None,
) -> None:  # fmt: skip
    import os

    from skep.supervisor.policy_v3 import Request, decide

    stack, _config = _unlocked_stack(tmp_path, "builder")
    req = Request(scope, action, value.format(home=os.environ["HOME"]))
    before = decide(req, stack=stack)  # no sandboxed fact: exactly as today
    got = decide(req, stack=stack, sandboxed=True)
    if verdict is None:
        assert got == before
        return
    assert (got.verdict, got.decided_by) == (verdict, decided_by)
    if got.layer.startswith("unlocked:"):
        assert (before.verdict, before.layer) == ("require_approval", "default")
    else:
        assert got == before


def test_the_unlocked_step_holds_the_role_the_chain_and_the_baseline_guard(
    tmp_path: Path,
) -> None:
    from skep.supervisor.policy_v3 import Request, decide
    from skep.supervisor.roles import SHIPPED_ROLES, parse_delegation_chain

    req = Request("shell", "run", "rm -rf build")
    stack, config = _unlocked_stack(tmp_path / "b", "builder")
    # a delegated run: the builder role would itself ask, so a human answers
    chain = parse_delegation_chain(["queen@builder"], config.home)
    held = decide(req, stack=stack, chain=chain, sandboxed=True)
    assert (held.verdict, held.deferred) == ("require_approval", True)
    # a worker role's catch-all ask binds (only a human grant answers it, P3-2)
    roled = decide(req, stack=stack, role=SHIPPED_ROLES["builder"], sandboxed=True)
    assert roled.verdict == "require_approval"
    # a zero-state home: the guard asks before any lane
    zero, _ = _unlocked_stack(tmp_path / "z", "zero")
    assert decide(req, stack=zero, sandboxed=True).decided_by == "baseline/no_baseline"
    # a project that is not unlocked: nothing changes
    locked, _ = _unlocked_stack(tmp_path / "l", "builder", unlocked=False)
    assert decide(req, stack=locked, sandboxed=True) == decide(req, stack=locked)
    # no repo (the Queen's own lanes): the step never applies
    from dataclasses import replace

    bare = replace(stack, run=None, unlocked=None)
    assert decide(req, stack=bare, sandboxed=True) == decide(req, stack=bare)


# --- ES-F145: a worker gate answers itself through decide() --------------------

_SANDBOX = pytest.mark.skipif(
    not __import__("skep.supervisor.sandbox", fromlist=["availability"]).availability().usable,
    reason="the auto-answer applies only to a sandboxed run",
)


def _gated_run(
    config: SupervisorConfig,
    repo: Path,
    gate: str,
    *,
    mode: str = "shellgate",
    unlocked: bool = True,
    execution_mode: str = "sandbox",
) -> tuple[Any, str]:
    """A serve app, an (un)locked project on ``repo`` and one run that stops
    on ``gate``; returns the client and the first run's terminal record."""
    from .conftest import wait_terminal

    config.home.mkdir(parents=True, exist_ok=True)
    lock_baseline(config.home)
    _bound(config, repo, {**UNLOCKED, "unlocked": unlocked}).close()
    client = serve_client(config)
    sent = client.post("/api/runs", json={
        "repo": str(repo), "instructions": f"Fix it. MODE:{mode} GATE:{gate}",
        "execution_mode": execution_mode,
    })  # fmt: skip
    assert sent.status_code == 202, sent.text
    return client, wait_terminal(client, sent.json()["task_id"])["task_id"]


def _decisions(config: SupervisorConfig) -> list[dict[str, Any]]:
    import json

    path = config.audit_dir / "policy" / "decisions.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return [row for row in rows if row.get("actor") == "auto:unlocked"]


def _settled(config: SupervisorConfig, task_id: str, *, resolved_only: bool = False) -> None:
    """The done-callback runs after the run reads terminal: wait until it has
    resolved the gate (or, unless ``resolved_only``, logged its decision)."""
    import time

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        store = RunStore(config.db_path)
        try:
            resolved = any(a.status != "pending" for a in store.approvals_for(task_id))
        finally:
            store.close()
        if resolved or (_decisions(config) and not resolved_only):
            return
        time.sleep(0.05)
    raise AssertionError("the unlocked auto-answer never ran")


def _landed(config: SupervisorConfig, task_id: str) -> tuple[str | None, str | None]:
    import time

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        store = RunStore(config.db_path)
        try:
            landed = [a for a in store.approvals_for(task_id) if a.action == "apply_patch"]
        finally:
            store.close()
        if landed and landed[0].status != "pending":
            return landed[0].resolved_by, landed[0].landing_branch
        time.sleep(0.05)
    raise AssertionError(f"run {task_id} never landed")


def _final(client: TestClient, config: SupervisorConfig, task_id: str) -> dict[str, Any]:
    """The last run of a resume chain, once terminal (``superseded`` follows
    the successor; a gate just resolved is superseded a moment later)."""
    import time

    from skep.supervisor.serve.app import TERMINAL_STATES

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        run: dict[str, Any] = client.get(f"/api/runs/{task_id}").json()["run"]
        store = RunStore(config.db_path)
        try:
            successor = store.resumed_as_for(task_id)
        finally:
            store.close()
        if successor is not None:
            task_id = successor
        elif run["state"] in TERMINAL_STATES and run["state"] != "pending_approval":
            return run
        time.sleep(0.05)
    raise AssertionError(f"run {task_id} never finished")


@_SANDBOX
@pytest.mark.parametrize(("mode", "gate"), [("shellgate", "make_lint"),
                                             ("hostgate", "files.example")])  # fmt: skip
def test_a_gate_on_an_unlocked_project_answers_itself_once(
    repo: Path, config: SupervisorConfig, mode: str, gate: str
) -> None:
    client, first = _gated_run(config, repo, gate, mode=mode)
    _settled(config, first, resolved_only=True)
    run = _final(client, config, first)
    assert run["state"] == "completed", run
    # zero clicks: G10 re-runs the pinned verify, then the patch lands on skep/*
    assert _landed(config, run["task_id"]) == ("auto:verified-patch", f"skep/{run['task_id']}")
    store = RunStore(config.db_path)
    try:
        resolved = store.approvals_for(first)
        assert [(a.status, a.resolved_by) for a in resolved] == [("approved", "auto:unlocked")]
        # once: no session grant was written for the command (B39) — `make
        # lint`, not a guarded class a human's approve never remembers either
        from skep.supervisor.policy_schema import POLICY_DOCUMENT_SETTINGS_KEY

        assert "make lint" not in str(store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY))
    finally:
        store.close()
    rows = _decisions(config)
    assert [r["decided_by"] for r in rows] == ["unlocked:unlockable/zero-ask"]
    assert rows[0]["task_id"] == first  # D34: the row names the run it answered
    # D34: the run's receipt lists the hosts it reached under unlocked —
    # the gated run and its resumed successor alike
    hosts = [{"host": gate, "count": 1}] if mode == "hostgate" else []
    for task_id in (first, run["task_id"]):
        assert client.get(f"/api/runs/{task_id}").json().get("unlocked_hosts", []) == hosts


@_SANDBOX
@pytest.mark.parametrize("gate", ["git_push_origin_main", "echo_$(cat_x)"])
def test_a_floor_or_unreadable_gate_stays_a_human_card(
    repo: Path, config: SupervisorConfig, gate: str
) -> None:
    client, first = _gated_run(config, repo, gate)
    _settled(config, first)
    assert client.get(f"/api/runs/{first}").json()["run"]["state"] == "pending_approval"
    assert [r["verdict"] for r in _decisions(config)] in (["deny"], ["require_approval"])


def _callback_ran(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Set once the done-callback reaches the terminal notice — which it
    does only after the auto-answer declined. A state read right after the
    run turns terminal races that callback, and passes whatever it does."""
    import threading

    from skep.supervisor.serve.run_status import notify_run_terminal

    ran = threading.Event()

    def notify(*args: Any, **kwargs: Any) -> None:
        ran.set()
        notify_run_terminal(*args, **kwargs)

    monkeypatch.setattr("skep.supervisor.serve.app.notify_run_terminal", notify)
    return ran


def test_a_gate_stays_a_card_when_the_project_is_not_unlocked(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran = _callback_ran(monkeypatch)
    client, first = _gated_run(config, repo, "rm_-rf_build", execution_mode="workspace",
                               unlocked=False)  # fmt: skip
    assert ran.wait(20), "the done-callback never declined the gate"
    assert client.get(f"/api/runs/{first}").json()["run"]["state"] == "pending_approval"
    assert _decisions(config) == []


def test_a_gate_stays_a_card_when_the_home_runs_no_sandbox(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    ran = _callback_ran(monkeypatch)
    unsandboxed = replace(config, sandbox=False)
    client, first = _gated_run(unsandboxed, repo, "rm_-rf_build")
    assert ran.wait(20), "the done-callback never declined the gate"
    assert client.get(f"/api/runs/{first}").json()["run"]["state"] == "pending_approval"
    assert _decisions(unsandboxed) == []


# --- ES-F146: the mode is visible -----------------------------------------------


def test_doctor_names_every_unlocked_project_and_the_way_out(repo: Path, tmp_path: Path) -> None:
    from skep.status import _unlocked_advisories, build_status
    from skep.supervisor.cli_cmds import build_config

    root = tmp_path / "doctor-home"
    config = build_config(root, None)
    config.home.mkdir(parents=True)
    assert _unlocked_advisories(root) == []
    _bound(config, repo, UNLOCKED).close()
    lines = _unlocked_advisories(root)
    assert len(lines) == 1
    assert "unlockable" in lines[0] and "--no-unlocked" in lines[0]
    assert lines[0] in build_status(root, provider_timeout=0.01)["advisories"]
    # D34: the hosts the mode auto-approved in the window, with counts
    from datetime import UTC, datetime, timedelta

    from skep.supervisor.policy_v3 import log_decision

    def row(host: str, at: datetime, layer: str = "unlocked:unlockable") -> dict[str, Any]:
        return {"at": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "actor": "auto:unlocked",
                "scope": "network", "action": "connect", "value": host, "verdict": "allow",
                "layer": layer, "decided_by": f"{layer}/zero-ask", "task_id": "t1"}  # fmt: skip

    now = datetime.now(UTC)
    for each in (row("files.example", now), row("files.example", now), row("pypi.org", now),
                 row("old.example", now - timedelta(days=30)),
                 row("named.example", now, layer="run")):  # fmt: skip
        log_decision(config.audit_dir, each)
    lines = _unlocked_advisories(root)
    assert len(lines) == 2
    assert lines[1] == (
        "unlocked project unlockable auto-approved 2 host(s) no rule names in the last "
        "7 days: files.example (2), pypi.org (1) — each is a decision-log row "
        "(`skep policy decisions --actor auto:unlocked`)"
    )


def test_the_chat_face_sets_unlocked_through_the_same_write(
    repo: Path, unlockable: SupervisorConfig
) -> None:
    from skep.supervisor.serve.settings import ConfigHolder
    from skep.supervisor.serve.tools import _execute_mutation

    store = RunStore(unlockable.db_path)
    try:
        args = {"project_id": "unlockable", "name": "Unlockable", "strategy": "trusted_local_dev",
                "repo_path": str(repo), "policy_overrides": {"verify_command": "true"},
                "unlocked": True, "seed_default_schedules": False}  # fmt: skip
        holder = ConfigHolder(unlockable, store)
        saved = _execute_mutation("setup_project", args, store=store, holder=holder,
                                  runner=None, actor="chat-user")  # type: ignore[arg-type]  # fmt: skip
        assert saved["policy"]["unlocked"] is True
    finally:
        store.close()


@pytest.mark.parametrize("value", ["false", "true", 1])
def test_the_chat_face_refuses_an_unlocked_that_is_not_a_boolean(
    repo: Path, unlockable: SupervisorConfig, value: Any
) -> None:
    """``"false"`` is truthy: coerced, it turned the mode ON for a project
    whose verify is already pinned, behind an ordinary card (the HIGH tier
    reads ``is True``)."""
    from skep.supervisor.serve.cards import high_tier
    from skep.supervisor.serve.settings import ConfigHolder
    from skep.supervisor.serve.tools import _execute_mutation

    assert _setup(serve_client(unlockable), repo, {"verify_command": "true"}).status_code == 201
    args = {"project_id": "unlockable", "name": "Unlockable", "strategy": "trusted_local_dev",
            "repo_path": str(repo), "unlocked": value, "seed_default_schedules": False}  # fmt: skip
    assert high_tier("setup_project", args) is None  # so it must never save as on
    store = RunStore(unlockable.db_path)
    try:
        holder = ConfigHolder(unlockable, store)
        with pytest.raises(Exception, match="unlocked must be a boolean"):
            _execute_mutation("setup_project", args, store=store, holder=holder,
                              runner=None, actor="chat-user")  # type: ignore[arg-type]  # fmt: skip
        record = store.get_project_policy("unlockable")
        assert record is not None and record.policy.get("unlocked") is not True
    finally:
        store.close()
