# ruff: noqa: F811  (the `home` fixture is imported)
"""W14: a policy write is one read -> check -> write.

Before, two keeps a few tenths of a second apart lost one while both
receipts said "remembered", a revoke racing a keep could bring the revoked
grant back, and two writers whose combination is forbidden could both land.
Every policy writer now holds one in-process lock across its read, check
and write (serve is one process per home), and the policy document and the
operator overlay are written compare-and-set, so a CLI process racing
serve re-reads and re-checks instead of overwriting. The advisory replay
runs after the lock is released.

Each race widens the window on purpose: the write-time check sleeps, so a
broken writer always loses a write and a locked one never does."""

from __future__ import annotations

import threading
import time
from functools import partial
from typing import Any

import pytest
from fastapi import HTTPException

from skep.supervisor import RunStore, policy_write
from skep.supervisor.policy_schema import (
    OPERATOR_POLICY_SETTINGS_KEY,
    POLICY_DOCUMENT_SETTINGS_KEY,
    LearnedRule,
    document_from_settings,
)
from skep.supervisor.serve import actions

from .race import race
from .test_policy_write_v3 import Home, _fetch_domain, _keep, home  # noqa: F401  (fixture)


@pytest.fixture()
def slow_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hold every writer inside its window for a moment."""
    real = policy_write.vet_write

    def slow(*args: Any, **kwargs: Any) -> Any:
        report = real(*args, **kwargs)
        time.sleep(0.05)
        return report

    monkeypatch.setattr(policy_write, "vet_write", slow)


def _patterns(h: Home) -> set[str]:
    return {rule.pattern for rule in h.learned()}


@pytest.mark.usefixtures("slow_check")
def test_concurrent_keeps_all_land(home: Home) -> None:
    commands = [f"ls -{letter}" for letter in "abcdefgh"]
    outcomes = race(*(lambda c=c: _keep(home, c) for c in commands))
    assert not [o for o in outcomes if isinstance(o, Exception)]
    assert _patterns(home) >= set(commands)


@pytest.mark.usefixtures("slow_check")
def test_a_revoke_racing_a_keep_is_never_undone(home: Home) -> None:
    _keep(home, "ls -a")
    (granted,) = home.learned()
    for _ in range(3):
        outcomes = race(
            partial(actions.revoke_policy_rule, home.store, rule_id=granted.rule_id),
            lambda: _keep(home, "ls -b"),
        )
        assert not [o for o in outcomes if isinstance(o, Exception)]
        assert "ls -a" not in _patterns(home) and "ls -b" in _patterns(home)
        _keep(home, "ls -a")
        (granted,) = [r for r in home.learned() if r.pattern == "ls -a"]


@pytest.mark.usefixtures("slow_check")
def test_a_grant_and_the_deny_it_collides_with_never_both_land_unsaid(home: Home) -> None:
    """Serially: the deny first refuses the grant (check 6), or the grant
    first and the deny's answer names the grant it now shadows. Racing, one
    of those two — never both stored with nothing said."""
    outcomes = race(
        lambda: _fetch_domain(home, "example.com"),
        lambda: actions.set_operator_policy_rule(
            home.store, scope="network", action="fetch", pattern="example.com", verdict="deny"
        ),
    )
    grant, deny = outcomes
    assert not isinstance(deny, Exception)
    if isinstance(grant, Exception):
        assert "check 6" in str(grant)
        assert "example.com" not in _patterns(home)
    else:
        assert "example.com" in _patterns(home)
        assert any("example.com" in w for w in deny.get("warnings", [])), deny


def test_another_process_writing_between_read_and_write_is_retried(
    home: Home, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI racing serve: a second connection on the same file adds a
    grant while this writer is checking. The compare-and-set refuses the
    stale write; the writer re-reads, re-checks and lands on top."""
    other = RunStore(home.config.db_path)
    real = policy_write.vet_write
    sneaked: list[bool] = []

    def interleaved(*args: Any, **kwargs: Any) -> Any:
        report = real(*args, **kwargs)
        if not sneaked:
            sneaked.append(True)
            doc = document_from_settings(other.get_setting(POLICY_DOCUMENT_SETTINGS_KEY))
            assert doc is not None
            rule = LearnedRule(rule_id="cli", action="run", pattern="ls -z", scope="shell",
                               provenance="allow-always:cli", matcher="exact_argv")  # fmt: skip
            updated = doc.model_copy(update={"learned": [*doc.learned, rule]})
            other.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, updated.model_dump_json())
        return report

    _keep(home, "ls -a")  # the document exists
    monkeypatch.setattr(policy_write, "vet_write", interleaved)
    try:
        _keep(home, "ls -b")
    finally:
        other.close()
    assert {"ls -a", "ls -b", "ls -z"} <= _patterns(home)


def test_a_setting_that_never_settles_fails_with_nothing_stored(
    home: Home, monkeypatch: pytest.MonkeyPatch
) -> None:
    _keep(home, "ls -a")
    other = RunStore(home.config.db_path)
    real = policy_write.vet_write
    bump = iter(range(1000))

    def always_changed(*args: Any, **kwargs: Any) -> Any:
        report = real(*args, **kwargs)
        other.set_setting(OPERATOR_POLICY_SETTINGS_KEY, {"bump": next(bump)})
        return report

    monkeypatch.setattr(policy_write, "vet_write", always_changed)
    try:
        with pytest.raises(HTTPException) as refused:
            actions.set_operator_policy_rule(
                home.store, scope="network", action="fetch", pattern="x.org", verdict="deny"
            )
    finally:
        other.close()
    assert refused.value.status_code == 400
    assert "another skep process changed" in str(refused.value.detail)
    assert "x.org" not in str(home.store.get_setting(OPERATOR_POLICY_SETTINGS_KEY))


def test_the_replay_runs_after_the_lock_is_released(
    home: Home, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A standing write's replay takes seconds on a full log; a keep on a
    card must not wait behind it, and the standing write still answers with
    its replay."""
    entered, release = threading.Event(), threading.Event()
    real = policy_write._replay

    def held(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(5)
        return real(*args, **kwargs)

    monkeypatch.setattr(policy_write, "_replay", held)
    report: dict[str, Any] = {}
    standing = threading.Thread(
        target=actions.learn_policy_rule,
        kwargs={"store": home.store, "rule_id": "net:x", "action": "fetch",
                "pattern": "docs.example.org", "scope": "network",
                "provenance": "allow-always:op", "report": report},
    )  # fmt: skip
    standing.start()
    try:
        assert entered.wait(5)
        done = threading.Thread(
            target=actions.remember_action_for_session,
            kwargs={"store": home.store, "tool": "run_shell", "args": {"command": "ls -q"},
                    "actor": "op", "tier": "session", "chat_id": "c"},
        )  # fmt: skip
        done.start()
        done.join(3)
        assert not done.is_alive(), "a session keep waited behind a replay"
    finally:
        release.set()
        standing.join(5)
    assert "replay_line" in report
    assert {"docs.example.org", "ls -q"} <= _patterns(home)
