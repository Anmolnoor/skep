"""v15 Step 4: ops schedules are conservative and policy-aware."""

from __future__ import annotations

from skep.supervisor.nodes import OPS_CAPABILITIES
from skep.supervisor.packs import ops_schedule_seeds
from skep.supervisor.scheduler import ops_schedule_is_conservative


def test_seeds_are_all_known_capabilities() -> None:
    for seed in ops_schedule_seeds():
        assert seed.capability in OPS_CAPABILITIES


def test_every_seed_is_conservative() -> None:
    # Local LLM health, disk usage, service health, repo hygiene, backup dry-run.
    names = {seed.name for seed in ops_schedule_seeds()}
    assert names == {
        "local-llm-health",
        "disk-usage",
        "service-health",
        "repo-hygiene",
        "backup-dry-run",
    }
    for seed in ops_schedule_seeds():
        assert ops_schedule_is_conservative(seed.capability, dry_run=seed.dry_run), seed.name


def test_backup_seed_is_dry_run() -> None:
    backup = next(s for s in ops_schedule_seeds() if s.name == "backup-dry-run")
    assert backup.dry_run is True


def test_non_dry_run_mutating_schedule_is_not_conservative() -> None:
    # A mutating capability that is NOT dry-run cannot run unattended.
    assert ops_schedule_is_conservative("ops.service.restart", dry_run=False) is False
    assert ops_schedule_is_conservative("ops.maintenance.clean_paths", dry_run=False) is False
    # Dry-run makes it eligible; inspection is always eligible.
    assert ops_schedule_is_conservative("ops.service.restart", dry_run=True) is True
    assert ops_schedule_is_conservative("ops.inspect.disk", dry_run=False) is True
    # Network probes are never eligible for unattended runs (fail-closed).
    assert ops_schedule_is_conservative("ops.network.probe", dry_run=False) is False
