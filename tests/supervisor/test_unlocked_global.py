"""SKEP_UNLOCKED — the global capability probe (sandbox tier) on top of ADR 0052.

The toggle is a single env var (config equivalent: a serve.env line). When on it
injects unlocked into EVERY project's resolved policy and opens run egress, while
instrumenting every gate to a JSONL the report reads back. Default off ⇒ the
injection never fires ⇒ resolution is byte-for-byte governed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skep.supervisor import RunStore
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.policy_resolver import resolve_run_policy, run_policy_for_repo
from skep.supervisor.unlocked import (
    build_report,
    classify_decision,
    record_gate,
    unlocked_active,
    unlocked_tier,
)


class _Dec:
    """A stand-in for AutonomyDecision (classify_decision is duck-typed)."""

    def __init__(self, verdict: str, reason: str, decided_by: str = "") -> None:
        self.verdict = verdict
        self.reason = reason
        self.decided_by = decided_by

    def allows_execution(self) -> bool:
        return self.verdict == "allow"


# ---- the toggle ---------------------------------------------------------


def test_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SKEP_UNLOCKED", raising=False)
    assert unlocked_tier() == "off"
    assert unlocked_active() is False


@pytest.mark.parametrize("value", ["1", "true", "on", "yes", "sandbox", "host", "all", "TRUE"])
def test_truthy_values_enable_sandbox_tier(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SKEP_UNLOCKED", value)
    assert unlocked_tier() == "sandbox"
    assert unlocked_active() is True


@pytest.mark.parametrize("value", ["", "0", "false", "off", "no", "banana"])
def test_falsey_or_unknown_fails_closed(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SKEP_UNLOCKED", value)
    assert unlocked_tier() == "off"


# ---- the policy injection (reuses the ADR 0052 read path) ---------------


def test_off_does_not_inject_unlocked(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SKEP_UNLOCKED", raising=False)
    config = build_config(tmp_path / "home", None)
    store = RunStore(config.db_path)
    try:
        effective = run_policy_for_repo(store, config, repo)
    finally:
        store.close()
    # Governed byte-for-byte: an unregistered repo has no unlocked key at all.
    assert effective.get("unlocked") is not True


def test_on_injects_unlocked_globally_with_provenance(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SKEP_UNLOCKED", "1")
    config = build_config(tmp_path / "home", None)
    store = RunStore(config.db_path)
    try:
        effective = run_policy_for_repo(store, config, repo)
    finally:
        store.close()
    assert effective["unlocked"] is True
    assert effective["_provenance"]["unlocked"] == "unlocked-global"
    # unlocked implies the verified-patch landing lane (ADR 0052 F5), so the
    # global probe inherits the zero-click landing exactly like a per-project one.
    assert effective["auto_apply_verified_patch"] is True


# ---- run egress opens under the probe -----------------------------------


def test_egress_opens_to_all_hosts_only_when_unlocked(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = build_config(tmp_path / "home", None)
    store = RunStore(config.db_path)

    def _net() -> list[str]:
        return resolve_run_policy(
            store=store,
            config=config,
            repo=repo,
            caste="coding",
            network=["github.com"],
            env_allowlist=None,
            wall_clock_seconds=None,
            max_iterations=None,
            max_actions=None,
            max_provider_calls=None,
            execution_mode="sandbox",
        ).permissions.network

    try:
        monkeypatch.delenv("SKEP_UNLOCKED", raising=False)
        assert _net() == ["github.com"]  # governed: exactly what was asked
        monkeypatch.setenv("SKEP_UNLOCKED", "1")
        assert _net() == ["*"]  # probe: blanket egress
    finally:
        store.close()
    # The bypass left a log line the report can read.
    report = build_report(config.audit_dir)
    assert "network.egress" in report


# ---- instrumentation classification -------------------------------------


def test_classify_decision_maps_every_verdict() -> None:
    assert classify_decision(None) == ("ask", "logged_only", "card.default")
    assert classify_decision(_Dec("deny", "shell.deny.catastrophic"))[0] == "deny"
    # an allow whose reason names unlocked is a bypass; any other allow is not.
    would, dispo, _ = classify_decision(
        _Dec("allow", "dispatch.auto_allowed.unlocked", "dispatch.auto_allowed.unlocked")
    )
    assert (would, dispo) == ("allow", "bypassed")
    assert classify_decision(_Dec("allow", "shell.allow.process_lifecycle"))[1] == "allowed"
    assert classify_decision(_Dec("require_approval", "card.default"))[0] == "ask"


def test_record_gate_is_best_effort(tmp_path: Path) -> None:
    # A log write must never break a run — a bad path is swallowed, not raised.
    bad = tmp_path / "a-file"
    bad.write_text("not a dir")
    record_gate(
        bad,  # its <bad>/unlocked mkdir will fail; must not raise
        gate="x",
        surface="s",
        would="ask",
        reason="r",
        action={},
        disposition="bypassed",
    )


def test_report_buckets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKEP_UNLOCKED", "1")
    audit = tmp_path / "audit"
    record_gate(
        audit,
        gate="dispatch_run",
        surface="queen_card",
        would="allow",
        reason="dispatch.auto_allowed.unlocked",
        action={"verb": "dispatch_run"},
        disposition="bypassed",
        run_id="c1",
    )
    record_gate(
        audit,
        gate="run_shell",
        surface="queen_card",
        would="deny",
        reason="shell.deny.catastrophic",
        action={"verb": "run_shell"},
        disposition="logged_only",
        run_id="c1",
    )
    record_gate(
        audit,
        gate="open_pr",
        surface="queen_card",
        would="ask",
        reason="card.default",
        action={"verb": "open_pr"},
        disposition="logged_only",
        run_id="c1",
    )
    report = build_report(audit)
    assert "WOULD HAVE DENIED" in report and "run_shell" in report
    assert "AUTO-ALLOWED BY UNLOCKED" in report and "dispatch_run" in report
    assert "STILL ASKED" in report and "open_pr" in report
    # run_shell denied at least once, so it is NOT a relaxation candidate.
    never_section = report.split("NEVER DENIED")[1]
    assert "run_shell" not in never_section


def test_report_empty_is_honest(tmp_path: Path) -> None:
    assert "No unlocked-mode gate events" in build_report(tmp_path / "empty")
