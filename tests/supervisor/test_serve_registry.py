"""Stage D (v5): registry endpoints (templates/schedules/skills/repos/settings)
plus the in-process ticker that replaces cron inside a container."""

from __future__ import annotations

import errno
import time
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import RunStore, SupervisorConfig, mint_task
from skep.supervisor.scheduler import make_schedule
from skep.supervisor.serve.app import TERMINAL_STATES
from skep.supervisor.skills import RunShape, draft_candidates, generate

from .conftest import serve_client as _client
from .conftest import wait_terminal as _wait_terminal


def test_template_crud_roundtrip(config: SupervisorConfig) -> None:
    client = _client(config)
    created = client.post(
        "/api/templates",
        json={
            "name": "dep-audit",
            "instructions": "Audit {{target}} dependencies",
            "params": [{"name": "target"}],
            "worker_kind": "audit",
        },
    )
    assert created.status_code == 201
    assert created.json()["provenance"] == "user"

    assert client.get("/api/templates/dep-audit").json()["worker_kind"] == "audit"
    names = [t["name"] for t in client.get("/api/templates").json()["templates"]]
    assert names == ["dep-audit"]

    assert client.post("/api/templates", json={"name": "broken"}).status_code == 400

    assert client.delete("/api/templates/dep-audit").json() == {"removed": True}
    assert client.get("/api/templates/dep-audit").status_code == 404


def test_approval_ledger_endpoint_filters_by_repo(
    repo: Path, tmp_path: Path, config: SupervisorConfig
) -> None:
    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()
    store = RunStore(config.db_path)
    try:
        first = mint_task(workspace=repo, instructions="Add a health endpoint.")
        second = mint_task(workspace=other_repo, instructions="Add a billing endpoint.")
        store.create_run(first, repo=repo, ref=None, execution_mode="workspace")
        store.create_run(second, repo=other_repo, ref=None, execution_mode="workspace")
        store.transition(first.task_id, "completed")
        store.transition(second.task_id, "failed")
        store.record_approval_ledger(
            task_id=first.task_id,
            action="shell.run",
            resource="python -m pytest",
            reason="run tests",
            approved_by="tester",
            remembered=True,
        )
        store.record_approval_ledger(
            task_id=second.task_id,
            action="network.fetch",
            resource="pypi.org",
            reason="fetch package metadata",
            approved_by="tester",
            remembered=True,
        )
    finally:
        store.close()

    response = _client(config).get("/api/ledger", params={"repo": str(repo)})

    assert response.status_code == 200
    assert response.json() == {
        "ledger": [
            {
                "id": 1,
                "review_id": None,
                "task_id": first.task_id,
                "action": "shell.run",
                "resource": "python -m pytest",
                "reason": "run tests",
                "instructions_snippet": "Add a health endpoint.",
                "repo_path": str(repo),
                "template_name": None,
                "approved_at": response.json()["ledger"][0]["approved_at"],
                "approved_by": "tester",
                "task_outcome": "completed",
                "remembered": True,
            }
        ]
    }


