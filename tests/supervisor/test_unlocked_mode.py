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

# v127-F5: a pin that is not known-vacuous — `true` no longer unlocks a project.
PIN = "grep -rq --include=*.py value ."


def _sandbox(usable: bool) -> Any:
    def probe(backend: str | None = None) -> SandboxAvailability:
        if usable:
            return SandboxAvailability(usable=True, backend="test")
        return SandboxAvailability(usable=False, reason="probe_failed", detail="forced off by test")

    return probe


def lock_baseline(home: Path, name: str = "builder", *, shell_rules: bool = True) -> None:
    """An approved baseline, so the zero-state guard binds nothing.
    ``shell_rules=False`` drops every shell rule: zero operator shell rules,
    so only the floor stands between a gate and the unlocked step."""
    from skep.supervisor import policy_baseline as pb
    from skep.supervisor.policy_templates import load_policy_template

    layer = load_policy_template(name).model_dump(mode="json")
    if not shell_rules:
        layer["rules"] = [rule for rule in layer["rules"] if rule["scope"] != "shell"]
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
    refused = _setup(serve_client(unlockable), repo, {"unlocked": True, "verify_command": PIN})
    assert refused.status_code == 400
    assert "sandbox" in refused.json()["detail"]
    assert "forced off by test" in refused.json()["detail"]  # the evidence (I9)


def test_setup_refuses_unlocked_on_a_home_with_no_approved_baseline(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import skep.supervisor.serve.registry as registry

    monkeypatch.setattr(registry, "sandbox_availability", _sandbox(True))
    refused = _setup(serve_client(config), repo, {"unlocked": True, "verify_command": PIN})
    assert refused.status_code == 400
    assert "skep policy approve" in refused.json()["detail"]


def test_setup_saves_unlocked_and_turns_it_back_off(
    repo: Path, unlockable: SupervisorConfig
) -> None:
    client = serve_client(unlockable)
    saved = _setup(client, repo, {"unlocked": True, "verify_command": PIN})
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
    base += ["--project-id", "unlockable", "--verify-command", PIN, "--no-seed-schedules"]
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
    assert _setup(client, repo, {"unlocked": True, "verify_command": PIN}).status_code == 201
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
    policy = {"codex_chatgpt_login": True, "loopback_ports": [5432], "verify_command": PIN}
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
        assert "hosts no rule names stay refused or asked" in why  # D42 (I8)
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


UNLOCKED = {"unlocked": True, "verify_command": PIN}


def _own_project(repo: Path) -> Any:
    from skep.worker_contract.task import ProjectContextPayload

    return ProjectContextPayload(
        project_id="unlockable", name="Unlockable", strategy="trusted_local_dev",
        phase="build", binding_kind="repo_path", binding_value=str(repo.resolve()),
    )  # fmt: skip


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
        assert moved["policy"]["verify_command"] == PIN
    finally:
        store.close()


def test_a_phase_change_keeps_the_coding_engine(repo: Path, config: SupervisorConfig) -> None:
    """v132-F4 (I2, I8): phase defaults name no engine, so a move must not
    silently switch which agent runs the next task."""
    from skep.supervisor.serve.registry import set_project_phase

    store = _bound(
        config, repo,
        {"verify_command": PIN, "coding_engine": "codex", "loopback_ports": [5432],
         "unlocked": True, "auto_apply_branch": "skep/maintain"},
    )  # fmt: skip
    try:
        moved = set_project_phase(store, "unlockable", "build")
        assert moved["policy"]["coding_engine"] == "codex"
        assert moved["policy"]["verify_command"] == PIN
        # Neon N4: every operator key is carried; the phase's own keys are not
        assert moved["policy"]["loopback_ports"] == [5432]
        assert "unlocked" not in moved["policy"]
        assert moved["policy"].get("auto_apply_branch") != "skep/maintain"
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
    # D42: unlocked never widens the network — an unnamed host still asks
    ("network", "connect", "files.example", "require_approval", "default/unmatched"),
    ("network", "connect", "*", "require_approval", "default/unmatched"),
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
    # ... and a bare-flag VARIANT (v93-F1): an approved `make -k deploy` also
    # runs `make deploy`, which the baseline names (D46 review)
    ("shell", "run", "make -k deploy", "require_approval", "default/unmatched"),
    ("shell", "run", "make -k lint", "allow", "unlocked:app/zero-ask"),
    ("shell", "run", "/usr/bin/make -k deploy", "require_approval", "default/unmatched"),
    ("shell", "run", "env make -k deploy", "require_approval", "default/unmatched"),
    ("shell", "run", "timeout -s KILL 5 make -k deploy", "require_approval", "default/unmatched"),
    ("shell", "run", "timeout 60 make lint", "require_approval", "default/unmatched"),
    ("shell", "run", "bash scripts/lint.sh", "require_approval", "default/unmatched"),
    ("shell", "run", "dash scripts/lint.sh", "require_approval", "default/unmatched"),
    ("shell", "run", "ksh scripts/lint.sh", "require_approval", "default/unmatched"),
    ("shell", "run", "node tools/build.js", "require_approval", "default/unmatched"),
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
    # ADR 0061 open item 1, decided 2026-11-21: a role's catch-all ask is a
    # default, and the operator's unlock answers defaults — a run dispatched
    # from a builder chat (the chain) or in the builder role no longer cards
    chain = parse_delegation_chain(["queen@builder"], config.home)
    builder = SHIPPED_ROLES["builder"]
    for kwargs in ({"chain": chain}, {"role": builder}, {"role": builder, "chain": chain}):
        got = decide(req, stack=stack, sandboxed=True, **kwargs)
        assert (got.verdict, got.decided_by, got.deferred) == (
            "allow",
            "unlocked:app/zero-ask",
            False,
        )
        # not sandboxed (a CLI run, REST, the hook): the role still asks
        assert decide(req, stack=stack, **kwargs).verdict == "require_approval"
        # what the operator named still binds under a role and a chain
        named = decide(
            Request("shell", "run", "make deploy"), stack=stack, sandboxed=True, **kwargs
        )
        assert named.verdict == "require_approval"
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
    shell_rules: bool = True,
) -> tuple[Any, str]:
    """A serve app, an (un)locked project on ``repo`` and one run that stops
    on ``gate``; returns the client and the first run's terminal record."""
    from .conftest import wait_terminal

    config.home.mkdir(parents=True, exist_ok=True)
    lock_baseline(config.home, shell_rules=shell_rules)
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
@pytest.mark.parametrize(("mode", "gate"), [("shellgate", "make_lint")])
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
    # and the chat's get_run says the same (the Queen's face of the receipt)
    from skep.supervisor.serve.settings import ConfigHolder
    from skep.supervisor.serve.tools import execute_read_tool

    store = RunStore(config.db_path)
    try:
        detail = execute_read_tool(
            "get_run", {"task_id": first}, store=store, holder=ConfigHolder(config, store)
        )
    finally:
        store.close()
    assert detail.get("unlocked_hosts", []) == hosts


