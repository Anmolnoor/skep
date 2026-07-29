"""v51-F5: batch_dispatch — N independent governed runs, one card (ADR 0025).

The batch is a submission convenience, not an execution model: each task
gets its own worktree, policy compile, audit trail, and re-verification.
One card shows the whole batch; auto-resolve requires EVERY member to match
its project's auto-dispatch policy. Known v1 limitation (recorded in the
ADR): no partial approval — the operator approves all or none.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.serve.jobs import Dispatcher
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import (
    BATCH_DISPATCH_CAP,
    MUTATING_TOOL_NAMES,
    execute_mutation,
)

from .conftest import git, wait_terminal
from .fake_ollama import FakeOllama
from .test_serve_chat import sse_events
from .test_serve_chat_tools import chat_client


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


def _second_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo-two"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "other.py").write_text("value = 1\n")
    git(repo, "add", "other.py")
    git(repo, "commit", "-qm", "seed")
    return repo


def test_batch_dispatch_is_a_mutating_tool() -> None:
    assert "batch_dispatch" in MUTATING_TOOL_NAMES
    assert BATCH_DISPATCH_CAP == 3


def test_batch_cards_then_confirm_dispatches_all_in_parallel(
    repo: Path, tmp_path: Path, config: SupervisorConfig, ollama: FakeOllama
) -> None:
    repo_two = _second_repo(tmp_path)
    client, chat_id = chat_client(config, ollama)
    tasks = [
        {
            "repo": str(repo),
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "workspace",
        },
        {
            "repo": str(repo_two),
            "instructions": "Fix the other bug. MODE:happy",
            "execution_mode": "workspace",
        },
    ]
    ollama.script_tool_call("batch_dispatch", {"tasks": tasks})
    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "do both"}).text
    )
    actions = [d for name, d in events if name == "action"]
    assert len(actions) == 1  # ONE card for the whole batch
    assert actions[0]["tool"] == "batch_dispatch"
    # The card shows every task — no hidden dispatch.
    assert [t["repo"] for t in actions[0]["args"]["tasks"]] == [str(repo), str(repo_two)]
    assert events[-1] == ("done", {"state": "awaiting_confirmation"})

    # Nothing dispatched until the verdict.
    assert client.get("/api/runs").json()["runs"] == []

    action_id = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]
    ollama.script_reply("both dispatched")
    confirm = sse_events(client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm").text)
    assert confirm[-1] == ("done", {"state": "complete"})

    resolved = client.get(f"/api/chats/{chat_id}").json()["actions"][0]
    payload = resolved["result"]["result"]
    task_ids = payload["dispatched"]
    assert payload["count"] == 2
    assert len(set(task_ids)) == 2  # independent task identities

    # Each run completes independently, with its own repo and audit trail.
    runs = {task_id: wait_terminal(client, task_id) for task_id in task_ids}
    assert all(run["state"] == "completed" for run in runs.values())
    assert {run["repo"] for run in runs.values()} == {str(repo.resolve()), str(repo_two.resolve())}


def test_batch_auto_resolves_only_when_every_member_matches(
    repo: Path, tmp_path: Path, config: SupervisorConfig, ollama: FakeOllama
) -> None:
    repo_two = _second_repo(tmp_path)  # stays unbound → gates the batch
    client, chat_id = chat_client(config, ollama)
    created = client.post(
        "/api/projects",
        json={
            "project_id": "trusted-fixture",
            "name": "Trusted Fixture",
            "strategy": "trusted_local_dev",
            "phase": "build",
            "policy": {
                "default_execution_mode": "workspace",
                "auto_dispatch_allowed": True,
            },
            "bindings": [{"kind": "repo_path", "value": str(repo)}],
        },
    )
    assert created.status_code == 201

    # Trusted + unbound → the batch cards, naming the gated member.
    ollama.script_tool_call(
        "batch_dispatch",
        {
            "tasks": [
                {"repo": str(repo), "instructions": "A. MODE:happy"},
                {"repo": str(repo_two), "instructions": "B. MODE:happy"},
            ]
        },
    )
    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "batch"}).text
    )
    actions = [d for name, d in events if name == "action"]
    assert len(actions) == 1
    assert actions[0]["decision"]["reason"] == "dispatch.require_approval.batch_member_gated"
    assert "task 2/2" in actions[0]["decision"]["detail"]

    # All-trusted → no card, both dispatch inside the turn.
    chat_two = client.post("/api/chats", json={}).json()["chat_id"]
    ollama.script_tool_call(
        "batch_dispatch",
        {
            "tasks": [
                {"repo": str(repo), "instructions": "C. MODE:happy"},
                {"repo": str(repo), "instructions": "D. MODE:happy"},
            ]
        },
    )
    ollama.script_reply("running both")
    auto_events = sse_events(
        client.post(f"/api/chats/{chat_two}/messages", json={"content": "batch"}).text
    )
    assert [name for name, _ in auto_events].count("action") == 0
    tool_events = [d for name, d in auto_events if name == "tool"]
    payload = tool_events[0]["result"]["result"]
    assert payload["count"] == 2
    for task_id in payload["dispatched"]:
        assert wait_terminal(client, task_id)["state"] == "completed"

    # v61-F1: the auto-allowed batch still leaves an action row — born
    # resolved (no card in the replay), carrying the dispatch result so
    # chat_for_task can route the runs' terminal notifications.
    (recorded,) = client.get(f"/api/chats/{chat_two}").json()["actions"]
    assert recorded["tool"] == "batch_dispatch"
    assert recorded["status"] == "confirmed"
    assert recorded["decided_by"] == "dispatch.auto_allowed.batch_project_policy_match"
    store = RunStore(config.db_path)
    try:
        for task_id in payload["dispatched"]:
            assert store.chat_for_task(task_id) == chat_two
    finally:
        store.close()


def test_batch_validation_is_honest(repo: Path, config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        runner = Dispatcher(holder, store)

        def mutate(tasks: object) -> object:
            return execute_mutation(
                "batch_dispatch",
                {"tasks": tasks},
                store=store,
                holder=holder,
                runner=runner,
                actor="tester",
            )

        with pytest.raises(ValueError, match="non-empty array"):
            mutate([])
        with pytest.raises(ValueError, match=f"caps at {BATCH_DISPATCH_CAP}"):
            mutate(
                [{"repo": str(repo), "instructions": "x. MODE:happy"}] * (BATCH_DISPATCH_CAP + 1)
            )
        with pytest.raises(ValueError, match="repo and instructions"):
            mutate([{"repo": str(repo)}])
        # Nothing dispatched by any rejected batch.
        assert store.recent_runs(10) == []
    finally:
        store.close()


def test_a_member_engine_reaches_that_member_only(repo: Path, config: SupervisorConfig) -> None:
    """v98-F1: the seed orchestrate-cli-coding-agent told the Queen to send one
    brief to different backends; the schema had no engine, so it could not.
    Per-member engine now reaches submit_run for that member alone."""
    store = RunStore(config.db_path)
    seen: list[object] = []
    try:
        holder = ConfigHolder(config, store)
        runner = Dispatcher(holder, store)
        import skep.supervisor.serve.actions as actions_mod

        real = actions_mod.submit_run

        def spy(*args: object, **kwargs: object) -> object:
            seen.append(kwargs.get("engine"))
            return real(*args, **kwargs)  # type: ignore[arg-type]

        actions_mod.submit_run = spy  # type: ignore[assignment]
        try:
            execute_mutation(
                "batch_dispatch",
                {
                    "tasks": [
                        {
                            "repo": str(repo),
                            "instructions": "A. MODE:happy",
                            "execution_mode": "workspace",
                        },
                        {
                            "repo": str(repo),
                            "instructions": "B. MODE:happy",
                            "execution_mode": "workspace",
                            "engine": "builtin",
                        },
                    ]
                },
                store=store,
                holder=holder,
                runner=runner,
                actor="tester",
            )
        finally:
            actions_mod.submit_run = real
        assert seen == [None, "builtin"]  # unset stays unset; no leak sideways
    finally:
        store.close()


def test_a_member_naming_an_unknown_engine_fails_closed(
    repo: Path, config: SupervisorConfig
) -> None:
    """resolve_engine's refusal (engines.py) is the only gate needed — the
    batch takes the single-dispatch path, so it names the known set too (I9)."""
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        runner = Dispatcher(holder, store)
        with pytest.raises(Exception, match="claude_code"):
            execute_mutation(
                "batch_dispatch",
                {
                    "tasks": [
                        {
                            "repo": str(repo),
                            "instructions": "A. MODE:happy",
                            "execution_mode": "workspace",
                            "engine": "gpt5",
                        },
                    ]
                },
                store=store,
                holder=holder,
                runner=runner,
                actor="tester",
            )
    finally:
        store.close()


def test_an_explicit_member_engine_cards_the_whole_batch(
    repo: Path, config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """v95-F3's rule survives the batch: an explicit engine is an explicit run
    override, so it cards even where the project auto-dispatches (I6/I7)."""
    client, chat_id = chat_client(config, ollama)
    created = client.post(
        "/api/projects",
        json={
            "project_id": "trusted-fixture",
            "name": "Trusted Fixture",
            "strategy": "trusted_local_dev",
            "phase": "build",
            "policy": {
                "default_execution_mode": "workspace",
                "auto_dispatch_allowed": True,
            },
            "bindings": [{"kind": "repo_path", "value": str(repo)}],
        },
    )
    assert created.status_code == 201

    ollama.script_tool_call(
        "batch_dispatch",
        {
            "tasks": [
                {"repo": str(repo), "instructions": "A. MODE:happy"},
                {"repo": str(repo), "instructions": "B. MODE:happy", "engine": "builtin"},
            ]
        },
    )
    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "compare"}).text
    )
    actions = [d for name, d in events if name == "action"]
    assert len(actions) == 1
    assert actions[0]["decision"]["reason"] == "dispatch.require_approval.batch_member_gated"
    assert "task 2/2" in actions[0]["decision"]["detail"]
    assert "explicit_run_overrides" in actions[0]["decision"]["detail"]


def test_batch_members_get_separate_audit_trails(
    repo: Path, config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """Per-task task.json in the audit dir — the batch never merges evidence."""
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call(
        "batch_dispatch",
        {
            "tasks": [
                {
                    "repo": str(repo),
                    "instructions": "One. MODE:happy",
                    "execution_mode": "workspace",
                },
                {
                    "repo": str(repo),
                    "instructions": "Two. MODE:happy",
                    "execution_mode": "workspace",
                },
            ]
        },
    )
    sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "go"}).text)
    action_id = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]
    ollama.script_reply("done")
    sse_events(client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm").text)
    resolved = client.get(f"/api/chats/{chat_id}").json()["actions"][0]
    task_ids = resolved["result"]["result"]["dispatched"]
    for task_id in task_ids:
        wait_terminal(client, task_id)
        task_json = config.audit_dir / task_id / "task.json"
        assert task_json.is_file()
        envelope = json.loads(task_json.read_text())
        assert envelope["task_id"] == task_id
    instructions = {
        json.loads((config.audit_dir / task_id / "task.json").read_text())["instructions"]
        for task_id in task_ids
    }
    assert instructions == {"One. MODE:happy", "Two. MODE:happy"}
