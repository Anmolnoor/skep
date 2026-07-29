from __future__ import annotations

import json

from skep.supervisor.scorecard import ScorecardMetric, ScorecardReport, metric


def test_at_least_metric_status() -> None:
    assert metric("completion", 1.0, threshold=0.95).status == "pass"
    assert metric("completion", 0.95, threshold=0.95).status == "pass"
    assert metric("completion", 0.9, threshold=0.95, warn_threshold=0.85).status == "warn"
    assert metric("completion", 0.5, threshold=0.95, warn_threshold=0.85).status == "fail"
    # Without a warn band, below-threshold fails outright.
    assert metric("completion", 0.9, threshold=0.95).status == "fail"


def test_at_most_metric_status() -> None:
    assert metric("drift", 0, threshold=0, comparison="at_most").status == "pass"
    assert metric("runs_per_task", 2, threshold=2, comparison="at_most").status == "pass"
    assert (
        metric("runs_per_task", 3, threshold=2, comparison="at_most", warn_threshold=4).status
        == "warn"
    )
    assert metric("drift", 5, threshold=0, comparison="at_most", warn_threshold=1).status == "fail"


def test_no_threshold_and_string_values_are_informational() -> None:
    assert metric("note", "provider configured", threshold=None).status == "pass"
    assert metric("count", 99, threshold=None).status == "pass"


def test_report_aggregates_failures() -> None:
    report = ScorecardReport.build(
        generated_at="2026-07-08T00:00:00Z",
        suite="autonomy",
        metrics=[
            metric("completion", 1.0, threshold=0.95),
            metric("drift", 3, threshold=0, comparison="at_most"),
            metric("runs_per_task", 5, threshold=2, comparison="at_most"),
        ],
    )
    assert report.ok is False
    assert report.failures == ("drift", "runs_per_task")


def test_report_ok_when_all_pass_or_warn() -> None:
    report = ScorecardReport.build(
        generated_at="2026-07-08T00:00:00Z",
        suite="autonomy",
        metrics=[
            metric("completion", 1.0, threshold=0.95),
            metric("escalation", 0.9, threshold=0.95, warn_threshold=0.8),
        ],
    )
    assert report.ok is True
    assert report.warnings == ("escalation",)
    assert report.failures == ()


def test_json_serialization_is_deterministic_and_structured() -> None:
    report = ScorecardReport.build(
        generated_at="2026-07-08T00:00:00Z",
        suite="autonomy",
        metrics=[metric("completion", 1.0, threshold=0.95)],
    )
    first = report.to_json()
    second = report.to_json()
    assert first == second
    payload = json.loads(first)
    assert payload["suite"] == "autonomy"
    assert payload["ok"] is True
    assert payload["metrics"][0]["name"] == "completion"
    assert payload["failures"] == []


def test_markdown_renders_table_and_failures() -> None:
    report = ScorecardReport.build(
        generated_at="2026-07-08T00:00:00Z",
        suite="autonomy",
        metrics=[
            ScorecardMetric("drift", 3, 0, "fail", detail="unexpected verdict"),
        ],
    )
    md = report.to_markdown()
    assert "# Autonomy scorecard — autonomy" in md
    assert "Overall: FAIL" in md
    assert "| drift | 3 | 0 |" in md
    assert "unexpected verdict" in md
    assert "## Failures" in md
