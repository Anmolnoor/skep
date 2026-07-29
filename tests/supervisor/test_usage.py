"""G8: per-task provider usage is captured so cost is answerable."""

from __future__ import annotations

from pathlib import Path

from skep.supervisor import RunStore, SupervisorConfig, run_task


def test_usage_is_recorded_per_task_and_aggregated(repo: Path, config: SupervisorConfig) -> None:
    first = run_task(repo, "Fix the bug. MODE:happy", config=config)
    second = run_task(repo, "Fix again. MODE:happy", config=config)

    store = RunStore(config.db_path)
    try:
        usage = store.usage_for(first.record.task_id)
        totals = store.usage_totals()
    finally:
        store.close()

    assert usage is not None
    assert usage.provider_calls == 2
    assert usage.input_tokens == 120
    assert usage.output_tokens == 40
    # Aggregation answers per-day/repo cost across the two runs (G8).
    assert totals.provider_calls == 4
    assert totals.input_tokens == 240
    assert totals.output_tokens == 80
    assert second.record.task_id != first.record.task_id