@_SANDBOX
@pytest.mark.parametrize(("mode", "gate"), [("shellgate", "git_push_origin_main"),
                                             ("shellgate", "echo_$(cat_x)")])  # fmt: skip
def test_a_floor_unreadable_or_host_gate_stays_a_human_card(
    repo: Path, config: SupervisorConfig, mode: str, gate: str
) -> None:
    client, first = _gated_run(config, repo, gate, mode=mode)
    _settled(config, first)
    detail = client.get(f"/api/runs/{first}").json()
    assert detail["run"]["state"] == "pending_approval"
    assert "unlocked_hosts" not in detail
    assert [r["verdict"] for r in _decisions(config)] in (["deny"], ["require_approval"])


@_SANDBOX
def test_a_host_gate_a_standing_rule_allows_still_stays_a_human_card(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v132-F1 (D42): unlocked answers no network host on any path — an
    unnamed host, or even one an existing rule already allows. Only a shell
    gate answers itself."""
    import skep.supervisor.serve.actions as actions
    from skep.supervisor.policy_v3 import Verdict

    def allow_everything(*_args: Any, **_kwargs: Any) -> Verdict:
        return Verdict("allow", "project", "allow-host", "project/allow-host", "run")

    monkeypatch.setattr(actions, "decide_v3", allow_everything)
    import time

    client, first = _gated_run(config, repo, "files.example", mode="hostgate")
    deadline = time.monotonic() + 3  # the done-callback answers in milliseconds
    while time.monotonic() < deadline:
        store = RunStore(config.db_path)
        try:
            assert all(a.status == "pending" for a in store.approvals_for(first))
        finally:
            store.close()
        time.sleep(0.05)
    detail = client.get(f"/api/runs/{first}").json()
    assert detail["run"]["state"] == "pending_approval"
    assert _decisions(config) == []  # never even asked decide() on the human's behalf


@_SANDBOX
def test_a_cli_run_on_an_unlocked_project_stops_at_an_unnamed_host_gate(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D42: the CLI path agrees with serve — an unnamed host gate is a stop
    for a human, never an auto-answer."""
    import shlex
    import sys

    from skep.supervisor.cli_cmds import build_config

    from .conftest import FAKE_WORKER

    root = tmp_path / "cli-home"
    config = build_config(root, None)
    config.home.mkdir(parents=True)
    lock_baseline(config.home)
    _bound(config, repo, UNLOCKED).close()
    argv = ["--home", str(root), "run", str(repo), "Fix it. MODE:hostgate GATE:files.example"]
    argv += ["--worker-cmd", shlex.join([sys.executable, str(FAKE_WORKER)]), "--quiet"]
    main(argv)
    assert "state:        pending_approval" in capsys.readouterr().out
    assert _decisions(config) == []


# --- phase-01 1.1: CLI `skep run` answers the same gates, through one decision --

# what the worker floor and decide()'s floor deny, and the opaque shell
# _widens keeps asking — with ZERO operator shell rules, so nothing but the
# floor and _widens stands between these gates and the unlocked step.
FLOOR_GATES = ["git_push_origin_main", "sudo_true", "env_git_push", "sh_-c_echo_x"]


def test_zero_shell_rules_never_unlock_the_floor_or_an_opaque_shell(
    repo: Path, config: SupervisorConfig
) -> None:
    from skep.supervisor.policy_v3 import Request, decide, load_stack
    from skep.supervisor.serve.settings import ConfigHolder

    config.home.mkdir(parents=True, exist_ok=True)
    lock_baseline(config.home, shell_rules=False)
    store = _bound(config, repo, UNLOCKED)
    try:
        stack = load_stack(ConfigHolder(config, store), store, repo=str(repo.resolve()))
    finally:
        store.close()
    assert stack.unlocked is not None
    for command, verdict in (("git push origin main", "deny"), ("sudo true", "deny"),
                             ("env git push", "deny"), ('sh -c "echo x"', "require_approval"),
                             ("make lint", "allow")):  # fmt: skip
        got = decide(Request("shell", "run", command), stack=stack, sandboxed=True)
        assert got.verdict == verdict, (command, got)


@_SANDBOX
@pytest.mark.parametrize("gate", FLOOR_GATES)
def test_serve_never_auto_answers_a_floor_or_opaque_shell_gate_with_zero_shell_rules(
    repo: Path, config: SupervisorConfig, gate: str
) -> None:
    client, first = _gated_run(config, repo, gate, shell_rules=False)
    _settled(config, first)
    assert client.get(f"/api/runs/{first}").json()["run"]["state"] == "pending_approval"
    rows = _decisions(config)
    assert [r["verdict"] for r in rows] == [
        "require_approval" if gate.startswith("sh_") else "deny"
    ]
    assert rows[0]["task_id"] == first


def _cli_run(
    tmp_path: Path,
    repo: Path,
    gate: str,
    *,
    unlocked: bool = True,
    shell_rules: bool = False,
    extra: tuple[str, ...] = (),
) -> tuple[SupervisorConfig, int]:
    """One CLI ``skep run`` on an (un)locked project whose worker gates on ``gate``."""
    import shlex
    import sys

    from skep.supervisor.cli_cmds import build_config

    from .conftest import FAKE_WORKER

    root = tmp_path / "cli-home"
    config = build_config(root, None)
    config.home.mkdir(parents=True)
    lock_baseline(config.home, shell_rules=shell_rules)
    _bound(config, repo, {**UNLOCKED, "unlocked": unlocked}).close()
    argv = ["--home", str(root), "run", str(repo), f"Fix it. MODE:shellgate GATE:{gate}"]
    argv += ["--worker-cmd", shlex.join([sys.executable, str(FAKE_WORKER)]), "--quiet"]
    argv += ["--execution-mode", "sandbox", *extra]
    return config, main(argv)


def _pending(config: SupervisorConfig) -> list[Any]:
    store = RunStore(config.db_path)
    try:
        return store.pending_approvals()
    finally:
        store.close()


@_SANDBOX
@pytest.mark.parametrize("gate", FLOOR_GATES)
def test_cli_never_auto_answers_a_floor_or_opaque_shell_gate_with_zero_shell_rules(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], gate: str
) -> None:
    config, code = _cli_run(tmp_path, repo, gate)
    assert code == 4
    assert "state:        pending_approval" in capsys.readouterr().out
    rows = _decisions(config)
    assert [r["verdict"] for r in rows] == [
        "require_approval" if gate.startswith("sh_") else "deny"
    ]
    assert [a.task_id for a in _pending(config)] == [rows[0]["task_id"]]


