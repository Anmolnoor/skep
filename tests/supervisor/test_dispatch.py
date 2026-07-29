"""End-to-end dispatch tests against the fake worker (hermetic: no external worker, no LLM)."""

from __future__ import annotations

import errno
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from skep.supervisor import (
    RunStore,
    SupervisorConfig,
    create_worktree,
    mint_task,
    remove_worktree,
    run_task,
    sandbox,
    spawn_worker,
)
from skep.worker_contract import Budget, Permissions

from .conftest import git


def _no_leftovers(repo: Path, config: SupervisorConfig) -> None:
    worktrees = list(config.worktrees_root.iterdir()) if config.worktrees_root.is_dir() else []
    assert worktrees == [], f"leftover worktrees: {worktrees}"
    listed = git(repo, "worktree", "list", "--porcelain").stdout
    assert listed.count("worktree ") == 1, f"git still tracks extra worktrees:\n{listed}"


def test_run_task_baseline_is_default_branch_not_operator_checkout(
    repo: Path, config: SupervisorConfig
) -> None:
    """v22-F1: the worktree baseline is the repo's default branch, regardless of
    which branch the operator's checkout happens to sit on."""
    default = git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip()
    git(repo, "checkout", "-b", "scratch")
    (repo / "scratch.txt").write_text("stray work\n", encoding="utf-8")
    git(repo, "add", "scratch.txt")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "scratch work")

    outcome = run_task(repo, "Fix the bug. MODE:happy", config=config)

    assert outcome.record.state == "completed"
    # The resolved baseline is recorded on the run, not left as None/HEAD.
    assert outcome.record.ref == default
    # The operator's checkout is untouched.
    assert git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip() == "scratch"


def test_happy_dispatch_produces_verified_run_record(repo: Path, config: SupervisorConfig) -> None:
    outcome = run_task(repo, "Fix the bug. MODE:happy", config=config)
    record = outcome.record

    assert record.state == "completed"
    assert record.summary == "fixed and verified"
    assert record.verification_outcome == "passed"
    assert record.worker_version == "fake-1.0"
    assert record.manifest_fingerprint == "f" * 64

    store = RunStore(config.db_path)
    try:
        artifacts = store.artifacts_for(record.task_id)
        kinds = {kind for kind, _, _ in artifacts}
        assert kinds == {"event_log", "patch"}
        for _, audit_path, _ in artifacts:
            assert Path(audit_path).is_file(), f"audit copy missing: {audit_path}"
        events = store.events_for(record.task_id)
        assert events[0].type.value == "task.start"
        assert events[-1].type.value == "task.terminal"
        transitions = [state for state, _, _ in store.transitions_for(record.task_id)]
        assert transitions == ["created", "dispatched", "running", "completed"]
        assert store.commands_for(record.task_id) == [
            ('grep -q "value = 1" existing.py', 0, "verify")
        ]
    finally:
        store.close()

    # The audited task.json and result.json survive teardown.
    audit_dir = config.audit_dir / record.task_id
    assert (audit_dir / "task.json").is_file()
    assert (audit_dir / "result.json").is_file()
    assert (audit_dir / "events.ndjson").is_file()

    # Source repo untouched; no worktrees or temp dirs left behind.
    assert (repo / "existing.py").read_text() == "value = 0\n"
    _no_leftovers(repo, config)


def test_pending_approval_enqueues_review(repo: Path, config: SupervisorConfig) -> None:
    outcome = run_task(repo, "Commit this. MODE:pending", config=config)

    assert outcome.record.state == "pending_approval"
    assert outcome.review_id is not None
    store = RunStore(config.db_path)
    try:
        pending = store.pending_approvals()
        assert len(pending) == 1
        assert pending[0].review_id == outcome.review_id
        assert pending[0].action == "git_commit"
        assert pending[0].status == "pending"
    finally:
        store.close()
    # The suspended run's worktree is preserved so an approved resume can
    # continue in-place (it is reclaimed once the gate approval resolves).
    preserved = config.worktrees_root / outcome.record.task_id
    assert preserved.is_dir(), "pending_approval must preserve its worktree"


def test_pending_worktree_survives_unrelated_runs_sweeps(
    repo: Path, config: SupervisorConfig
) -> None:
    pending = run_task(repo, "Commit this. MODE:pending", config=config)
    preserved = config.worktrees_root / pending.record.task_id
    assert preserved.is_dir()

    happy = run_task(repo, "Do work. MODE:happy", config=config)

    assert happy.record.state == "completed"
    assert preserved.is_dir(), "unrelated runs' sweeps must spare pending-gate worktrees"


def test_denied_gate_worktree_is_reclaimed_by_next_sweep(
    repo: Path, config: SupervisorConfig
) -> None:
    pending = run_task(repo, "Commit this. MODE:pending", config=config)
    preserved = config.worktrees_root / pending.record.task_id
    assert preserved.is_dir()

    store = RunStore(config.db_path)
    try:
        assert pending.review_id is not None
        store.resolve_approval(pending.review_id, approved=False, actor="tester")
    finally:
        store.close()
    run_task(repo, "Do work. MODE:happy", config=config)

    assert not preserved.exists(), "a denied gate's worktree must fall to the next sweep"


