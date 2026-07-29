"""D2: the `audit` caste worker and its dispatch through the supervisor.

The unit tests pin the deterministic audit logic; the dispatch test proves the
supervisor treats a *second caste* exactly like the coding worker — caste routing
spawns it, the contract lifecycle runs, and G10 re-verification independently
confirms its result. This is the U1 spine, fully offline (no provider, no network).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.dispatch import run_task
from skep.workers.audit import Bump, apply_bumps, load_advisories, scan_requirements

from ..fixtures.toy_repo import create_audit_toy_repo


def test_scan_flags_only_pins_below_safe_version() -> None:
    advisories = {"requests": "2.31.0", "urllib3": "2.0.7"}
    text = "click==8.1.7\nrequests==2.28.0\nurllib3==2.0.7\n"
    bumps = scan_requirements(text, advisories)
    # requests is below safe -> flagged; urllib3 is exactly safe -> not; click absent.
    assert bumps == [Bump(package="requests", old="2.28.0", new="2.31.0")]


def test_apply_bumps_rewrites_only_flagged_lines() -> None:
    text = "click==8.1.7\nrequests==2.28.0\n"
    result = apply_bumps(text, [Bump("requests", "2.28.0", "2.31.0")])
    assert result == "click==8.1.7\nrequests==2.31.0\n"


def test_unpinned_and_commented_lines_are_left_alone() -> None:
    advisories = {"requests": "2.31.0"}
    text = "requests  # pinned elsewhere\nrequests>=2.0\n"
    assert scan_requirements(text, advisories) == []


def test_load_advisories_built_in_has_requests() -> None:
    assert load_advisories().get("requests") == "2.31.0"


@pytest.fixture()
def audit_config(tmp_path: Path) -> SupervisorConfig:
    return SupervisorConfig(
        home=tmp_path / "skep-home",
        worker_command=("false",),  # the coding worker is never invoked here
        caste_worker_commands={"audit": (sys.executable, "-m", "skep.workers.audit")},
        grace_seconds=5.0,
        heartbeat_seconds=10.0,
        poll_seconds=0.02,
    )


def test_audit_caste_dispatch_end_to_end(tmp_path: Path, audit_config: SupervisorConfig) -> None:
    repo = create_audit_toy_repo(tmp_path / "audit-repo")

    outcome = run_task(
        repo,
        "Audit dependencies and bump anything with a known advisory.",
        config=audit_config,
        worker_kind="audit",
    )
    record = outcome.record

    assert record.state == "completed", record.summary
    assert record.verification_outcome == "passed"
    assert record.worker_version == "audit-0.1.0"  # G7 identity from a non-coding caste

    # The supervisor independently re-verified the audit's claim (G10) and confirmed it.
    from skep.supervisor.store import RunStore

    db = RunStore(audit_config.db_path)
    try:
        reverify = db.reverification_for(record.task_id)
        assert reverify is not None and reverify.confirmed, "G10 should confirm the audit"
        artifacts = {kind for kind, _, _ in db.artifacts_for(record.task_id)}
        assert {"event_log", "patch"} <= artifacts
    finally:
        db.close()

    # The evidence (changed files) reflects exactly the manifest edit.
    result_copy = audit_config.audit_dir / record.task_id / "result.json"
    assert result_copy.is_file()
    assert "requirements.txt" in result_copy.read_text()
