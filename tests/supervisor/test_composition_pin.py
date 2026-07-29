"""v73-F6: R9 closed — the pinned end-to-end composition record.

The machinery has existed since v51 (batch_dispatch) and v71 (await_runs);
what R9 lacked was a PINNED record of the whole loop: three independent
workers fan out in separate worktrees, the collect half gathers all three,
each piece lands through its OWN approval on its OWN branch, and the
synthesis COMPOSES results — main never advances, no worker writes outside
its worktree (I3).
"""

from __future__ import annotations

import re
from pathlib import Path

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.serve.jobs import Dispatcher
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import (
    BATCH_DISPATCH_CAP,
    execute_mutation,
    execute_read_tool,
)

from .conftest import git, serve_client


def test_r9_composition_three_workers_three_approvals_compose_not_merge(
    repo: Path, config: SupervisorConfig
) -> None:
    parts = ("part_a.py", "part_b.py", "part_c.py")
    for name in parts:
        (repo / name).write_text("value = 0\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "seed parts")
    baseline = git(repo, "rev-parse", "HEAD").stdout.strip()

    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="composed",
            name="composed",
            strategy="trusted_local_dev",
            phase="build",
            policy={
                "default_execution_mode": "workspace",
                "auto_apply_verified_patch": False,
            },
        )
        store.add_project_binding(
            project_id="composed", binding_kind="repo_path", binding_value=str(repo)
        )
        holder = ConfigHolder(config, store)
        batch = execute_mutation(
            "batch_dispatch",
            {
                "tasks": [
                    {
                        "repo": str(repo),
                        "instructions": f"Part {index}. MODE:happy FILE:{name}",
                        "execution_mode": "workspace",
                    }
                    for index, name in enumerate(parts)
                ]
            },
            store=store,
            holder=holder,
            runner=Dispatcher(holder, store),
            actor="tester",
        )
        task_ids = [str(task_id) for task_id in batch["dispatched"]]
        assert len(task_ids) == BATCH_DISPATCH_CAP == 3

        # The collect half: await_runs blocks until every member settles.
        collected = execute_read_tool(
            "await_runs",
            {"task_ids": task_ids, "timeout_seconds": 120},
            store=store,
            holder=holder,
        )
        assert collected["settled"] is True
        assert [run["state"] for run in collected["runs"]] == ["completed"] * 3

        # I3: no shared mutable state — one worktree per worker.
        workspaces = set()
        for task_id in task_ids:
            record = store.get_run(task_id)
            assert record is not None and record.workspace
            workspaces.add(record.workspace)
        assert len(workspaces) == 3
    finally:
        store.close()

    # Each piece lands through its OWN approval — landing IS the commit.
    client = serve_client(config)
    for task_id in task_ids:
        landed = client.post(f"/api/runs/{task_id}/land", json={"actor": "operator"})
        assert landed.status_code == 200
        assert landed.json()["branch"] == f"skep/{task_id}"

    verify = RunStore(config.db_path)
    try:
        for task_id in task_ids:
            approvals = verify.approvals_for(task_id)
            assert [a.status for a in approvals] == ["approved"]
            assert approvals[0].landing_branch == f"skep/{task_id}"
        # Queen-side synthesis is plain composition over the three results.
        summaries = [verify.get_run(task_id) for task_id in task_ids]
        assert all(r is not None and r.summary == "fixed and verified" for r in summaries)
    finally:
        verify.close()

    # Compose, don't merge: the default branch never advanced, and each
    # landing branch carries exactly its own piece.
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == baseline
    changed_per_branch = [
        tuple(git(repo, "diff", "--name-only", f"{baseline}..skep/{task_id}").stdout.split())
        for task_id in task_ids
    ]
    assert all(len(changed) == 1 for changed in changed_per_branch)
    assert {changed[0] for changed in changed_per_branch} == set(parts)


def test_part_ii_backlog_tells_the_truth() -> None:
    """v73-F6 (I8): no Part II entry may claim it waits on a record that
    exists — R1 through R12 are all landed, and say so."""
    text = (Path(__file__).resolve().parents[2] / "docs" / "invariants.md").read_text()
    for number in range(1, 13):
        header = re.search(rf"\*\*R{number} \(([^)]*)\)", text)
        assert header is not None, f"R{number} has no status parenthetical"
        assert "landed" in header.group(1), f"R{number} not marked landed"
    part_ii = text[text.index("**R1 ") :]
    assert "waits on" not in part_ii
