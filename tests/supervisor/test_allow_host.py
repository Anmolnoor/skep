"""v109-F7: approve-and-remember reaches network approvals, per project.

The field ledger holds the same install family approved repeatedly in one
workspace with `remembered=0` on every row: a network approval was
approve-once or resume-grant only — nothing could say "this host is fine for
this project, stop asking". The blocked hostname rides the approval
decision's detail (the slot the resume verdict already grants from, v90-F3);
allow-host persists it into the bound project's ``default_network`` (global
when unbound) and resumes the gated run with ``remembered=True`` so the
ledger row finally says so (I13).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.contracts_io import mint_task
from skep.supervisor.serve import actions
from skep.supervisor.serve.jobs import Dispatcher
from skep.supervisor.serve.settings import ConfigHolder
from skep.worker_contract import CONTRACT_VERSION, Event


def _pending_network_run(
    store: RunStore, repo: Path, *, host: str | None = "docs.example.com"
) -> tuple[str, str]:
    """A run suspended on a network.fetch approval; returns (task_id, review_id)."""
    task = mint_task(workspace=repo, instructions="Fetch the metadata.")
    store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
    if host is not None:
        store.ingest_events(
            [
                Event.model_validate(
                    {
                        "contract_version": CONTRACT_VERSION,
                        "event_id": "approval-requested-1",
                        "seq": 1,
                        "task_id": task.task_id,
                        "trace_id": task.trace_id,
                        "ts": "2026-08-05T00:00:00Z",
                        "type": "approval.requested",
                        "payload": {
                            "action": "network.fetch",
                            "reason": (
                                "network.fetch requires approval with a task network allowlist"
                            ),
                            "decision": {
                                "verdict": "require_approval",
                                "reason": "capability.require_approval.network_allowlist_missing",
                                "detail": host,
                            },
                        },
                    }
                )
            ]
        )
    review_id = store.enqueue_approval(
        task.task_id,
        action="network.fetch",
        reason="network.fetch requires approval with a task network allowlist",
    )
    store.transition(task.task_id, "pending_approval", "gated on network.fetch")
    return task.task_id, review_id


def test_persist_prefers_the_bound_project_policy(repo: Path, config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="proj-net",
            name="net project",
            strategy="trusted_local_dev",
            phase="build",
            policy={"default_network": ["pypi.org"]},
        )
        store.add_project_binding(
            project_id="proj-net", binding_kind="repo_path", binding_value=str(repo)
        )
        holder = ConfigHolder(config, store)
        actions._persist_remembered_network_host(store, holder, repo, "docs.example.com")
        # Idempotent — a second remember writes nothing new.
        actions._persist_remembered_network_host(store, holder, repo, "docs.example.com")
        project = store.project_for_binding("repo_path", str(repo))
        assert project is not None
        assert project.policy["default_network"] == ["pypi.org", "docs.example.com"]
    finally:
        store.close()


def test_persist_falls_back_to_the_global_setting_for_unbound_repos(
    repo: Path, config: SupervisorConfig
) -> None:
    from skep.supervisor.serve.settings import DEFAULT_NETWORK

    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        actions._persist_remembered_network_host(store, holder, repo, "docs.example.com")
        stored = store.get_setting(DEFAULT_NETWORK)
        assert isinstance(stored, list) and "docs.example.com" in stored
    finally:
        store.close()


def test_allow_host_refuses_non_network_approvals(repo: Path, config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        task = mint_task(workspace=repo, instructions="x")
        store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
        review_id = store.enqueue_approval(
            task.task_id, action="shell.run", reason="shell.run requires approval for command: ls"
        )
        store.transition(task.task_id, "pending_approval", "gated")
        run = actions.require_run(store, task.task_id)
        approval = actions.pending_approval_or_409(store, review_id)
        with pytest.raises(HTTPException) as refused:
            actions.allow_network_host_and_resume(
                store, holder, Dispatcher(holder, store), run, approval, review_id, "tester"
            )
        assert refused.value.status_code == 409
        # I9: the refusal names the right verb for the shape it got.
        assert "allow-command" in refused.value.detail
    finally:
        store.close()


def test_allow_host_refuses_when_no_hostname_is_recorded(
    repo: Path, config: SupervisorConfig
) -> None:
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        task_id, review_id = _pending_network_run(store, repo, host=None)
        run = actions.require_run(store, task_id)
        approval = actions.pending_approval_or_409(store, review_id)
        with pytest.raises(HTTPException) as refused:
            actions.allow_network_host_and_resume(
                store, holder, Dispatcher(holder, store), run, approval, review_id, "tester"
            )
        assert refused.value.status_code == 409
        assert "approve it once" in refused.value.detail
    finally:
        store.close()


def test_allow_host_never_remembers_the_wildcard(repo: Path, config: SupervisorConfig) -> None:
    """The wildcard is the trust ramp, not a host (ADR 0048's spirit)."""
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        task_id, review_id = _pending_network_run(store, repo, host="*")
        run = actions.require_run(store, task_id)
        approval = actions.pending_approval_or_409(store, review_id)
        with pytest.raises(HTTPException) as refused:
            actions.allow_network_host_and_resume(
                store, holder, Dispatcher(holder, store), run, approval, review_id, "tester"
            )
        assert refused.value.status_code == 409
    finally:
        store.close()


def test_allow_host_persists_and_resumes_remembered(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: the host lands in the project policy and the resume rides
    the existing gate machinery with remembered=True (the ledger row will say
    so — resolve_approval threads it, same as the shell remember)."""
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="proj-net",
            name="net project",
            strategy="trusted_local_dev",
            phase="build",
            policy={},
        )
        store.add_project_binding(
            project_id="proj-net", binding_kind="repo_path", binding_value=str(repo)
        )
        holder = ConfigHolder(config, store)
        task_id, review_id = _pending_network_run(store, repo)
        run = actions.require_run(store, task_id)
        approval = actions.pending_approval_or_409(store, review_id)

        seen: dict[str, object] = {}

        def fake_resume(*args: object, **kwargs: object) -> str:
            seen["review_id"] = args[4]
            seen["remembered"] = kwargs.get("remembered")
            return "resumed-task-1"

        monkeypatch.setattr(actions, "resume_past_gate", fake_resume)
        resumed = actions.allow_network_host_and_resume(
            store, holder, Dispatcher(holder, store), run, approval, review_id, "tester"
        )
        assert resumed == "resumed-task-1"
        assert seen == {"review_id": review_id, "remembered": True}
        project = store.project_for_binding("repo_path", str(repo))
        assert project is not None
        assert project.policy["default_network"] == ["docs.example.com"]
    finally:
        store.close()


def test_allow_command_review_routes_network_approvals() -> None:
    """One chat tool for both shapes (the fresh-chat tool floor is a hard
    24KB ratchet, and a small model does better with one 'allow this review'
    verb) — the arm routes by the approval's action; the description says so."""
    from skep.supervisor.serve.cards import risk
    from skep.supervisor.serve.tools import MUTATING_TOOL_NAMES, tool_description

    assert "allow_command_review" in MUTATING_TOOL_NAMES
    description = tool_description("allow_command_review")
    assert "network" in description and "host" in description
    line = risk("allow_command_review", {"review_id": "r"})
    assert line is not None and "without asking" in line
