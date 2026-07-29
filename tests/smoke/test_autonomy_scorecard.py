"""v12 Step 3: trusted-project smoke scenarios for the autonomy scorecard.

Six golden end-to-end scenarios that prove the trust loop works and stays
bounded. They are deterministic (audit caste — no provider, no network — plus a
scripted FakeOpenAI coding run and pure capability decisions) and opt-in via the
``smoke`` marker, so the scorecard (Step 4) can run them as evidence.

1. Setup -> dispatch -> execute -> verify -> re-verify -> land.
2. A blocked shell action escalates with a capability reason code.
3. Approving the gate resumes the run to completion.
4. Phase-aware landing differs by phase (maintain auto-lands, build files it).
5. Scheduled maintenance runs unattended and advances the schedule.
6. Plugin-risk escalation does not widen grants.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from skep.profile import run_personal_setup
from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.policy import SAFE_DEPENDENCY_RULE
from skep.supervisor.scheduler import make_schedule, run_due
from skep.workers.capabilities import CapabilityRegistry, PluginToolSpec
from tests.fixtures.toy_repo import create_audit_toy_repo
from tests.supervisor.conftest import serve_client as _client
from tests.supervisor.conftest import wait_terminal as _wait_terminal
from tests.supervisor.fake_openai import FakeOpenAI

pytestmark = pytest.mark.smoke


def _audit_config(home: Path, *, auto_land: bool = True) -> SupervisorConfig:
    return SupervisorConfig(
        home=home / "supervisor",
        worker_command=("false",),
        caste_worker_commands={"audit": (sys.executable, "-m", "skep.workers.audit")},
        auto_approval_rules=(SAFE_DEPENDENCY_RULE,) if auto_land else (),
        grace_seconds=5.0,
        heartbeat_seconds=10.0,
        poll_seconds=0.02,
    )


def _branch_exists(repo: Path, branch: str) -> bool:
    out = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", branch],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return branch in out


# --- Scenario 1: full trust-loop pipeline, unattended auto-land --------------


def test_scenario_1_dispatch_execute_verify_reverify_land(tmp_path: Path) -> None:
    repo = create_audit_toy_repo(tmp_path / "safe")
    config = _audit_config(tmp_path / "home")
    store = RunStore(config.db_path)
    try:
        store.add_schedule(
            make_schedule(
                name="safe-nightly",
                repo=repo,
                instructions="Audit dependencies and bump anything with a known advisory.",
                interval_seconds=86400,
                worker_kind="audit",
                start_at="2026-06-11T00:00:00Z",
            )
        )
        ran = run_due(store=store, config=config, now="2026-06-11T03:00:00Z")
        assert len(ran) == 1
        result = ran[0]
        assert result.state == "completed", result
        assert result.task_id is not None

        # Supervisor-side re-verification (G10) independently confirmed the fix.
        reverify = store.reverification_for(result.task_id)
        assert reverify is not None and reverify.confirmed

        # The safe fix auto-landed on its review branch (patch-as-approval).
        approvals = store.approvals_for(result.task_id)
        assert any(
            a.status == "approved" and (a.resolved_by or "").startswith("auto:")
            for a in approvals
        ), approvals
        assert _branch_exists(repo, f"skep/{result.task_id}")
    finally:
        store.close()


# --- Scenarios 2 & 3: blocked shell escalates with a reason code, then resumes


def test_scenario_2_3_blocked_shell_escalates_then_resume_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = build_config(tmp_path / "home", None)
    client = _client(config)
    server = FakeOpenAI(api_key="sk-fake").start()
    monkeypatch.setenv("SKEP_TEST_LLM_KEY", "sk-fake")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    (repo / "existing.py").write_text("value = 0\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"], check=True)

    write_cmd = [sys.executable, "-c", "from pathlib import Path; Path('a.txt').write_text('a')"]
    plan = json.dumps(
        {
            "summary": "run a non-allowlisted write command then verify",
            "required_tools": ["shell.run"],
            "steps": [
                {"tool": "shell.run", "args": {"argv": write_cmd}},
                {
                    "tool": "shell.run",
                    "args": {"argv": [sys.executable, "-c", "print('ok')"], "purpose": "verify"},
                },
            ],
            "verify": {},
        }
    )
    try:
        run_personal_setup(
            config.home.parent,
            provider="openai-compat",
            model="gpt-oss",
            endpoint=server.base_url,
            api_key_env="SKEP_TEST_LLM_KEY",
        )
        client.put(
            "/api/policy",
            json={
                "trusted_workspace_roots": [str(tmp_path)],
                "default_execution_mode": "workspace",
            },
        )
        server.script_reply(plan)
        server.script_reply(plan)
        task_id = client.post(
            "/api/runs",
            json={
                "repo": str(repo),
                "instructions": "Run a command.",
                "network": ["*"],
                "env_allowlist": ["SKEP_TEST_LLM_KEY"],
            },
        ).json()["task_id"]

        # Scenario 2: the non-allowlisted shell command escalates — exactly one gate.
        assert _wait_terminal(client, task_id)["state"] == "pending_approval"
        approvals = client.get("/api/approvals").json()["approvals"]
        assert len(approvals) == 1, "one gate for the whole plan"

        store = RunStore(config.db_path)
        try:
            events = store.events_for(task_id)
        finally:
            store.close()
        reasons = [
            e.payload["decision"].get("reason")
            for e in events
            if isinstance(e.payload.get("decision"), dict)
        ]
        assert any(
            r == "capability.require_approval.shell_nonverify_not_allowlisted" for r in reasons
        ), reasons

        # Scenario 3: one approve grants the command and the single resume completes.
        review_id = approvals[0]["review_id"]
        response = client.post(f"/api/approvals/{review_id}/approve", json={"actor": "tester"})
        assert response.status_code == 200
        resumed_id = str(response.json()["resumed_as"])
        resumed = _wait_terminal(client, resumed_id)
        assert resumed["state"] == "completed"
        assert resumed["resume_of"] == task_id
    finally:
        server.stop()


# --- Scenario 4: phase-aware landing differs by phase ------------------------


def _bind_audit_project(
    store: RunStore, repo: Path, *, project_id: str, phase: str
) -> None:
    store.add_project_policy(
        project_id=project_id,
        name=f"trusted {phase}",
        strategy="trusted_local_dev",
        phase=phase,
        policy={
            "auto_dispatch_allowed": True,
            "default_execution_mode": "workspace",
            # v90-F4: the auto-landing lane requires the PROJECT to say what
            # verification means. This is the audit worker's own real check
            # (`audit._VERIFY_COMMAND`) — supervisor-declared instead of
            # worker-nominated, which is the whole point of the rule.
            "verify_command": f"{sys.executable} -m pytest -q",
        },
    )
    store.add_project_binding(
        project_id=project_id, binding_kind="repo_path", binding_value=str(repo)
    )


def test_scenario_4_phase_aware_landing(tmp_path: Path) -> None:
    maintain_repo = create_audit_toy_repo(tmp_path / "maintain")
    build_repo = create_audit_toy_repo(tmp_path / "build")
    # No config-level rule: landing must be governed by project phase policy only.
    config = _audit_config(tmp_path / "home", auto_land=False)
    store = RunStore(config.db_path)
    try:
        _bind_audit_project(store, maintain_repo, project_id="maintain-proj", phase="maintain")
        _bind_audit_project(store, build_repo, project_id="build-proj", phase="build")
        for name, repo in (("maintain-audit", maintain_repo), ("build-audit", build_repo)):
            store.add_schedule(
                make_schedule(
                    name=name,
                    repo=repo,
                    instructions="Audit dependencies nightly.",
                    interval_seconds=86400,
                    worker_kind="audit",
                    start_at="2026-06-11T00:00:00Z",
                )
            )

        ran = {r.name: r for r in run_due(store=store, config=config, now="2026-06-11T09:00:00Z")}
        assert set(ran) == {"maintain-audit", "build-audit"}
        maintain_id = ran["maintain-audit"].task_id
        build_id = ran["build-audit"].task_id
        assert maintain_id is not None and build_id is not None

        # maintain: auto_apply_verified_patch defaults True -> auto-lands on a branch.
        assert _branch_exists(maintain_repo, f"skep/{maintain_id}")
        # build: auto_apply defaults False -> filed for review, nothing landed.
        assert not any(
            (a.resolved_by or "").startswith("auto:") for a in store.approvals_for(build_id)
        )
        assert not _branch_exists(build_repo, f"skep/{build_id}")
    finally:
        store.close()


# --- Scenario 5: scheduled maintenance runs unattended and advances ----------


def test_scenario_5_scheduled_maintenance_unattended(tmp_path: Path) -> None:
    repo = create_audit_toy_repo(tmp_path / "safe")
    config = _audit_config(tmp_path / "home")
    store = RunStore(config.db_path)
    try:
        store.add_schedule(
            make_schedule(
                name="weekly-maintain",
                repo=repo,
                instructions="Weekly low-risk maintenance.",
                interval_seconds=604800,
                worker_kind="audit",
                start_at="2026-06-11T00:00:00Z",
            )
        )
        # Not yet due.
        assert run_due(store=store, config=config, now="2026-06-10T00:00:00Z") == []
        # Due: runs with zero human interaction and advances the next run forward.
        ran = run_due(store=store, config=config, now="2026-06-11T01:00:00Z")
        assert len(ran) == 1 and ran[0].state == "completed"

        schedule = store.list_schedules()[0]
        assert schedule.last_task_id == ran[0].task_id
        assert schedule.next_run_at > "2026-06-11T01:00:00Z"
        # A second tick before the new due time does nothing (unattended, bounded).
        assert run_due(store=store, config=config, now="2026-06-12T00:00:00Z") == []
    finally:
        store.close()


# --- Scenario 6: plugin-risk escalation does not widen grants ----------------


def _plugin_registry(
    tmp_path: Path,
    *,
    approved_capability_ids: tuple[str, ...] = (),
    approved_plugin_risks: dict[str, str] | None = None,
) -> CapabilityRegistry:
    tmp_path.mkdir(parents=True, exist_ok=True)
    script = tmp_path / "plugin.py"
    script.write_text("print('{}')\n", encoding="utf-8")
    tools = (
        PluginToolSpec(
            plugin_id="writer",
            tool_id="writer.write",
            description="Write through plugin.",
            risk="write",
            command=("python", str(script)),
        ),
        PluginToolSpec(
            plugin_id="other",
            tool_id="other.write",
            description="A different write plugin.",
            risk="write",
            command=("python", str(script)),
        ),
    )
    return CapabilityRegistry(
        tmp_path,
        emit=lambda _type, _payload: None,
        approved_capability_ids=approved_capability_ids,
        approved_plugin_risks=approved_plugin_risks or {},
        plugin_tools=tools,
    )


def test_scenario_6_plugin_risk_escalation_does_not_widen_grants(tmp_path: Path) -> None:
    # No grants: a write-risk plugin escalates for approval.
    registry = _plugin_registry(tmp_path / "a")
    decision = registry.decision_for("writer.write", {})
    assert decision.verdict == "require_approval"
    assert decision.reason == "capability.require_approval.plugin_risk_not_allowed"

    # A resume grant scoped to writer.write allows *only* writer.write; a
    # different write plugin still escalates — the grant did not widen.
    granted = _plugin_registry(
        tmp_path / "b",
        approved_capability_ids=("writer.write",),
        approved_plugin_risks={"writer.write": "write"},
    )
    allowed = granted.decision_for("writer.write", {})
    assert allowed.verdict == "allow_with_constraints"
    assert allowed.reason == "capability.allow.resume_approved.plugin_tool"

    still_blocked = granted.decision_for("other.write", {})
    assert still_blocked.verdict == "require_approval"
    assert still_blocked.reason == "capability.require_approval.plugin_risk_not_allowed"
