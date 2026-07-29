"""v3.5 acceptance demo — the workflow-template round-trip, through the real CLI.

Author ONE template by hand, once, then prove both surfaces from it:
  (1) run it on demand                — `skep run --template ...`
  (2) bind it to a schedule and tick  — `skep schedule add --template ...` + `skep tick`

Both mint completely normal tasks (zero contract change), run the deterministic,
offline audit caste, complete, and are independently re-verified (G10). This is
the v3 U1 nightly bot with its instructions/knobs lifted into a reusable recipe —
templates plug straight into the existing spine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skep.cli import main
from skep.supervisor import RunStore
from tests.fixtures.toy_repo import create_audit_toy_repo

pytestmark = pytest.mark.smoke


def _cli(home: Path, *args: str) -> int:
    return main(["--home", str(home), *args])


def test_template_round_trip_run_and_schedule(tmp_path: Path) -> None:
    home = tmp_path / "home"
    on_demand_repo = create_audit_toy_repo(tmp_path / "on-demand")
    scheduled_repo = create_audit_toy_repo(tmp_path / "scheduled")

    # Author the template once, by hand (the v3.5 "user-authored" recipe).
    assert (
        _cli(
            home,
            "template",
            "add",
            "dep-audit",
            "--caste",
            "audit",
            "--instructions",
            "Audit {{ project }} dependencies and bump known advisories.",
            "--param",
            "project",
            "--budget-max-provider-calls",
            "0",
            "--description",
            "Nightly dependency audit",
        )
        == 0
    )

    # (1) Run it on demand against one repo.
    assert (
        _cli(
            home,
            "run",
            "--template",
            "dep-audit",
            str(on_demand_repo),
            "--param",
            "project=on-demand",
            "--quiet",
        )
        == 0
    )

    # (2) Bind the SAME template to a schedule against another repo, then tick.
    assert (
        _cli(
            home,
            "schedule",
            "add",
            "nightly",
            str(scheduled_repo),
            "--template",
            "dep-audit",
            "--param",
            "project=scheduled",
            "--every",
            "1d",
        )
        == 0
    )
    assert _cli(home, "tick") == 0

    store = RunStore(home / "supervisor" / "supervisor.sqlite3")
    try:
        runs = {r.instructions: r for r in store.recent_runs(10)}
        assert len(runs) == 2  # exactly the on-demand run and the scheduled run

        on_demand = runs["Audit on-demand dependencies and bump known advisories."]
        scheduled = runs["Audit scheduled dependencies and bump known advisories."]

        # Both completed and were independently re-verified (G10) — normal tasks.
        for record in (on_demand, scheduled):
            assert record.state == "completed", record
            reverify = store.reverification_for(record.task_id)
            assert reverify is not None and reverify.confirmed

        # The scheduled run came from the hand-authored template (live binding).
        schedule = store.get_schedule("nightly")
        assert schedule is not None
        assert schedule.template_name == "dep-audit"
        assert schedule.params == {"project": "scheduled"}
    finally:
        store.close()