def test_template_suggestions_preview_and_confirm(repo: Path, config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        task = mint_task(workspace=repo, instructions="Add a login page with JWT support")
        store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
        store.transition(task.task_id, "completed")
        store.record_approval_ledger(
            task_id=task.task_id,
            action="network.fetch",
            resource="https://pypi.org/simple/pyjwt/",
            reason="install PyJWT",
            approved_by="tester",
            remembered=True,
        )
        store.record_approval_ledger(
            task_id=task.task_id,
            action="shell.run",
            resource="python -m pytest",
            reason="run tests",
            approved_by="tester",
            remembered=True,
        )
    finally:
        store.close()

    client = _client(config)
    preview = client.get(
        "/api/suggestions",
        params={
            "name": "web-feature",
            "repo": str(repo),
            "instructions": "Add a signup page with JWT support",
        },
    )

    assert preview.status_code == 200
    assert preview.json()["suggestions"] == [
        {
            "id": "web-feature",
            "template": {
                "name": "web-feature",
                "description": "",
                "worker_kind": "coding",
                "provenance": "learned",
                "instructions": "Add a signup page with JWT support",
                "repo": str(repo),
                "ref": None,
                "network": ["pypi.org"],
                "env_allowlist": [],
                "shell_allowlist": [["python", "-m", "pytest"]],
                "allow_git_mutation": False,
                "budget": {
                    "wall_clock_seconds": 900,
                    "max_iterations": 16,
                    "max_actions": 100,
                    "max_provider_calls": 64,
                },
                "params": [],
            },
            "profile": {
                "repo_path": str(repo),
                "instruction_keywords": ["add", "jwt", "page", "signup", "support"],
                "network": ["pypi.org"],
                "env_allowlist": [],
                "shell_allowlist": [["python", "-m", "pytest"]],
                "allow_git_mutation": False,
                "source_entry_ids": [1, 2],
            },
        }
    ]

    confirmed = client.post(
        "/api/suggestions/web-feature/confirm",
        json={
            "repo": str(repo),
            "instructions": "Add a signup page with JWT support",
        },
    )

    assert confirmed.status_code == 201
    assert confirmed.json()["template"]["name"] == "web-feature"
    assert client.get("/api/templates/web-feature").json()["network"] == ["pypi.org"]


def test_schedule_crud_toggle_and_template_binding(repo: Path, config: SupervisorConfig) -> None:
    client = _client(config)
    created = client.post(
        "/api/schedules",
        json={
            "name": "nightly",
            "repo": str(repo),
            "every": "1d",
            "instructions": "Fix the bug. MODE:happy",
        },
    )
    assert created.status_code == 201
    assert created.json()["interval_seconds"] == 86400

    client.post(
        "/api/templates",
        json={"name": "audit-t", "instructions": "Audit {{t}}", "params": [{"name": "t"}]},
    )
    bound = client.post(
        "/api/schedules",
        json={
            "name": "nightly-audit",
            "repo": str(repo),
            "every": "1d",
            "template": "audit-t",
            "params": {"t": "acme"},
        },
    )
    assert bound.status_code == 201
    assert bound.json()["template_name"] == "audit-t"

    # Missing params fail eagerly, at schedule time — not at 3am.
    assert (
        client.post(
            "/api/schedules",
            json={"name": "broken", "repo": str(repo), "every": "1d", "template": "audit-t"},
        ).status_code
        == 400
    )

    toggled = client.patch("/api/schedules/nightly", json={"enabled": False})
    assert toggled.json()["enabled"] is False
    assert client.delete("/api/schedules/nightly").json() == {"removed": True}
    assert client.delete("/api/schedules/nightly").status_code == 404


def test_note_schedule_needs_no_repo(config: SupervisorConfig) -> None:
    """Caste 'note' schedules are repo-less; the text is the whole payload."""
    client = _client(config)
    created = client.post(
        "/api/schedules",
        json={"name": "joke", "every": "30s", "instructions": "tell me a joke", "caste": "note"},
    )
    assert created.status_code == 201
    assert created.json()["worker_kind"] == "note"
    assert created.json()["repo"] == ""
    assert created.json()["chat_id"] is None  # API-created: ticks post inert notes
    # a worker schedule without a repo is still an error.
    missing = client.post(
        "/api/schedules", json={"name": "broken", "every": "1d", "instructions": "lint"}
    )
    assert missing.status_code == 400
    # ...and so is a note schedule without its text.
    empty = client.post("/api/schedules", json={"name": "empty", "every": "1d", "caste": "note"})
    assert empty.status_code == 400


def test_script_schedule_face_is_repo_less_and_needs_a_command(
    config: SupervisorConfig,
) -> None:
    """v44-F4: caste 'script' — the token-authed API is the operator, so
    direct creation is the operator's own crontab trust level."""
    client = _client(config)
    created = client.post(
        "/api/schedules",
        json={
            "name": "sys-monitor",
            "every": "5m",
            "instructions": "~/bin/system_monitor.sh",
            "caste": "script",
        },
    )
    assert created.status_code == 201
    assert created.json()["worker_kind"] == "script"
    assert created.json()["repo"] == ""
    missing = client.post(
        "/api/schedules", json={"name": "no-cmd", "every": "5m", "caste": "script"}
    )
    assert missing.status_code == 400
    assert "shell command" in missing.json()["detail"]


def test_one_shot_reminder_carries_once_and_start_at(config: SupervisorConfig) -> None:
    """v44-F2: 'remind me tomorrow at 9am' = once + start_at; the row shows
    both and the tick self-disables it after the single fire."""
    client = _client(config)
    created = client.post(
        "/api/schedules",
        json={
            "name": "deploy-check",
            "every": "1d",
            "instructions": "check the deploy",
            "caste": "note",
            "once": True,
            "start_at": "2030-01-02T09:00:00Z",
        },
    )
    assert created.status_code == 201
    assert created.json()["once"] is True
    assert created.json()["next_run_at"] == "2030-01-02T09:00:00Z"


def test_schedule_list_reports_bound_project_context_from_template_binding(
    repo: Path, config: SupervisorConfig
) -> None:
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="project-1",
            name="trusted nightly",
            strategy="trusted_local_dev",
            phase="maintain",
            policy={"default_execution_mode": "workspace"},
        )
        store.add_project_binding(
            project_id="project-1",
            binding_kind="template_name",
            binding_value="audit-t",
        )
    finally:
        store.close()

    client = _client(config)
    client.post(
        "/api/templates",
        json={"name": "audit-t", "instructions": "Audit {{t}}", "params": [{"name": "t"}]},
    )
    created = client.post(
        "/api/schedules",
        json={
            "name": "nightly-audit",
            "repo": str(repo),
            "every": "1d",
            "template": "audit-t",
            "params": {"t": "acme"},
        },
    )
    assert created.status_code == 201
    assert created.json()["project_context"] == {
        "project_id": "project-1",
        "name": "trusted nightly",
        "strategy": "trusted_local_dev",
        "phase": "maintain",
        "binding_kind": "template_name",
        "binding_value": "audit-t",
    }

    listed = client.get("/api/schedules").json()["schedules"]
    assert listed[0]["project_context"] == {
        "project_id": "project-1",
        "name": "trusted nightly",
        "strategy": "trusted_local_dev",
        "phase": "maintain",
        "binding_kind": "template_name",
        "binding_value": "audit-t",
    }