def test_crash_synthesizes_worker_crashed(repo: Path, config: SupervisorConfig) -> None:
    outcome = run_task(repo, "Do work. MODE:crash", config=config)

    assert outcome.record.state == "worker_crashed"
    store = RunStore(config.db_path)
    try:
        events = store.events_for(outcome.record.task_id)
        terminal = events[-1]
        assert terminal.type.value == "task.terminal"
        assert terminal.payload["status"] == "worker_crashed"
        assert terminal.payload["synthesized"] is True
        assert terminal.payload["exit_code"] == 9
    finally:
        store.close()
    # The synthesized terminal is also in the audit event-log copy.
    audit_events = (config.audit_dir / outcome.record.task_id / "events.ndjson").read_text()
    last = json.loads(audit_events.splitlines()[-1])
    assert last["payload"]["synthesized"] is True
    _no_leftovers(repo, config)


@pytest.mark.parametrize("mode", ["noresult", "badresult"])
def test_completed_terminal_without_valid_result_fails_evidence_chain(
    repo: Path, config: SupervisorConfig, mode: str
) -> None:
    outcome = run_task(repo, f"Do work. MODE:{mode}", config=config)

    assert outcome.record.state == "failed"
    store = RunStore(config.db_path)
    try:
        transitions = store.transitions_for(outcome.record.task_id)
    finally:
        store.close()
    assert "result envelope" in str(transitions[-1][1])
    _no_leftovers(repo, config)


def test_hang_is_killed_and_synthesized_as_timeout(repo: Path, config: SupervisorConfig) -> None:
    budget = Budget(wall_clock_seconds=60, max_iterations=4, max_actions=10, max_provider_calls=8)
    outcome = run_task(repo, "Do work. MODE:hang", config=config, budget=budget)

    assert outcome.record.state == "worker_timeout"
    store = RunStore(config.db_path)
    try:
        terminal = store.events_for(outcome.record.task_id)[-1]
        assert terminal.payload["status"] == "worker_timeout"
        assert terminal.payload["synthesized"] is True
        assert terminal.payload["reason"] == "heartbeat_lost"
    finally:
        store.close()
    _no_leftovers(repo, config)


