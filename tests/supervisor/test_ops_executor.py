"""v32-F1: the ops executor — real, bounded, last-guard, evidence."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from skep.workers.ops import OpsDecision
from skep.workers.ops_executor import OpsExecutionError, execute_ops, plan_ops


def _fake_runner(argv: Sequence[str]) -> tuple[int, str, str]:
    return 0, "active", ""


def test_a_dry_run_decision_executes_nothing(tmp_path: Path) -> None:
    victim = tmp_path / "junk.log"
    victim.write_text("data")
    decision = OpsDecision("allow_with_constraints", "ops.allow.dry_run", dry_run=True)
    result = execute_ops(
        decision,
        capability="ops.maintenance.clean_paths",
        arguments={"paths": [str(victim)], "allowed_roots": [str(tmp_path)]},
    )
    assert result.executed is False
    assert result.dry_run is True
    assert victim.exists()  # nothing was touched
    # The plan discloses what WOULD happen.
    assert result.evidence["would_execute"] is True


def test_clean_paths_deletes_only_within_bounds(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    keep_outside = tmp_path / "outside.txt"
    keep_outside.write_text("safe")
    target = root / "old.log"
    target.write_text("stale")

    decision = OpsDecision(
        "allow_with_constraints",
        "ops.allow.maintenance_bounded",
        write_roots=(str(root),),
    )
    result = execute_ops(
        decision,
        capability="ops.maintenance.clean_paths",
        arguments={"paths": [str(target)]},
    )
    assert result.executed is True
    assert not target.exists()
    assert keep_outside.exists()
    assert result.evidence["bytes_freed"] > 0


def test_an_out_of_bounds_path_is_refused_even_if_passed(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    escapee = tmp_path / "secret.txt"
    escapee.write_text("do not delete")
    decision = OpsDecision(
        "allow_with_constraints", "ops.allow.maintenance_bounded", write_roots=(str(root),)
    )
    with pytest.raises(OpsExecutionError):
        execute_ops(
            decision,
            capability="ops.maintenance.clean_paths",
            arguments={"paths": [str(escapee)]},
        )
    assert escapee.exists()  # the last guard held


def test_root_as_a_bounded_root_is_refused(tmp_path: Path) -> None:
    decision = OpsDecision(
        "allow_with_constraints", "ops.allow.maintenance_bounded", write_roots=("/",)
    )
    with pytest.raises(OpsExecutionError):
        execute_ops(
            decision,
            capability="ops.maintenance.clean_paths",
            arguments={"paths": ["/etc/passwd"]},
        )


def test_rotate_logs_truncates_in_place(tmp_path: Path) -> None:
    log = tmp_path / "app.log"
    log.write_text("lots of logs\n" * 100)
    decision = OpsDecision(
        "allow_with_constraints", "ops.allow.maintenance_bounded", write_roots=(str(tmp_path),)
    )
    result = execute_ops(
        decision,
        capability="ops.maintenance.rotate_logs",
        arguments={"paths": [str(log)]},
    )
    assert result.executed is True
    assert log.exists()  # inode kept
    assert log.read_text() == ""  # truncated


def test_backup_copies_only_to_an_allowed_dest(tmp_path: Path) -> None:
    source = tmp_path / "data.db"
    source.write_text("important")
    dest = tmp_path / "backups"
    dest.mkdir()
    decision = OpsDecision(
        "allow_with_constraints", "ops.allow.backup_bounded", write_roots=(str(dest),)
    )
    result = execute_ops(
        decision,
        capability="ops.backup.run",
        arguments={"source": str(source), "dest": str(dest)},
    )
    assert result.executed is True
    assert (dest / "data.db").read_text() == "important"

    with pytest.raises(OpsExecutionError):
        execute_ops(
            decision,
            capability="ops.backup.run",
            arguments={"source": str(source), "dest": str(tmp_path / "elsewhere")},
        )


def test_service_restart_uses_the_injected_runner_only_when_approved() -> None:
    calls: list[Sequence[str]] = []

    def runner(argv: Sequence[str]) -> tuple[int, str, str]:
        calls.append(argv)
        return 0, "", ""

    # Unapproved (dry_run) -> no runner call, a plan.
    dry = OpsDecision("allow_with_constraints", "ops.allow.dry_run", dry_run=True)
    result = execute_ops(
        dry, capability="ops.service.restart", arguments={"service": "nginx"}, runner=runner
    )
    assert result.executed is False
    assert calls == []

    # Approved -> the real restart argv reaches the runner.
    approved = OpsDecision("allow_with_constraints", "ops.allow.service_restart_approved")
    result = execute_ops(
        approved, capability="ops.service.restart", arguments={"service": "nginx"}, runner=runner
    )
    assert result.executed is True
    assert calls == [["systemctl", "restart", "nginx"]]
    assert result.evidence["service"] == "nginx"


def test_inspect_disk_returns_real_usage_with_evidence(tmp_path: Path) -> None:
    decision = OpsDecision("allow", "ops.allow.inspect_trusted_local")
    result = execute_ops(
        decision, capability="ops.inspect.disk", arguments={"path": str(tmp_path)}
    )
    assert result.executed is True
    assert result.evidence["total"] > 0
    assert result.evidence["free"] >= 0


def test_a_denied_decision_never_executes(tmp_path: Path) -> None:
    victim = tmp_path / "junk"
    victim.write_text("x")
    denied = OpsDecision("deny", "ops.deny.capability_not_allowed_on_node")
    result = execute_ops(
        denied,
        capability="ops.maintenance.clean_paths",
        arguments={"paths": [str(victim)], "allowed_roots": [str(tmp_path)]},
    )
    assert result.executed is False
    assert victim.exists()


def test_plan_ops_discloses_without_acting(tmp_path: Path) -> None:
    target = tmp_path / "f.log"
    target.write_text("x" * 10)
    decision = OpsDecision(
        "allow_with_constraints", "ops.allow.maintenance_bounded", write_roots=(str(tmp_path),)
    )
    plan = plan_ops(
        decision, capability="ops.maintenance.clean_paths", arguments={"paths": [str(target)]}
    )
    assert plan["targets"] == [str(target)]
    assert plan["bytes"] == 10
    assert target.exists()  # planning touched nothing
