"""v12 Step 4: run the deterministic autonomy suites and emit a scorecard.

The runner executes a fixed set of deterministic test suites (the policy
regression corpus, the capability matrix, the trusted-project smoke scenarios,
the v19/v20 field-test regressions, and the scorecard schema tests) as one
pytest subprocess that writes a JUnit XML report. It parses that machine-readable
report — never terminal prose — attributes each case to a suite, and turns the
outcomes into scorecard metrics. Any failing metric fails the scorecard (a
non-zero exit), so CI can gate on it.

The report-building logic (:func:`build_report`) is pure and unit-tested against
synthetic results; only :func:`run_scorecard` shells out to pytest.
"""

from __future__ import annotations

import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .scorecard import ScorecardMetric, ScorecardReport, metric

REPO_ROOT = Path(__file__).resolve().parents[3]
OUTPUT_DIR = REPO_ROOT / "output" / "scorecard"

SUITE_SCORECARD = "AUTONOMY"


@dataclass(frozen=True)
class CaseResult:
    """One test case outcome parsed from the JUnit report."""

    classname: str
    name: str
    outcome: str  # "passed" | "failed" | "skipped"

    @property
    def nodeid(self) -> str:
        return f"{self.classname}::{self.name}"


@dataclass(frozen=True)
class Suite:
    """A named group of test targets whose pass rate becomes one metric."""

    key: str
    label: str
    targets: tuple[str, ...]
    classname_prefixes: tuple[str, ...]
    counts_as_drift: bool = False


# The deterministic evidence the scorecard summarizes. Every target is offline
# (audit caste / capability decisions / scripted FakeOpenAI) so the scorecard is
# reproducible in CI.
SUITES: tuple[Suite, ...] = (
    Suite(
        key="policy_regression_pass_rate",
        label="policy regression corpus",
        targets=("tests/supervisor/test_policy_regression.py",),
        classname_prefixes=("tests.supervisor.test_policy_regression",),
        counts_as_drift=True,
    ),
    Suite(
        key="capability_matrix_pass_rate",
        label="capability allow/escalate/deny matrix",
        targets=("tests/supervisor/test_capability_policy_matrix.py",),
        classname_prefixes=("tests.supervisor.test_capability_policy_matrix",),
        counts_as_drift=True,
    ),
    Suite(
        key="trusted_project_smoke_pass_rate",
        label="trusted-project smoke scenarios",
        targets=("tests/smoke/test_autonomy_scorecard.py",),
        classname_prefixes=("tests.smoke.test_autonomy_scorecard",),
    ),
    Suite(
        key="field_test_regression_pass_rate",
        label="v19/v20 field-test regressions",
        targets=(
            "tests/supervisor/test_field_test_v19.py",
            "tests/supervisor/test_field_test_v20.py",
        ),
        classname_prefixes=(
            "tests.supervisor.test_field_test_v19",
            "tests.supervisor.test_field_test_v20",
        ),
        counts_as_drift=True,
    ),
    Suite(
        key="scorecard_schema_pass_rate",
        label="scorecard schema",
        targets=("tests/supervisor/test_scorecard.py",),
        classname_prefixes=("tests.supervisor.test_scorecard",),
    ),
)


def _all_targets() -> list[str]:
    seen: dict[str, None] = {}
    for suite in SUITES:
        for target in suite.targets:
            seen.setdefault(target, None)
    return list(seen)


def parse_junit_xml(xml_text: str) -> list[CaseResult]:
    """Parse a pytest JUnit XML report into per-case outcomes."""
    root = ET.fromstring(xml_text)
    cases: list[CaseResult] = []
    for case in root.iter("testcase"):
        classname = case.get("classname", "")
        name = case.get("name", "")
        outcome = "passed"
        for child in case:
            tag = child.tag
            if tag in {"failure", "error"}:
                outcome = "failed"
                break
            if tag == "skipped":
                outcome = "skipped"
                break
        cases.append(CaseResult(classname=classname, name=name, outcome=outcome))
    return cases


def _cases_for(suite: Suite, cases: Sequence[CaseResult]) -> list[CaseResult]:
    return [
        case
        for case in cases
        if any(case.classname.startswith(prefix) for prefix in suite.classname_prefixes)
    ]


