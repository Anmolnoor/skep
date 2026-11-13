"""v109-F7: approve-and-remember reaches network approvals, per project.

The field ledger holds the same install family approved repeatedly in one
workspace with `remembered=0` on every row: a network approval was
approve-once or resume-grant only — nothing could say "this host is fine for
this project, stop asking". The blocked hostname rides the approval
decision's detail (the slot the resume verdict already grants from, v90-F3);
allow-host stores it as a project grant bound to the repo's project (the
repo itself when unbound; W28, B18's network half — never the
``default_network`` knob a locked baseline ignores) and resumes the gated run
with ``remembered=True`` so the ledger row finally says so (I13).
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
                        "event_id": f"approval-requested-{task.task_id}",
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


def _network_grants(store: RunStore) -> list[tuple[str, dict[str, str]]]:
    from skep.supervisor.policy_schema import POLICY_DOCUMENT_SETTINGS_KEY, document_from_settings

    document = document_from_settings(store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY))
    learned = [] if document is None else document.learned
    return [
        (rule.pattern, {str(k): v for k, v in rule.bound.items()})
        for rule in learned
        if rule.scope == "network"
    ]


def test_persist_binds_a_grant_to_the_repos_project(repo: Path, config: SupervisorConfig) -> None:
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
        where = actions.persist_remembered_network_host(store, holder, repo, "docs.example.com")
        # Idempotent — a second remember writes nothing new.
        actions.persist_remembered_network_host(store, holder, repo, "docs.example.com")
        assert where == "project:proj-net"
        assert _network_grants(store) == [("docs.example.com", {"project_id": "proj-net"})]
        project = store.project_for_binding("repo_path", str(repo))
        assert project is not None
        assert project.policy["default_network"] == ["pypi.org"]  # the knob is untouched
    finally:
        store.close()


def test_persist_binds_an_unbound_repos_grant_to_the_repo(
    repo: Path, config: SupervisorConfig
) -> None:
    from skep.supervisor.serve.settings import DEFAULT_NETWORK

    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        where = actions.persist_remembered_network_host(store, holder, repo, "docs.example.com")
        assert where == f"repo:{repo.resolve()}"
        assert _network_grants(store) == [("docs.example.com", {"repo": str(repo.resolve())})]
        assert "docs.example.com" not in (store.get_setting(DEFAULT_NETWORK) or [])
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
        assert _network_grants(store) == [("docs.example.com", {"project_id": "proj-net"})]
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


def test_every_allow_host_receipt_says_where_the_grant_holds(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W28: REST, the chat tool and the ledger remember name the binding."""
    from skep.supervisor.serve.tools import execute_mutation

    from .conftest import serve_client

    monkeypatch.setattr(actions, "resume_past_gate", lambda *a, **k: "resumed-1")
    client = serve_client(config)
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        where = f"repo:{repo.resolve()}"
        _task, review = _pending_network_run(store, repo)
        response = client.post(f"/api/approvals/{review}/allow-host", json={})
        assert response.status_code == 200, response.text
        assert response.json()["remembered"] == where
        _task, review = _pending_network_run(store, repo, host="api.example.com")
        receipt = execute_mutation(
            "allow_command_review",
            {"review_id": review},
            store=store,
            holder=holder,
            runner=Dispatcher(holder, store),
            actor="chat-user",
        )
        assert receipt["remembered"] == where
        ledger = actions.remember_ledger_entry(
            store, holder, action="network.fetch", resource="cdn.example.com", repo=str(repo)
        )
        assert ledger["holds"] == where
    finally:
        store.close()