@pytest.mark.skipif(
    sandbox.availability().backend == "bubblewrap",
    reason="asserts Seatbelt SBPL profile content; bubblewrap evidence is argv JSON",
)
def test_sandbox_profile_allows_git_worktree_metadata(
    repo: Path,
    config: SupervisorConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = create_worktree(repo, config.worktrees_root, "task-git-metadata")
    try:
        task = mint_task(workspace=workspace, instructions="inspect git metadata")
        worker = tmp_path / "worker.py"
        worker.write_text("import sys\nsys.exit(0)\n")
        task_path = workspace / ".events" / "task.json"
        result_path = config.results_dir / f"{task.task_id}.json"
        log_path = config.audit_dir / task.task_id / "worker.log"
        sandbox_config = replace(config, worker_command=(sys.executable, str(worker)))

        monkeypatch.setattr(sandbox, "available", lambda: True)
        monkeypatch.setattr(
            sandbox, "wrap_command", lambda argv, _profile_path, backend=None: list(argv)
        )

        proc = spawn_worker(
            sandbox_config,
            task,
            task_path,
            result_path,
            log_path=log_path,
        )
        proc.wait(timeout=5)

        git_dir = _git_path(workspace, "--git-dir")
        common_dir = _git_path(workspace, "--git-common-dir")
        profile = (log_path.parent / "sandbox.profile.sb").read_text()
        assert f'(subpath "{git_dir}")' in profile
        assert f'(subpath "{common_dir}")' in profile
    finally:
        remove_worktree(repo, workspace)


def _git_path(workspace: Path, arg: str) -> Path:
    raw = git(workspace, "rev-parse", arg).stdout.strip()
    path = Path(raw)
    if not path.is_absolute():
        path = workspace / path
    return path.resolve()


@pytest.mark.skipif(
    sandbox.availability().backend != "seatbelt",
    reason="Seatbelt proof runs only on macOS",
)
def test_every_dispatched_run_is_sandboxed_with_the_profile_recorded(
    repo: Path, config: SupervisorConfig
) -> None:
    """Q1: the dispatch path physically sandboxes the worker; the exact profile is evidence."""
    outcome = run_task(repo, "Fix the bug. MODE:happy", config=config)
    profile = config.audit_dir / outcome.record.task_id / "sandbox.profile.sb"
    assert profile.is_file(), "the enforced Seatbelt profile was not recorded as evidence"
    text = profile.read_text()
    assert "(deny network*)" in text, "deny-all network was not enforced"
    assert "(deny file-write*)" in text, "writes were not confined"
    _no_leftovers(repo, config)


@pytest.mark.skipif(
    sandbox.availability().backend != "seatbelt",
    reason="Seatbelt proof runs only on macOS",
)
def test_dispatched_worker_cannot_open_an_outbound_socket(
    repo: Path, config: SupervisorConfig
) -> None:
    """Q1 end-to-end: a worker's outbound connection is physically refused (EPERM)."""
    outcome = run_task(repo, "Probe the network. MODE:netprobe", config=config)
    assert outcome.record.state == "completed"
    probe_path = config.results_dir / f"netprobe-{outcome.record.task_id}.json"
    probe = json.loads(probe_path.read_text())
    assert probe["connected"] is False, "the worker reached the network — boundary is broken"
    assert probe["errno"] == errno.EPERM, (
        f"expected EPERM from the sandbox, got errno={probe['errno']} "
        "(a non-EPERM failure would not prove the sandbox blocked it)"
    )


def test_env_allowlist_is_a_boundary(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G2 v1 acceptance criterion: a parent-env canary never reaches the child."""
    monkeypatch.setenv("CANARY_SECRET", "leak-me-if-you-can")
    monkeypatch.setenv("ALLOWED_PROVIDER_KEY", "ok-to-pass")
    permissions = Permissions(
        read=["workspace"],
        write=["workspace"],
        network=[],
        env_allowlist=["ALLOWED_PROVIDER_KEY"],
    )

    outcome = run_task(repo, "Dump env. MODE:envdump", config=config, permissions=permissions)
    assert outcome.record.state == "completed"

    # The fake worker writes the dump next to --out so it survives teardown.
    dump_path = config.results_dir / f"envdump-{outcome.record.task_id}.json"
    assert dump_path.is_file(), "envdump evidence missing"
    child_env = json.loads(dump_path.read_text())
    assert "CANARY_SECRET" not in child_env
    assert child_env.get("ALLOWED_PROVIDER_KEY") == "ok-to-pass"
    assert "PATH" in child_env and "HOME" in child_env
    assert os.environ["CANARY_SECRET"] == "leak-me-if-you-can"


# ---------- v13 Step 8: curated-memory injection ----------


def test_resolve_injected_memory_is_project_scoped(config: SupervisorConfig) -> None:
    from skep.supervisor.policy_resolver import resolve_injected_memory
    from skep.worker_contract import ProjectContextPayload

    store = RunStore(config.db_path)
    try:
        glob = store.add_memory_item(
            memory_class="durable_preference", content="global pref", actor="s"
        )
        p1 = store.add_memory_item(
            memory_class="project_fact", content="p1 fact", actor="s", project_id="proj-1"
        )
        p2 = store.add_memory_item(
            memory_class="project_fact", content="p2 fact", actor="s", project_id="proj-2"
        )
        pc = ProjectContextPayload(
            project_id="proj-1",
            name="n",
            strategy="trusted_local_dev",
            phase="maintain",
            binding_kind="repo_path",
            binding_value="/x",
        )
        bound = {m.memory_id for m in resolve_injected_memory(store, pc)}
        assert glob.memory_id in bound
        assert p1.memory_id in bound
        assert p2.memory_id not in bound  # other project's memory never leaks

        unbound = {m.memory_id for m in resolve_injected_memory(store, None)}
        assert unbound == {glob.memory_id}  # unbound run sees only global memory
    finally:
        store.close()


def test_run_injects_approved_memory_into_task_envelope(
    repo: Path, config: SupervisorConfig
) -> None:
    store = RunStore(config.db_path)
    try:
        item = store.add_memory_item(
            memory_class="durable_preference", content="Prefer uv over pip", actor="s"
        )
    finally:
        store.close()

    outcome = run_task(repo, "Fix the bug. MODE:happy", config=config)
    task_json = json.loads(
        (config.audit_dir / outcome.record.task_id / "task.json").read_text()
    )
    # The audit copy of the task envelope records exactly what memory was injected.
    assert [m["memory_id"] for m in task_json["memory"]] == [item.memory_id]
    assert task_json["memory"][0]["content"] == "Prefer uv over pip"


def test_pending_proposals_are_never_injected(repo: Path, config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        store.create_memory_proposal(
            memory_class="project_fact", content="unapproved", actor="s"
        )
    finally:
        store.close()
    outcome = run_task(repo, "Fix the bug. MODE:happy", config=config)
    task_json = json.loads(
        (config.audit_dir / outcome.record.task_id / "task.json").read_text()
    )
    assert task_json["memory"] == []  # only approved (durable) memory is injected


def test_injected_memory_reaches_the_worker_prompt_as_context() -> None:
    from skep.worker_contract import MemoryContextEntry
    from skep.workers.llm_plan import _memory_block

    block = _memory_block(
        [
            MemoryContextEntry(
                memory_id="m1", memory_class="durable_preference", content="Prefer uv over pip"
            )
        ]
    )
    assert "Prefer uv over pip" in block
    assert "NOT authority" in block  # worker sees memory as context, not authority
    assert _memory_block([]) == ""