@_SANDBOX
def test_cli_run_answers_an_allowed_gate_itself_and_lands_like_serve(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, code = _cli_run(tmp_path, repo, "make_lint")
    out = capsys.readouterr().out
    assert code == 0, out
    rows = _decisions(config)
    assert [(r["verdict"], r["decided_by"]) for r in rows] == [
        ("allow", "unlocked:unlockable/zero-ask")
    ]
    first = rows[0]["task_id"]
    store = RunStore(config.db_path)
    try:
        resolved = store.approvals_for(first)
        assert [(a.status, a.resolved_by) for a in resolved] == [("approved", "auto:unlocked")]
        final = store.resumed_as_for(first)
        record = store.get_run(first)
        assert record is not None and record.state == "superseded"
    finally:
        store.close()
    assert final is not None and f"task {final}" in out
    # G10 confirmed the pinned verify, then the patch landed on skep/* — never main
    assert _landed(config, final) == ("auto:verified-patch", f"skep/{final}")


@_SANDBOX
def test_cli_run_stops_answering_at_the_resume_cap(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker that gates again after every resume must not loop the
    foreground forever: past the cap its gate waits for a human."""
    from skep.supervisor import cli_cmds

    real_run_task = cli_cmds.run_task  # type: ignore[attr-defined]

    def regating(*args: Any, **kwargs: Any) -> Any:
        kwargs.pop("approval_verdict", None)  # the fake worker gates again
        return real_run_task(*args, **kwargs)

    monkeypatch.setattr(cli_cmds, "run_task", regating)
    monkeypatch.setattr(cli_cmds, "MAX_UNLOCKED_RESUMES", 2)
    config, code = _cli_run(tmp_path, repo, "make_lint")
    out = capsys.readouterr().out
    assert code == 4, out
    assert "answered 2 gates in a row (the cap)" in out
    rows = _decisions(config)
    assert [r["verdict"] for r in rows] == ["allow", "allow"]
    pending = _pending(config)
    assert len(pending) == 1 and pending[0].task_id not in {r["task_id"] for r in rows}


@_SANDBOX
def test_cli_run_on_a_locked_project_still_stops_at_the_gate(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, code = _cli_run(tmp_path, repo, "make_lint", unlocked=False, shell_rules=True)
    assert code == 4
    assert "state:        pending_approval" in capsys.readouterr().out
    assert _decisions(config) == []
    assert len(_pending(config)) == 1


@_SANDBOX
def test_cli_minimal_run_never_answers_its_gate(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """R1 G1 (I5, I6): --minimal promises deny-all grants and learning through
    approvals; the unlocked step reloads the project's policy, so it never
    answers for a run that chose narrower grants."""
    config, code = _cli_run(tmp_path, repo, "make_lint", extra=("--minimal",))
    assert code == 4
    assert "state:        pending_approval" in capsys.readouterr().out
    assert _decisions(config) == []
    assert len(_pending(config)) == 1


def test_a_batch_gate_logs_every_request_before_it_answers(
    repo: Path, config: SupervisorConfig
) -> None:
    """R1 G2 (I13): each request a batch gate names is decided and logged,
    the ones after a refusal too; the gate answers only if all allow."""
    from skep.supervisor import mint_task
    from skep.supervisor.serve.actions import unlocked_gate_allowed
    from skep.supervisor.serve.settings import ConfigHolder

    config.home.mkdir(parents=True, exist_ok=True)
    lock_baseline(config.home, shell_rules=False)
    store = _bound(config, repo, UNLOCKED)
    try:
        task = mint_task(workspace=repo, instructions="gate")
        task = task.model_copy(update={"project_context": _own_project(repo)})
        store.create_run(task, repo=repo.resolve(), ref=None, execution_mode="sandbox")
        (config.audit_dir / task.task_id).mkdir(parents=True)
        (config.audit_dir / task.task_id / "task.json").write_text(task.model_dump_json())
        store.transition(task.task_id, "pending_approval", None)
        store.enqueue_approval(
            task.task_id, action="shell.run", reason="batch",
            commands=[["git", "push", "origin", "main"], ["make", "lint"]],
        )  # fmt: skip
        assert unlocked_gate_allowed(store, ConfigHolder(config, store), task.task_id) is None
    finally:
        store.close()
    rows = _decisions(config)
    assert [(r["verdict"], r["task_id"]) for r in rows] == [
        ("deny", task.task_id),
        ("allow", task.task_id),
    ]


@_SANDBOX
def test_cli_run_leaves_the_gate_when_its_resume_is_refused(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1 G3: a resume refused before it starts (no auth env, a missing
    engine) is an answer, not a traceback — the gate stays a human's,
    unclaimed, as serve leaves its card."""
    from skep.supervisor import cli_cmds

    real_run_task = cli_cmds.run_task  # type: ignore[attr-defined]

    def refusing(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("approval_verdict") is not None:
            raise ValueError("coding_engine 'codex': NO AUTH ENV")
        return real_run_task(*args, **kwargs)

    monkeypatch.setattr(cli_cmds, "run_task", refusing)
    config, code = _cli_run(tmp_path, repo, "make_lint")
    captured = capsys.readouterr()
    assert code == 4, captured
    assert "NO AUTH ENV" in captured.err and "Traceback" not in captured.err
    assert "state:        pending_approval" in captured.out
    rows = _decisions(config)
    assert [a.task_id for a in _pending(config)] == [rows[0]["task_id"]]


@_SANDBOX
def test_cli_run_leaves_the_gate_when_its_task_envelope_is_unreadable(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1 G3: the decision itself reads task.json — an unreadable one is a
    printed reason and a standing gate, never a traceback."""
    import skep.supervisor.serve.actions as actions

    def unreadable(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError("task.json")

    monkeypatch.setattr(actions, "unlocked_gate_allowed", unreadable)
    config, code = _cli_run(tmp_path, repo, "make_lint")
    captured = capsys.readouterr()
    assert code == 4, captured
    assert "could not answer the gate" in captured.err and "Traceback" not in captured.err
    assert len(_pending(config)) == 1


@_SANDBOX
def test_cli_auto_resume_keeps_the_audited_pin_caste_and_engine(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1 G7 (I2): the resumed successor runs under the audited task's pinned
    verify_command, caste, protocol and engine — never repo-policy defaults
    (a template-bound pin is one run_task's repo lookup cannot see)."""
    from skep.supervisor import cli_cmds
    from skep.worker_contract import CodingWorkerTask

    real_run_task = cli_cmds.run_task  # type: ignore[attr-defined]
    resumed: list[dict[str, Any]] = []

    def watching(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("approval_verdict") is not None:
            resumed.append(kwargs)
        return real_run_task(*args, **kwargs)

    monkeypatch.setattr(cli_cmds, "run_task", watching)
    config, code = _cli_run(tmp_path, repo, "make_lint")
    assert code == 0, capsys.readouterr()
    first = _decisions(config)[0]["task_id"]
    audited = CodingWorkerTask.model_validate_json(
        (config.audit_dir / first / "task.json").read_text()
    )
    assert audited.verify_command == PIN
    [kwargs] = resumed
    assert kwargs["verify_command"] == PIN
    assert kwargs["worker_kind"] == audited.worker_kind
    assert kwargs["planning_protocol"] == audited.planning_protocol
    assert kwargs["coding_engine"] == "builtin"


@_SANDBOX
def test_cli_template_run_under_a_locked_project_never_answers_its_gate(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review R3 (H2, I5, I6): a template bound to a LOCKED project resolves
    that project's policy; the unlocked step reloads the repo's own
    (unlocked) one — not the policy the run ran under — so it never answers."""
    import shlex
    import sys

    from skep.supervisor.cli_cmds import build_config
    from skep.supervisor.templates import WorkflowTemplate

    from .conftest import FAKE_WORKER

    root = tmp_path / "cli-home"
    config = build_config(root, None)
    config.home.mkdir(parents=True)
    lock_baseline(config.home, shell_rules=False)
    store = _bound(config, repo, UNLOCKED)
    try:
        store.add_project_policy(
            project_id="locked", name="Locked", strategy="trusted_local_dev", phase="build",
            policy={"verify_command": PIN},
        )  # fmt: skip
        store.add_project_binding(
            project_id="locked", binding_kind="template_name", binding_value="tpl"
        )
        store.add_template(
            WorkflowTemplate(name="tpl", instructions="Fix it. MODE:shellgate GATE:make_lint")
        )
    finally:
        store.close()
    argv = ["--home", str(root), "run", "--template", "tpl", str(repo), "--quiet"]
    argv += ["--worker-cmd", shlex.join([sys.executable, str(FAKE_WORKER)])]
    argv += ["--execution-mode", "sandbox"]
    assert main(argv) == 4, capsys.readouterr()
    assert "state:        pending_approval" in capsys.readouterr().out
    assert _decisions(config) == []
    assert len(_pending(config)) == 1


@_SANDBOX
def test_cli_auto_resume_after_a_pin_change_does_not_auto_land(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review R3 (H5, I2): the operator changed the pin while the gate was
    pending — the successor runs under the audited pin but never lands by
    itself under the superseded check; the CLI says so."""
    from skep.supervisor import cli_cmds

    real_run_task = cli_cmds.run_task  # type: ignore[attr-defined]
    resumed: list[dict[str, Any]] = []

    def drifting(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("approval_verdict") is not None:
            resumed.append(kwargs)
            return real_run_task(*args, **kwargs)
        outcome = real_run_task(*args, **kwargs)
        store = RunStore(kwargs["config"].db_path)
        try:
            store.add_project_policy(
                project_id="unlockable", name="Unlockable", strategy="trusted_local_dev",
                phase="build", policy={**UNLOCKED, "verify_command": "true"},
            )  # fmt: skip
        finally:
            store.close()
        return outcome

    monkeypatch.setattr(cli_cmds, "run_task", drifting)
    config, code = _cli_run(tmp_path, repo, "make_lint")
    out = capsys.readouterr().out
    [kwargs] = resumed
    assert kwargs["verify_command"] == PIN
    assert kwargs["auto_apply_verified_patch"] is False
    assert "verify pin changed while the gate waited" in out
    final = _decisions(config)[0]["task_id"]
    store = RunStore(config.db_path)
    try:
        successor = store.resumed_as_for(final)
        assert successor is not None
        # R3 (Neon): the approval ledger says so too, as serve's does
        [gate] = store.approvals_for(final)
        assert "verify pin changed while the gate waited" in (gate.resolution_note or "")
        landed = [a for a in store.approvals_for(successor) if a.action == "apply_patch"]
        assert all(a.resolved_by != "auto:verified-patch" for a in landed), landed
    finally:
        store.close()
    assert code in (0, 4), out


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
    # D42 / L4: doctor tells the truth about hosts and the CLI path
    assert "a host no rule names is still refused or asked, never auto-approved" in lines[0]
    # phase-01 1.1/1.2: the CLI answers unlocked gates too, up to its cap
    assert "still stops at every gate" not in lines[0]
    assert "a CLI `skep run` (which answers up to 10 gates in a row" in lines[0]
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
        "7 days: files.example (2), pypi.org (1) — historical: unlocked no longer "
        "approves any host (D42); each is a decision-log row "
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
                "repo_path": str(repo), "policy_overrides": {"verify_command": PIN},
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

    assert _setup(serve_client(unlockable), repo, {"verify_command": PIN}).status_code == 201
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


@pytest.mark.parametrize("pack", [None, "public_free"])
def test_a_phase_change_keeps_an_operator_value_over_a_strategy_default(
    repo: Path, config: SupervisorConfig, pack: str | None
) -> None:
    """sol-loop R2 T1 / R3 U1: a public_free project's own default_network
    survived as far as the merge, then the strategy default ([]) overwrote
    it — for a first-party project and a pack-installed one alike."""
    from skep.supervisor.serve.registry import set_project_phase

    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="pub", name="pub", strategy="public_free", phase="build",
            policy={"default_network": ["pypi.org"], "verify_command": PIN},
            **({"pack_name": pack, "pack_version": "1"} if pack else {}),
        )  # fmt: skip
        store.add_project_binding(project_id="pub", binding_kind="repo_path",
                                  binding_value=str(repo.resolve()))  # fmt: skip
        moved = set_project_phase(store, "pub", "maintain")
        assert moved["policy"]["default_network"] == ["pypi.org"]
    finally:
        store.close()
