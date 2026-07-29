"""Approval grants accumulate across a resume chain instead of dropping.

Each approval used to carry only the single just-approved command into the
resumed run, so a replayed multi-command plan (git add → commit → push)
oscillated between gates forever. ``merge_approval_grants`` unions the prior
run's accumulated grants with the verdict it ran under; the supervisor injects
the result into the resumed envelope's ``worker_state``.
"""

from __future__ import annotations

from skep.worker_contract import (
    APPROVAL_GRANTS_STATE_KEY,
    ApprovalVerdict,
    AutonomyDecisionPayload,
    approval_grants_from_state,
    merge_approval_grants,
)


def _shell_verdict(command: str) -> ApprovalVerdict:
    return ApprovalVerdict(
        approved=True,
        actor="tester",
        ts="2026-07-01T00:00:00Z",
        action="shell.run",
        reason=f"shell.run requires approval for command: {command}",
        decision=AutonomyDecisionPayload(
            verdict="require_approval",
            reason="capability.require_approval.shell_nonverify_not_allowlisted",
            detail=command,
        ),
    )


def test_merge_starts_the_chain_from_a_shell_verdict() -> None:
    grants = merge_approval_grants(None, _shell_verdict("git add README.md"))
    assert grants is not None
    assert grants["shell_commands"] == [["git", "add", "README.md"]]


def test_merge_folds_a_batch_verdict_commands() -> None:
    """v19-F1: one verdict carrying a `commands` list grants all of them."""
    verdict = ApprovalVerdict(
        approved=True,
        actor="tester",
        ts="2026-07-01T00:00:00Z",
        action="shell.run",
        reason="shell.run requires approval for 3 commands: echo a",
        commands=[["echo", "a"], ["echo", "b"], ["echo", "c"]],
    )
    grants = merge_approval_grants(None, verdict)
    assert grants is not None
    assert grants["shell_commands"] == [["echo", "a"], ["echo", "b"], ["echo", "c"]]


def test_merge_extends_prior_grants_with_the_next_verdict() -> None:
    first = merge_approval_grants(None, _shell_verdict("git add README.md"))
    second = merge_approval_grants(
        {APPROVAL_GRANTS_STATE_KEY: first}, _shell_verdict("git commit -m 'Add README'")
    )
    assert second is not None
    assert second["shell_commands"] == [
        ["git", "add", "README.md"],
        ["git", "commit", "-m", "Add README"],
    ]


def test_merge_converges_over_an_add_commit_push_chain() -> None:
    state: dict[str, object] | None = None
    for command in ("git add README.md", "git commit -m 'Add README'", "git push origin main"):
        grants = merge_approval_grants(state, _shell_verdict(command))
        state = {APPROVAL_GRANTS_STATE_KEY: grants}
    shell_commands, _, _, _ = approval_grants_from_state(state)
    assert shell_commands == [
        ["git", "add", "README.md"],
        ["git", "commit", "-m", "Add README"],
        ["git", "push", "origin", "main"],
    ]


def test_merge_ignores_a_denied_verdict() -> None:
    denied = ApprovalVerdict(
        approved=False,
        actor="tester",
        ts="2026-07-01T00:00:00Z",
        action="shell.run",
        reason="shell.run requires approval for command: rm -rf /",
    )
    assert merge_approval_grants(None, denied) is None


def test_merge_returns_none_when_nothing_to_carry() -> None:
    verdict = ApprovalVerdict(approved=True, actor="tester", ts="2026-07-01T00:00:00Z")
    assert merge_approval_grants(None, verdict) is None


def test_merge_collects_capability_and_plugin_grants() -> None:
    commit = ApprovalVerdict(
        approved=True,
        actor="tester",
        ts="2026-07-01T00:00:00Z",
        action="git.commit",
        reason="git.commit requires approval",
    )
    plugin = ApprovalVerdict(
        approved=True,
        actor="tester",
        ts="2026-07-01T00:00:00Z",
        action="writer.touch",
        reason="writer.touch requires approval for risk 'write'",
        decision=AutonomyDecisionPayload(
            verdict="require_approval",
            reason="capability.require_approval.plugin_risk_not_allowed",
            detail="write",
        ),
    )
    first = merge_approval_grants(None, commit)
    second = merge_approval_grants({APPROVAL_GRANTS_STATE_KEY: first}, plugin)
    assert second is not None
    assert second["capability_ids"] == ["git.commit", "git.stage", "writer.touch"]
    assert second["plugin_risks"] == {"writer.touch": "write"}


def test_grants_from_state_ignores_malformed_payloads() -> None:
    malformed = {
        APPROVAL_GRANTS_STATE_KEY: {
            "shell_commands": [["git", "add"], "not-a-list", [1, 2], []],
            "capability_ids": ["git.stage", 7],
            "plugin_risks": {"writer.touch": "write", "bad.tool": "not-a-risk", 3: "write"},
            # v90-F3: hosts accumulate too, and malformed entries are dropped.
            "network_hosts": ["docs.example.com", 7, None],
        }
    }
    shell_commands, capability_ids, plugin_risks, network_hosts = approval_grants_from_state(
        malformed
    )
    assert shell_commands == [["git", "add"]]
    assert capability_ids == frozenset({"git.stage"})
    assert plugin_risks == {"writer.touch": "write"}
    assert network_hosts == frozenset({"docs.example.com"})
    assert approval_grants_from_state({APPROVAL_GRANTS_STATE_KEY: "junk"}) == (
        [],
        frozenset(),
        {},
        frozenset(),
    )
    assert approval_grants_from_state(None) == ([], frozenset(), {}, frozenset())


def _host_verdict(host: str) -> ApprovalVerdict:
    return ApprovalVerdict(
        approved=True,
        actor="tester",
        ts="2026-07-01T00:00:00Z",
        action="network.fetch",
        reason=f"network.fetch requires approval for host: {host}",
        decision=AutonomyDecisionPayload(
            verdict="require_approval",
            reason="capability.require_approval.network_allowlist_missing",
            detail=host,
        ),
    )


def test_network_host_grants_accumulate_across_the_resume_chain() -> None:
    """v90-F3: approving host A then host B must not lose A.

    The worker read hosts from the CURRENT verdict only, so a run needing two
    hosts oscillated: approve A → resume → asks for B → approve B → resume →
    asks for A again. Shell commands and capability ids were fixed by the
    accumulated grants; the network lane was missed.
    """
    state: dict[str, object] | None = None
    for host in ("docs.example.com", "api.example.org"):
        grants = merge_approval_grants(state, _host_verdict(host))
        state = {APPROVAL_GRANTS_STATE_KEY: grants}

    _, _, _, network_hosts = approval_grants_from_state(state)
    assert network_hosts == frozenset({"docs.example.com", "api.example.org"})


def test_the_worker_sees_accumulated_hosts_not_just_the_latest_verdict() -> None:
    """The worker-side reader is the half that actually decides — pin it too."""
    from skep.workers.coding_minimal import _approved_network_hosts

    # Only the accumulated grants (a resume whose fresh verdict is a shell one).
    assert _approved_network_hosts(None, ["a.example.com"]) == ("a.example.com",)
    # Accumulated plus the verdict riding this resume, de-duplicated and ordered.
    hosts = _approved_network_hosts(_host_verdict("b.example.com"), ["a.example.com"])
    assert hosts == ("a.example.com", "b.example.com")
    assert _approved_network_hosts(_host_verdict("a.example.com"), ["a.example.com"]) == (
        "a.example.com",
    )
