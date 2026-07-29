"""v12: the autonomy scorecard schema.

The scorecard stops us judging trusted autonomy by vibes. It is a set of
metrics, each with a threshold and a pass/warn/fail status, aggregated into a
report that fails hard when any metric fails. The report is deterministic: the
caller supplies ``generated_at`` and the metric values, so the same inputs
always render the same JSON and Markdown.

This module is only the schema and the status/serialization logic. The metric
*values* are computed by ``scripts/scorecard.py`` (Step 4) from deterministic
test outcomes; the policy-regression corpus and smoke suite (Steps 2-3) are the
evidence those values summarize.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

MetricStatus = Literal["pass", "warn", "fail"]
Comparison = Literal["at_least", "at_most"]

MetricValue = float | int | str


@dataclass(frozen=True)
class ScorecardMetric:
    name: str
    value: MetricValue
    threshold: float | int | None
    status: MetricStatus
    detail: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "value": self.value,
            "threshold": self.threshold,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ScorecardReport:
    generated_at: str
    suite: str
    metrics: tuple[ScorecardMetric, ...]
    failures: tuple[str, ...]

    @classmethod
    def build(
        cls,
        *,
        generated_at: str,
        suite: str,
        metrics: Iterable[ScorecardMetric],
    ) -> ScorecardReport:
        collected = tuple(metrics)
        failures = tuple(metric.name for metric in collected if metric.status == "fail")
        return cls(
            generated_at=generated_at,
            suite=suite,
            metrics=collected,
            failures=failures,
        )

    @property
    def ok(self) -> bool:
        """True when no metric failed (warnings do not fail the scorecard)."""
        return not self.failures

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(metric.name for metric in self.metrics if metric.status == "warn")

    def to_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "suite": self.suite,
            "ok": self.ok,
            "metrics": [metric.to_dict() for metric in self.metrics],
            "failures": list(self.failures),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

    def to_markdown(self) -> str:
        status_emoji = {"pass": "✅", "warn": "⚠️", "fail": "❌"}
        lines = [
            f"# Autonomy scorecard — {self.suite}",
            "",
            f"Generated at: {self.generated_at}",
            "",
            f"Overall: {'PASS' if self.ok else 'FAIL'}",
            "",
            "| Metric | Value | Threshold | Status | Detail |",
            "| --- | --- | --- | --- | --- |",
        ]
        for metric in self.metrics:
            threshold = "—" if metric.threshold is None else metric.threshold
            detail = metric.detail or ""
            emoji = status_emoji.get(metric.status, metric.status)
            lines.append(
                f"| {metric.name} | {metric.value} | {threshold} "
                f"| {emoji} {metric.status} | {detail} |"
            )
        if self.failures:
            lines.extend(["", "## Failures", ""])
            lines.extend(f"- {name}" for name in self.failures)
        lines.append("")
        return "\n".join(lines)


def _status_at_least(
    value: float | int,
    threshold: float | int | None,
    warn_threshold: float | int | None,
) -> MetricStatus:
    if threshold is None:
        return "pass"
    if value >= threshold:
        return "pass"
    if warn_threshold is not None and value >= warn_threshold:
        return "warn"
    return "fail"


def _status_at_most(
    value: float | int,
    threshold: float | int | None,
    warn_threshold: float | int | None,
) -> MetricStatus:
    if threshold is None:
        return "pass"
    if value <= threshold:
        return "pass"
    if warn_threshold is not None and value <= warn_threshold:
        return "warn"
    return "fail"


def metric(
    name: str,
    value: MetricValue,
    *,
    threshold: float | int | None,
    comparison: Comparison = "at_least",
    warn_threshold: float | int | None = None,
    detail: str | None = None,
) -> ScorecardMetric:
    """Build a metric, computing its status from ``value`` against ``threshold``.

    ``at_least`` metrics (rates, success ratios) pass when the value is at or
    above the threshold; ``at_most`` metrics (drift counts, runs-per-task) pass
    when the value is at or below it. A string value carries no threshold and is
    always informational (``pass``).
    """
    if isinstance(value, str):
        status: MetricStatus = "pass"
    elif comparison == "at_least":
        status = _status_at_least(value, threshold, warn_threshold)
    else:
        status = _status_at_most(value, threshold, warn_threshold)
    return ScorecardMetric(
        name=name,
        value=value,
        threshold=threshold,
        status=status,
        detail=detail,
    )
