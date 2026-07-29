"""v90-F3: session grants in the policy document, and the receipt that says so."""

from __future__ import annotations

from skep.supervisor.config import SupervisorConfig
from skep.supervisor.policy_schema import PolicyDocument
from skep.supervisor.serve import actions
from skep.supervisor.store import RunStore


def _doc(store: RunStore) -> PolicyDocument:
    from skep.supervisor.policy_schema import (
        POLICY_DOCUMENT_SETTINGS_KEY,
        PolicyDocument,
        document_from_settings,
    )

    raw = store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY)
    return document_from_settings(raw) or PolicyDocument()


def test_confirming_a_url_card_grants_the_host_for_the_session(config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        grant = actions.remember_action_for_session(
            store, tool="read_url", args={"url": "https://docs.example.com/x"}, actor="me"
        )
        assert grant == {"scope": "network", "pattern": "docs.example.com", "tier": "session"}
        learned = _doc(store).learned
        assert [r.pattern for r in learned] == ["docs.example.com"]
        assert learned[0].provenance == "session:me"

        # The same decision function the read_url card consults now allows it.
        from skep.supervisor.serve.tools import fetch_grant_decision

        decision = fetch_grant_decision(store, "docs.example.com")
        assert decision is not None and decision.verdict == "allow"
        # EXACT host only — a subdomain is its own decision (fail closed).
        assert fetch_grant_decision(store, "other.example.com") is None
    finally:
        store.close()


def test_session_grants_are_dropped_at_serve_startup(config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        actions.remember_action_for_session(
            store, tool="read_url", args={"url": "https://a.example.com/"}, actor="me"
        )
        actions.learn_policy_rule(
            store,
            rule_id="network:fetch:durable.example.com",
            action="fetch",
            pattern="durable.example.com",
            scope="network",
            provenance="allow-always:me",
        )
        assert actions.clear_session_policy_rules(store) == 1
        remaining = [r.pattern for r in _doc(store).learned]
        assert remaining == ["durable.example.com"]  # the always tier survives
        assert actions.clear_session_policy_rules(store) == 0
    finally:
        store.close()


def test_never_grantable_shell_classes_stay_approve_once(config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        for command in (
            "git push origin main",
            "sudo systemctl restart nginx",
            "himalaya message send --to a@b",
        ):
            assert (
                actions.remember_action_for_session(
                    store, tool="run_shell", args={"command": command}, actor="me"
                )
                is None
            ), command
        assert _doc(store).learned == []
    finally:
        store.close()


def test_a_plain_shell_command_grants_its_exact_argv(config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        grant = actions.remember_action_for_session(
            store, tool="run_shell", args={"command": "uv  run   pytest -q"}, actor="me"
        )
        assert grant is not None
        assert grant["pattern"] == "uv run pytest -q"  # normalised, exact
        # A second identical approve does not stack a duplicate rule.
        assert (
            actions.remember_action_for_session(
                store, tool="run_shell", args={"command": "uv run pytest -q"}, actor="me"
            )
            is None
        )
        assert len(_doc(store).learned) == 1
    finally:
        store.close()