def _pass_rate(cases: Sequence[CaseResult]) -> tuple[float, int, int]:
    executed = [case for case in cases if case.outcome != "skipped"]
    if not executed:
        return 0.0, 0, 0
    passed = sum(1 for case in executed if case.outcome == "passed")
    return passed / len(executed), passed, len(executed)


def build_report(cases: Sequence[CaseResult], *, generated_at: str) -> ScorecardReport:
    """Turn parsed case outcomes into the scorecard report (pure)."""
    metrics: list[ScorecardMetric] = []
    drift = 0
    total_executed = 0
    for suite in SUITES:
        suite_cases = _cases_for(suite, cases)
        rate, passed, executed = _pass_rate(suite_cases)
        total_executed += executed
        if suite.counts_as_drift:
            drift += executed - passed
        detail = f"{passed}/{executed} passed" if executed else "no cases collected"
        metrics.append(
            metric(suite.key, round(rate, 4), threshold=1.0, detail=detail)
        )
    metrics.append(
        metric(
            "policy_drift_count",
            drift,
            threshold=0,
            comparison="at_most",
            detail="failed cases in policy/capability/field-test suites",
        )
    )
    # v12 Step 5: the 2026-07-08 field-test regressions locked as permanent
    # checks. Every case whose id carries "v19" — the v19_* corpus fixtures
    # (F2 provider-host merge, F3 remote-git deny, F4 remembered-command guard)
    # and the v19 end-to-end field test (F1 one-gate/one-run, F3, F7) — must
    # pass, or these regressions have returned.
    v19_cases = [case for case in cases if "v19" in case.name.lower()]
    v19_failed = sum(1 for case in v19_cases if case.outcome == "failed")
    metrics.append(
        metric(
            "v19_regressions_locked",
            v19_failed,
            threshold=0,
            comparison="at_most",
            detail=f"{len(v19_cases)} v19 regression checks",
        )
    )
    metrics.append(
        metric("checks_total", total_executed, threshold=None, detail="deterministic cases run")
    )
    return ScorecardReport.build(
        generated_at=generated_at, suite=SUITE_SCORECARD, metrics=metrics
    )


def _pytest_tmpdir() -> Path:
    """A sandbox-safe TMPDIR for worker tests (outside /tmp; the bwrap sandbox
    tmpfs-masks /tmp, hiding a test SKEP_HOME created there)."""
    tmp = OUTPUT_DIR / ".pytest-tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    return tmp


def run_pytest_junit(
    targets: Sequence[str], junit_path: Path, *, cwd: Path = REPO_ROOT
) -> None:
    """Run the deterministic suites once, writing a JUnit XML report.

    ``-o addopts=`` clears the repo default ``-m 'not smoke...'`` so the smoke
    scenarios are collected. A non-zero pytest exit (test failures) is expected
    and handled by the caller via the parsed report, not the return code.
    """
    import os

    env = dict(os.environ)
    env.setdefault("TMPDIR", str(_pytest_tmpdir()))
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
            "--junit-xml",
            str(junit_path),
            *targets,
        ],
        cwd=str(cwd),
        env=env,
        check=False,
    )


def run_scorecard(*, generated_at: str | None = None) -> ScorecardReport:
    """Run the deterministic suites and build the scorecard report."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    junit_path = OUTPUT_DIR / "junit.xml"
    run_pytest_junit(_all_targets(), junit_path)
    cases = parse_junit_xml(junit_path.read_text(encoding="utf-8")) if junit_path.is_file() else []
    stamp = generated_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return build_report(cases, generated_at=stamp)


def write_reports(report: ScorecardReport) -> tuple[Path, Path]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUTPUT_DIR / "scorecard.json"
    md_path = OUTPUT_DIR / "scorecard.md"
    json_path.write_text(report.to_json(), encoding="utf-8")
    md_path.write_text(report.to_markdown(), encoding="utf-8")
    return json_path, md_path


def main() -> int:
    report = run_scorecard()
    json_path, md_path = write_reports(report)
    print(report.to_markdown())
    print(f"\nwrote {json_path} and {md_path}")
    if not report.ok:
        print(f"SCORECARD FAILED: {', '.join(report.failures)}", file=sys.stderr)
        return 1
    return 0