def test_ticker_dispatches_a_due_schedule_with_no_cron(
    repo: Path, config: SupervisorConfig
) -> None:
    seed = RunStore(config.db_path)
    try:
        seed.set_setting("ticker_interval_seconds", 1)
        seed.add_schedule(
            make_schedule(
                name="due-now",
                repo=repo,
                instructions="Fix the bug. MODE:happy",
                interval_seconds=3600,
                start_at="2020-01-01T00:00:00Z",
            )
        )
    finally:
        seed.close()

    # The context manager runs the lifespan — that is what starts the ticker.
    with _client(config) as client:
        deadline = time.monotonic() + 30.0
        runs: list[dict[str, object]] = []
        while time.monotonic() < deadline:
            runs = client.get("/api/runs").json()["runs"]
            if runs and runs[0]["state"] in TERMINAL_STATES:
                break
            time.sleep(0.2)
        assert runs, "the ticker never dispatched the due schedule"
        assert runs[0]["state"] == "completed"
        # The run turns terminal at ingest, inside the tick — a beat before
        # run_due records the schedule as ran. Poll the row, don't race it.
        schedule = client.get("/api/schedules").json()["schedules"][0]
        while schedule["last_task_id"] is None and time.monotonic() < deadline:
            time.sleep(0.2)
            schedule = client.get("/api/schedules").json()["schedules"][0]
        assert schedule["last_task_id"] == runs[0]["task_id"]


def test_repo_registry_clone_run_by_slug_and_delete(repo: Path, config: SupervisorConfig) -> None:
    client = _client(config)
    created = client.post("/api/repos", json={"url": str(repo), "name": "fixture"})
    assert created.status_code == 201
    assert client.post("/api/repos", json={"url": str(repo), "name": "fixture"}).status_code == 409

    listed = client.get("/api/repos").json()["repos"]
    assert [r["name"] for r in listed] == ["fixture"]

    # A run can now name the repo by slug — no host path involved.
    task_id = client.post(
        "/api/runs",
        json={
            "repo": "fixture",
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "workspace",
        },
    ).json()["task_id"]
    run = _wait_terminal(client, task_id)
    assert run["state"] == "completed"
    assert str(run["repo"]).endswith("repos/fixture")

    # The run is terminal at ingest, a beat before dispatch tears down its
    # worktree (which touches the clone's .git/worktrees bookkeeping) — wait
    # out the teardown so the delete below cannot race it.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and any(config.worktrees_root.glob("*")):
        time.sleep(0.05)

    assert client.delete("/api/repos/fixture").json() == {"removed": True}
    assert client.get("/api/repos").json()["repos"] == []
    # v106-F8 (I9): a second delete of the same name teaches instead of the
    # bare 404 that had the Queen retrying blind names in the field.
    gone = client.delete("/api/repos/fixture")
    assert gone.status_code == 404
    assert "no registered clone named 'fixture'" in gone.json()["detail"]
    assert "deleting its project" in gone.json()["detail"]
    # The slug no longer resolves, and 'fixture' is not a host path either.
    assert (
        client.post(
            "/api/runs",
            json={"repo": "fixture", "instructions": "x", "execution_mode": "workspace"},
        ).status_code
        == 400
    )


