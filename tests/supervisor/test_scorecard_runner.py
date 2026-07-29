from __future__ import annotations

from skep.supervisor.scorecard_runner import (
    SUITES,
    CaseResult,
    build_report,
    parse_junit_xml,
)

_GEN = "2026-07-08T00:00:00Z"


def _all_green_cases() -> list[CaseResult]:
    cases: list[CaseResult] = []
    for suite in SUITES:
        prefix = suite.classname_prefixes[0]
        cases.append(CaseResult(classname=prefix, name="test_a", outcome="passed"))
        cases.append(CaseResult(classname=prefix, name="test_b", outcome="passed"))
    return cases


def test_parse_junit_xml_classifies_outcomes() -> None:
    xml = """<?xml version="1.0"?>
    <testsuites><testsuite>
      <testcase classname="tests.supervisor.test_scorecard" name="test_ok"/>
      <testcase classname="tests.supervisor.test_scorecard" name="test_bad">
        <failure message="boom">trace</failure>
      </testcase>
      <testcase classname="tests.supervisor.test_scorecard" name="test_skip">
        <skipped/>
      </testcase>
    </testsuite></testsuites>"""
    cases = parse_junit_xml(xml)
    outcomes = {case.name: case.outcome for case in cases}
    assert outcomes == {"test_ok": "passed", "test_bad": "failed", "test_skip": "skipped"}


def test_all_green_report_passes() -> None:
    report = build_report(_all_green_cases(), generated_at=_GEN)
    assert report.ok is True
    assert report.failures == ()
    names = {m.name: m for m in report.metrics}
    assert names["policy_regression_pass_rate"].value == 1.0
    assert names["policy_drift_count"].value == 0
    assert names["checks_total"].value == len(SUITES) * 2


def test_drift_in_policy_suite_fails_the_scorecard() -> None:
    cases = _all_green_cases()
    # Fail one policy-regression case: pass rate drops and drift climbs.
    corpus_prefix = SUITES[0].classname_prefixes[0]
    cases.append(CaseResult(classname=corpus_prefix, name="test_c", outcome="failed"))
    report = build_report(cases, generated_at=_GEN)
    assert report.ok is False
    assert "policy_regression_pass_rate" in report.failures
    assert "policy_drift_count" in report.failures
    drift = next(m for m in report.metrics if m.name == "policy_drift_count")
    assert drift.value == 1


def test_missing_suite_cases_fail_that_metric() -> None:
    # Only the schema suite reported anything: every other suite scores 0.0.
    schema_prefix = SUITES[-1].classname_prefixes[0]
    cases = [CaseResult(classname=schema_prefix, name="test_a", outcome="passed")]
    report = build_report(cases, generated_at=_GEN)
    assert report.ok is False
    assert "policy_regression_pass_rate" in report.failures
    assert "scorecard_schema_pass_rate" not in report.failures


def test_v19_regressions_locked_metric() -> None:
    cases = _all_green_cases()
    corpus_prefix = SUITES[0].classname_prefixes[0]
    cases.append(
        CaseResult(classname=corpus_prefix, name="test_corpus[v19-f2-x]", outcome="passed")
    )
    report = build_report(cases, generated_at=_GEN)
    locked = next(m for m in report.metrics if m.name == "v19_regressions_locked")
    assert locked.status == "pass"
    assert locked.value == 0

    # A returned v19 regression fails the lock.
    cases.append(
        CaseResult(classname=corpus_prefix, name="test_corpus[v19-f3-x]", outcome="failed")
    )
    failed_report = build_report(cases, generated_at=_GEN)
    assert "v19_regressions_locked" in failed_report.failures


def test_smoke_suite_drift_does_not_count_as_policy_drift() -> None:
    cases = _all_green_cases()
    smoke_prefix = next(s for s in SUITES if s.key == "trusted_project_smoke_pass_rate")
    cases.append(
        CaseResult(classname=smoke_prefix.classname_prefixes[0], name="x", outcome="failed")
    )
    report = build_report(cases, generated_at=_GEN)
    # The smoke pass-rate fails, but policy_drift_count stays 0 (smoke is not a
    # policy-verdict suite).
    assert "trusted_project_smoke_pass_rate" in report.failures
    assert "policy_drift_count" not in report.failures
