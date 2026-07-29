"""Stage C: the `skep skill ...` CLI surface (argparse wiring + exit codes).

Drives the real CLI through ``main()`` so the flag plumbing (``--as``,
``--min-occurrences``, ``--param``) and the human/auto gates are exercised exactly
as a user would. Offline: the audit caste needs no provider.
"""

from __future__ import annotations

import sys
from pathlib import Path

from skep.cli import main
from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.dispatch import run_task

from ..fixtures.toy_repo import create_audit_toy_repo


def _seed_two_audits(home: Path, tmp_path: Path) -> None:
    """Seed two successful audit runs into the CLI's store (home/supervisor)."""
    config = SupervisorConfig(
        home=home / "supervisor",
        worker_command=("false",),
        caste_worker_commands={"audit": (sys.executable, "-m", "skep.workers.audit")},
        grace_seconds=5.0,
        heartbeat_seconds=10.0,
        poll_seconds=0.02,
    )
    store = RunStore(config.db_path)
    try:
        for project in ("acme", "globex"):
            repo = create_audit_toy_repo(tmp_path / project)
            outcome = run_task(
                repo,
                f"Audit {project} dependencies and bump known advisories.",
                config=config,
                worker_kind="audit",
                store=store,
            )
            assert outcome.record.state == "completed"
    finally:
        store.close()


def _cli(home: Path, *args: str) -> int:
    return main(["--home", str(home), *args])


def _registry(home: Path) -> RunStore:
    return RunStore(home / "supervisor" / "supervisor.sqlite3")


def test_skill_cli_full_lifecycle(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_two_audits(home, tmp_path)

    # propose -> one draft
    assert _cli(home, "skill", "propose") == 0
    store = _registry(home)
    try:
        candidates = store.list_candidates()
        assert len(candidates) == 1
        name = candidates[0].name
    finally:
        store.close()

    assert _cli(home, "skill", "list") == 0
    assert _cli(home, "skill", "show", name) == 0

    # test against a fresh repo (the G10 gate) -> tested
    target = create_audit_toy_repo(tmp_path / "target")
    assert _cli(home, "skill", "test", name, str(target), "--param", "arg1=target") == 0

    # approving a tested candidate under a friendly name -> registry
    assert _cli(home, "skill", "approve", name, "--as", "dep-audit", "--actor", "alice") == 0
    store = _registry(home)
    try:
        template = store.get_template("dep-audit")
        assert template is not None and template.provenance == "learned"
    finally:
        store.close()

    # the learned skill now runs exactly like a user template
    fresh = create_audit_toy_repo(tmp_path / "fresh")
    assert (
        _cli(home, "run", "--template", "dep-audit", str(fresh), "--param", "arg1=fresh", "--quiet")
        == 0
    )


def test_skill_cli_failed_test_blocks_approval(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_two_audits(home, tmp_path)
    assert _cli(home, "skill", "propose") == 0
    store = _registry(home)
    try:
        name = store.list_candidates()[0].name
    finally:
        store.close()

    # A failing repo -> the test gate auto-rejects (exit 3).
    broken = create_audit_toy_repo(tmp_path / "broken", passing=False)
    assert _cli(home, "skill", "test", name, str(broken), "--param", "arg1=broken") == 3
    # Approval is now refused (exit 2 = the CLI error code) and nothing is registered.
    assert _cli(home, "skill", "approve", name) == 2
    store = _registry(home)
    try:
        assert store.list_templates() == []
        assert store.get_candidate(name).status == "rejected"  # type: ignore[union-attr]
    finally:
        store.close()


def test_skill_cli_reject_blocks_approval(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_two_audits(home, tmp_path)
    assert _cli(home, "skill", "propose") == 0
    store = _registry(home)
    try:
        name = store.list_candidates()[0].name
    finally:
        store.close()

    assert _cli(home, "skill", "reject", name, "--actor", "bob") == 0
    assert _cli(home, "skill", "approve", name) == 2  # refused
    store = _registry(home)
    try:
        assert store.list_templates() == []
    finally:
        store.close()