def test_repo_delete_retries_git_cleanup_race(config: SupervisorConfig, monkeypatch: Any) -> None:
    client = _client(config)
    repo_root = config.home.parent / "repos" / "fixture"
    (repo_root / ".git").mkdir(parents=True)
    calls = 0

    def flaky_rmtree(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(errno.ENOTEMPTY, "Directory not empty", str(path / ".git"))
        repo_root.rename(repo_root.with_suffix(".removed"))

    monkeypatch.setattr("skep.supervisor.serve.registry.shutil.rmtree", flaky_rmtree)

    assert client.delete("/api/repos/fixture").json() == {"removed": True}
    assert calls == 2


def test_project_registry_crud_and_binding_validation(repo: Path, config: SupervisorConfig) -> None:
    client = _client(config)
    client.post(
        "/api/templates",
        json={"name": "audit-t", "instructions": "Audit {{t}}", "params": [{"name": "t"}]},
    )
    created_repo = client.post("/api/repos", json={"url": str(repo), "name": "fixture"})
    assert created_repo.status_code == 201

    created = client.post(
        "/api/projects",
        json={
            "project_id": "acme",
            "name": "Acme API",
            "strategy": "trusted_local_dev",
            "phase": "build",
            "policy": {
                "default_execution_mode": "workspace",
                "default_network": ["*"],
                "allowed_shell_commands": [["pytest"]],
            },
            "bindings": [
                {"kind": "repo_path", "value": str(repo)},
                {"kind": "repo_slug", "value": "fixture"},
                {"kind": "template_name", "value": "audit-t"},
            ],
        },
    )
    assert created.status_code == 201
    assert created.json()["project_id"] == "acme"
    assert [binding["kind"] for binding in created.json()["bindings"]] == [
        "repo_path",
        "repo_slug",
        "template_name",
    ]

    listed = client.get("/api/projects")
    assert listed.status_code == 200
    assert [project["project_id"] for project in listed.json()["projects"]] == ["acme"]

    detail = client.get("/api/projects/acme")
    assert detail.status_code == 200
    assert detail.json()["strategy"] == "trusted_local_dev"
    assert detail.json()["phase"] == "build"
    assert detail.json()["bindings"][1] == {"kind": "repo_slug", "value": "fixture"}

    invalid = client.post(
        "/api/projects",
        json={
            "project_id": "broken",
            "name": "Broken",
            "strategy": "wizard_mode",
            "phase": "now",
            "policy": {},
            "bindings": [{"kind": "bogus", "value": "x"}],
        },
    )
    assert invalid.status_code == 400

    invalid_policy = client.post(
        "/api/projects",
        json={
            "project_id": "broken-policy",
            "name": "Broken Policy",
            "strategy": "trusted_local_dev",
            "phase": "build",
            "policy": {"wizard_mode": True},
            "bindings": [{"kind": "repo_path", "value": str(repo)}],
        },
    )
    assert invalid_policy.status_code == 400
    assert "unknown project policy fields" in invalid_policy.json()["detail"]

    assert client.delete("/api/projects/acme").json() == {"removed": True}
    assert client.get("/api/projects/acme").status_code == 404


def test_project_setup_applies_first_party_pack_defaults(
    repo: Path, config: SupervisorConfig
) -> None:
    client = _client(config)
    client.post(
        "/api/templates",
        json={"name": "audit-t", "instructions": "Audit {{t}}", "params": [{"name": "t"}]},
    )
    created_repo = client.post("/api/repos", json={"url": str(repo), "name": "fixture"})
    assert created_repo.status_code == 201

    created = client.post(
        "/api/projects/setup",
        json={
            "project_id": "packed",
            "name": "Packed Project",
            "strategy": "trusted_local_dev",
            "phase": "maintain",
            "repo_path": str(repo),
            "repo_slug": "fixture",
            "template_names": ["audit-t"],
            "policy_overrides": {"allowed_shell_commands": [["pytest"]]},
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert body["project_id"] == "packed"
    assert body["strategy"] == "trusted_local_dev"
    assert body["phase"] == "maintain"
    assert body["policy"]["default_execution_mode"] == "workspace"
    assert body["policy"]["auto_dispatch_allowed"] is True
    assert body["policy"]["auto_apply_verified_patch"] is True
    assert body["policy"]["allowed_shell_commands"] == [["pytest"]]
    assert [schedule["name"] for schedule in body["seeded_schedules"]] == ["packed-maintain-weekly"]
    assert body["seeded_schedules"][0]["repo"] == str(repo.resolve())
    assert body["seeded_schedules"][0]["interval_seconds"] == 604800
    assert [binding["kind"] for binding in body["bindings"]] == [
        "repo_path",
        "repo_slug",
        "template_name",
    ]

    detail = client.get("/api/projects/packed")
    assert detail.status_code == 200
    assert detail.json()["policy"]["auto_apply_verified_patch"] is True
    schedules = client.get("/api/schedules").json()["schedules"]
    assert [schedule["name"] for schedule in schedules] == ["packed-maintain-weekly"]

    missing_binding = client.post(
        "/api/projects/setup",
        json={
            "project_id": "empty",
            "name": "Empty",
            "strategy": "public_free",
            "phase": "build",
        },
    )
    assert missing_binding.status_code == 400
    assert "at least one binding" in missing_binding.json()["detail"]


def test_project_setup_preview_and_save_from_policy_pack(
    repo: Path, config: SupervisorConfig
) -> None:
    client = _client(config)

    packs = client.get("/api/projects/packs")
    assert packs.status_code == 200
    assert [pack["name"] for pack in packs.json()["packs"]] == [
        "public_free",
        "trusted_local_dev",
        "trusted_local_ops",
    ]
    # v15 Step 3: trusted_local_ops was promoted from draft to supported.
    assert packs.json()["packs"][2]["status"] == "supported"

    preview = client.post(
        "/api/projects/preview",
        json={
            "project_id": "free-project",
            "name": "Free Project",
            "pack": "public_free",
            "phase": "build",
            "repo_path": str(repo),
        },
    )
    assert preview.status_code == 200
    body = preview.json()
    assert body["pack"] == {"name": "public_free", "version": "1", "status": "supported"}
    assert body["project"]["strategy"] == "public_free"
    assert body["project"]["pack_name"] == "public_free"
    assert body["project"]["pack_version"] == "1"
    assert body["effective_policy"]["default_execution_mode"] == "workspace"
    assert body["effective_policy"]["default_network"] == []
    assert (
        body["sample_dispatch_decision"]["reason"] == "dispatch.auto_allowed.project_policy_match"
    )
    assert body["sample_landing_decision"]["reason"] == (
        "landing.require_approval.project_policy_disabled_auto_apply"
    )
    assert "auto_dispatch_allowed" in body["dangerous_grant_warnings"]
    assert [template["name"] for template in body["seeded_templates"]][:2] == [
        "free-project-public-free-deps",
        "free-project-public-free-docs",
    ]
    assert [schedule["template"] for schedule in body["seeded_schedules"]][:2] == [
        "free-project-public-free-deps",
        "free-project-public-free-docs",
    ]
    assert client.get("/api/projects/free-project").status_code == 404

    saved = client.post(
        "/api/projects/setup",
        json={
            "project_id": "free-project",
            "name": "Free Project",
            "pack": "public_free",
            "phase": "build",
            "repo_path": str(repo),
        },
    )
    assert saved.status_code == 201
    saved_body = saved.json()
    assert saved_body["pack_name"] == "public_free"
    assert saved_body["pack_version"] == "1"
    assert [template["provenance"] for template in saved_body["seeded_templates"]] == [
        "pack:public_free@1",
        "pack:public_free@1",
        "pack:public_free@1",
        "pack:public_free@1",
    ]
    assert client.get("/api/projects/free-project").json()["pack_name"] == "public_free"
    assert [template["name"] for template in client.get("/api/templates").json()["templates"]] == [
        "free-project-public-free-changelog",
        "free-project-public-free-deps",
        "free-project-public-free-docs",
        "free-project-public-free-health",
    ]
    assert [
        schedule["template_name"] for schedule in client.get("/api/schedules").json()["schedules"]
    ] == [
        "free-project-public-free-deps",
        "free-project-public-free-docs",
        "free-project-public-free-health",
    ]


def test_project_setup_can_skip_default_schedule_seeding(
    repo: Path, config: SupervisorConfig
) -> None:
    client = _client(config)
    created = client.post(
        "/api/projects/setup",
        json={
            "project_id": "no-seeds",
            "name": "No Seeds",
            "strategy": "public_free",
            "phase": "build",
            "repo_path": str(repo),
            "seed_default_schedules": False,
        },
    )
    assert created.status_code == 201
    assert created.json()["seeded_schedules"] == []
    assert client.get("/api/schedules").json()["schedules"] == []


def test_skill_lifecycle_over_http_keeps_both_gates(repo: Path, config: SupervisorConfig) -> None:
    # Seed a draft candidate (the v4 generalizer output) straight into the store.
    # The varying word becomes the recipe's parameter (arg1).
    shapes = [
        RunShape(task_id="t1", worker_kind="coding", instructions="Fix acme now. MODE:happy"),
        RunShape(task_id="t2", worker_kind="coding", instructions="Fix globex now. MODE:happy"),
    ]
    candidate = draft_candidates(generate(shapes), created_at="2026-06-11T00:00:00Z")[0]
    seed = RunStore(config.db_path)
    try:
        seed.add_candidate(candidate)
    finally:
        seed.close()

    client = _client(config)
    skills = client.get("/api/skills").json()["skills"]
    assert [s["status"] for s in skills] == ["draft"]
    name = skills[0]["name"]

    # Gate order is fail-closed: a draft cannot be approved.
    assert client.post(f"/api/skills/{name}/approve", json={"actor": "t"}).status_code == 409

    tested = client.post(
        f"/api/skills/{name}/test", json={"repo": str(repo), "params": {"arg1": "acme"}}
    )
    assert tested.status_code == 200
    assert tested.json()["passed"] is True
    assert tested.json()["candidate"]["status"] == "tested"

    approved = client.post(f"/api/skills/{name}/approve", json={"actor": "anmol"})
    assert approved.status_code == 200
    target = approved.json()["template"]

    registry = {t["name"]: t for t in client.get("/api/templates").json()["templates"]}
    assert registry[target]["provenance"] == "learned"

    # Terminal decisions stay terminal.
    assert client.post(f"/api/skills/{name}/reject", json={"actor": "t"}).status_code == 409


def test_provider_settings_roundtrip_stores_key_name_only(config: SupervisorConfig) -> None:
    client = _client(config)
    assert client.get("/api/settings").json() == {"configured": False}

    updated = client.put(
        "/api/settings",
        json={
            "provider": "anthropic",
            "model": "claude-fable-5",
            "api_key_env": "ANTHROPIC_API_KEY",
        },
    ).json()
    assert updated["configured"] is True
    assert updated["model"] == "claude-fable-5"
    assert updated["api_key_env"] == "ANTHROPIC_API_KEY"

    profile = (config.home.parent / "profile.json").read_text()
    assert "ANTHROPIC_API_KEY" in profile  # the env-var *name*…
    assert "sk-" not in profile  # …never a secret value


# ---------- v14 Step 8: schedule + provider health API views ----------


def test_schedule_and_provider_health_routes(config: SupervisorConfig) -> None:
    from skep.supervisor.providers import ProviderHealth, ProviderProfile

    store = RunStore(config.db_path)
    try:
        store.add_schedule(
            make_schedule(
                name="nightly",
                repo=config.home / "repo",
                instructions="x",
                interval_seconds=86400,
                worker_kind="audit",
                start_at="2026-06-11T00:00:00Z",
            )
        )
        store.record_schedule_outcome("nightly", task_id="t1", state="completed")
        store.upsert_provider_profile(
            ProviderProfile(
                provider_id="local",
                protocol="ollama",
                base_url="http://localhost:11434",
                model="qwen3",
                active=True,
            )
        )
        store.record_provider_health(
            ProviderHealth(
                provider_id="local",
                reachable=True,
                model_found=True,
                latency_ms=5,
                error=None,
                checked_at="2026-07-08T00:00:00Z",
            )
        )
    finally:
        store.close()

    client = _client(config)
    sched = client.get("/api/schedules/health").json()["health"]
    assert sched[0]["name"] == "nightly"
    assert sched[0]["success_rate"] == 1.0

    providers = client.get("/api/providers").json()["providers"]
    assert [p["provider_id"] for p in providers] == ["local"]

    prov_health = client.get("/api/providers/health").json()["health"]
    assert prov_health[0]["reachable"] is True
    assert prov_health[0]["latency_ms"] == 5


def test_node_registry_routes(config: SupervisorConfig) -> None:
    from .conftest import serve_client

    client = serve_client(config)
    assert client.get("/api/nodes").json() == {"nodes": []}
    added = client.post(
        "/api/nodes",
        json={
            "node_id": "localhost",
            "trust_tier": "trusted_local",
            "allowed_capabilities": ["ops.inspect.disk"],
        },
    ).json()
    assert added["node_id"] == "localhost"
    assert added["allowed_capabilities"] == ["ops.inspect.disk"]
    assert [n["node_id"] for n in client.get("/api/nodes").json()["nodes"]] == ["localhost"]
    # Bad capability is a 400, not a silent write.
    assert (
        client.post(
            "/api/nodes", json={"node_id": "n2", "allowed_capabilities": ["ops.nope"]}
        ).status_code
        == 400
    )


def test_project_setup_seeds_toolchain_commands(repo: Path, config: SupervisorConfig) -> None:
    """v23-F4: setup previews the repo's own dev-loop commands as an allowlist
    batch the human approves once; opting out is possible; every seed passes
    the persistence guard."""
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (repo / "Makefile").write_text(
        "test:\n\techo test\n\nlint:\n\techo lint\n.PHONY: test lint\n", encoding="utf-8"
    )
    client = _client(config)

    preview = client.post(
        "/api/projects/preview",
        json={
            "project_id": "toolchain-proj",
            "name": "Toolchain",
            "pack": "trusted_local_dev",
            "phase": "build",
            "repo_path": str(repo),
        },
    )
    assert preview.status_code == 200
    body = preview.json()
    seeded = body["seeded_shell_commands"]
    assert ["uv", "run", "pytest"] in seeded
    assert ["pytest"] in seeded
    assert ["make", "test"] in seeded
    assert ["make", "lint"] in seeded
    assert body["effective_policy"]["allowed_shell_commands"] == seeded

    saved = client.post(
        "/api/projects/setup",
        json={
            "project_id": "toolchain-proj",
            "name": "Toolchain",
            "pack": "trusted_local_dev",
            "phase": "build",
            "repo_path": str(repo),
            "seed_default_schedules": False,
        },
    )
    assert saved.status_code in (200, 201)
    assert ["make", "test"] in saved.json()["seeded_shell_commands"]

    opted_out = client.post(
        "/api/projects/preview",
        json={
            "project_id": "toolchain-lean",
            "name": "Lean",
            "pack": "trusted_local_dev",
            "phase": "build",
            "repo_path": str(repo),
            "seed_shell_commands": False,
        },
    )
    assert opted_out.json()["seeded_shell_commands"] == []


def test_project_setup_pins_a_verify_command_by_default(
    repo: Path, config: SupervisorConfig
) -> None:
    """v91-F1 (I2): setup pins what verification MEANS instead of leaving G10 to
    re-run whatever the worker nominated for itself, and the pin survives the
    move into maintain — the one lane that lands without a human (v90-F4)."""
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (repo / "uv.lock").write_text("", encoding="utf-8")
    (repo / "tests").mkdir(exist_ok=True)
    client = _client(config)

    body = {
        "project_id": "pinned-proj",
        "name": "Pinned",
        "pack": "trusted_local_dev",
        "phase": "build",
        "repo_path": str(repo),
        "seed_default_schedules": False,
    }
    preview = client.post("/api/projects/preview", json=body).json()
    assert preview["seeded_verify_command"] == "uv run pytest"
    assert preview["effective_policy"]["verify_command"] == "uv run pytest"

    saved = client.post("/api/projects/setup", json=body)
    assert saved.status_code in (200, 201)
    assert saved.json()["policy"]["verify_command"] == "uv run pytest"

    # The phase move re-derives policy from defaults, which carry no pin.
    moved = client.post("/api/projects/pinned-proj/phase", json={"phase": "maintain"})
    assert moved.json()["policy"]["verify_command"] == "uv run pytest"
    assert moved.json()["policy"]["auto_apply_verified_patch"] is True


def test_project_setup_verify_pin_defers_to_overrides_and_skips_the_undetectable(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A confidently wrong pin is worse than none: nothing detected means no pin
    and the pre-v91 worker-nominated fallback, and an explicit pin always wins."""
    from skep.supervisor.serve.registry import verify_command_seed

    # v101-F14: the seed now probes the HOST as well as the repo, so pin the
    # host — otherwise this test asserts what the CI box happens to have
    # installed, which is the same mistake the seed itself was making.
    monkeypatch.setattr(
        "skep.supervisor.serve.registry.shutil.which", lambda name: f"/usr/bin/{name}"
    )

    client = _client(config)
    bare = client.post(
        "/api/projects/preview",
        json={
            "project_id": "bare-proj",
            "name": "Bare",
            "pack": "trusted_local_dev",
            "phase": "build",
            "repo_path": str(repo),
        },
    ).json()
    assert bare["seeded_verify_command"] == ""
    assert not bare["effective_policy"].get("verify_command")

    (repo / "Makefile").write_text("test:\n\techo t\n", encoding="utf-8")
    overridden = client.post(
        "/api/projects/preview",
        json={
            "project_id": "override-proj",
            "name": "Override",
            "pack": "trusted_local_dev",
            "phase": "build",
            "repo_path": str(repo),
            "policy_overrides": {"verify_command": "just check"},
        },
    ).json()
    assert overridden["seeded_verify_command"] == ""
    assert overridden["effective_policy"]["verify_command"] == "just check"

    # Targets that do not exist are never pinned: pytest with no tests exits 5,
    # which reverify reads as "failed", and npm's placeholder test exits 1.
    (repo / "Makefile").unlink()
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    assert verify_command_seed(repo) == ""
    (repo / "package.json").write_text(
        '{"scripts": {"test": "echo \\"Error: no test specified\\" && exit 1"}}',
        encoding="utf-8",
    )
    assert verify_command_seed(repo) == ""
    (repo / "package.json").write_text('{"scripts": {"test": "vitest run"}}', encoding="utf-8")
    assert verify_command_seed(repo) == "npm test"


def test_repeated_project_setup_never_reseeds(repo: Path, config: SupervisorConfig) -> None:
    """v24-F4: re-running setup is a policy update — existing templates and
    schedules are skipped, not re-created (which reset their timers and caused
    a surprise tick in the 2026-07-10 field test)."""
    client = _client(config)
    body = {
        "project_id": "reseed-proj",
        "name": "Reseed",
        "pack": "trusted_local_dev",
        "phase": "build",
        "repo_path": str(repo),
    }
    first = client.post("/api/projects/setup", json=body).json()
    assert len(first["seeded_schedules"]) == 1
    assert first["seeds_skipped"] == []
    store = RunStore(config.db_path)
    try:
        schedule_name = first["seeded_schedules"][0]["name"]
        before = store.get_schedule(schedule_name)
    finally:
        store.close()

    second = client.post("/api/projects/setup", json=body).json()
    assert second["seeded_templates"] == []
    assert second["seeded_schedules"] == []
    assert any(entry.startswith("schedule:") for entry in second["seeds_skipped"])
    assert any(entry.startswith("template:") for entry in second["seeds_skipped"])
    store = RunStore(config.db_path)
    try:
        after = store.get_schedule(schedule_name)
    finally:
        store.close()
    assert before is not None and after is not None
    assert after.next_run_at == before.next_run_at, "re-setup must not reset the timer"


def test_register_repo_pushes_baseline_when_origin_is_empty(tmp_path: Path) -> None:
    """v79-F1: an empty GitHub repo gets its synthesized baseline pushed at
    register time — the trap that killed the 2026-07-17 PR arc (local init
    commit never on origin, so no base branch existed for any PR)."""
    from skep.supervisor.serve.registry import register_repo

    from .conftest import git

    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "-q", "--bare")

    result = register_repo(tmp_path / "repos", url=str(origin))

    assert result["initialized_empty_repo"] is True
    assert result["baseline_pushed"] is True
    assert "baseline_push_detail" not in result
    heads = git(origin, "for-each-ref", "refs/heads")
    assert heads.stdout.strip(), "origin must now have a default branch"
    log = git(origin, "log", "--oneline", "-1")
    assert "Initialize repository for skep" in log.stdout


def test_register_repo_leaves_seeded_origin_alone(tmp_path: Path) -> None:
    """A remote that already has branches is never pushed to at register time."""
    from skep.supervisor.serve.registry import register_repo

    from .conftest import git

    origin = tmp_path / "seeded"
    origin.mkdir()
    git(origin, "init", "-q")
    git(origin, "config", "user.email", "t@e.com")
    git(origin, "config", "user.name", "T")
    (origin / "seed.py").write_text("x = 1\n")
    git(origin, "add", "seed.py")
    git(origin, "commit", "-qm", "seed")
    before = git(origin, "rev-parse", "HEAD").stdout.strip()

    result = register_repo(tmp_path / "repos", url=str(origin))

    assert result["initialized_empty_repo"] is False
    assert result["baseline_pushed"] is False
    assert "baseline_push_detail" not in result
    assert git(origin, "rev-parse", "HEAD").stdout.strip() == before


def test_resolve_repo_arg_never_resolves_against_daemon_cwd(
    tmp_path: Path, monkeypatch: Any, config: SupervisorConfig
) -> None:
    """v87-F1: a bare name is a slug, a workon binding, or an honest miss
    under the clone root — the serve process's CWD never participates."""
    from skep.supervisor.serve.registry import resolve_repo_arg

    root = tmp_path / "clones"
    root.mkdir()
    daemon_cwd = tmp_path / "daemon-cwd"
    (daemon_cwd / "my-workspace").mkdir(parents=True)  # the CWD decoy
    monkeypatch.chdir(daemon_cwd)
    store = RunStore(config.db_path)
    try:
        # Unknown bare name: lands under the clone root (so the downstream
        # error teaches list_repos), never on the decoy next to the daemon.
        assert resolve_repo_arg("my-workspace", root, store) == (root / "my-workspace").resolve()

        # A workon-bound directory resolves by its name.
        bound = tmp_path / "elsewhere" / "my-workspace"
        bound.mkdir(parents=True)
        store.add_project_policy(
            project_id="ws",
            name="ws",
            strategy="trusted_local_dev",
            phase="build",
            policy={},
        )
        store.add_project_binding(
            project_id="ws", binding_kind="repo_path", binding_value=str(bound)
        )
        assert resolve_repo_arg("my-workspace", root, store) == bound.resolve()

        # A registered clone slug still wins over a binding of the same name.
        (root / "my-workspace" / ".git").mkdir(parents=True)
        assert resolve_repo_arg("my-workspace", root, store) == (root / "my-workspace").resolve()

        # Explicit paths keep the documented host-path route.
        assert resolve_repo_arg(str(bound), root, store) == bound.resolve()
    finally:
        store.close()


def test_the_verify_pin_must_be_runnable_on_this_host(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v101-F14: `verify_command_seed` checked that the repo DECLARES an entry
    point and never that the supervisor can RUN it, breaking the rule its own
    docstring states. Run 019faa33 — claude_code on the skep project itself —
    re-verified with `make test` and got exit 127: `make` is not installed on
    that machine and has not been since v19. G10 was permanently inoperative on
    skep's own repo, every run NOT CONFIRMED.

    The irony is the whole fix: the target it could not reach was
    `test: uv run pytest`, exactly what the next branch pins."""
    from skep.supervisor.serve.registry import verify_command_seed

    (repo / "Makefile").write_text("test:\n\tuv run pytest\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (repo / "uv.lock").write_text("", encoding="utf-8")
    (repo / "tests").mkdir(exist_ok=True)

    present = {"make", "uv", "pytest", "npm"}
    monkeypatch.setattr(
        "skep.supervisor.serve.registry.shutil.which",
        lambda name: f"/usr/bin/{name}" if name in present else None,
    )
    assert verify_command_seed(repo) == "make test"

    # No make on the host: fall THROUGH to the runnable branch, do not pin a
    # command that exits 127. This is the live skep repo's exact shape.
    present.discard("make")
    assert verify_command_seed(repo) == "uv run pytest"

    # uv gone too — the bare pytest branch, still probed.
    present.discard("uv")
    assert verify_command_seed(repo) == "pytest"


def test_a_makefile_only_repo_infers_nothing_when_make_is_absent(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No runnable detection means NO pin and the worker-nominated fallback —
    which is weaker, and honest about being weaker. A pin that cannot run is
    not the stronger option; it is a gate that has stopped measuring."""
    from skep.supervisor.serve.registry import verify_command_seed

    (repo / "Makefile").write_text("test:\n\techo t\n", encoding="utf-8")
    monkeypatch.setattr("skep.supervisor.serve.registry.shutil.which", lambda _: None)
    assert verify_command_seed(repo) == ""
