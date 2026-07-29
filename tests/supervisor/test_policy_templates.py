"""v40-F11 (v36-F7): four templates as data, each pinned by a golden.

Per template: the document validates, its resolution matches the committed
golden byte-for-byte (the anti-drift guarantee: a verdict change without a
doc change fails here), and its documented character holds as assertions.
The immutable-floor test asserts against the RESOLVER: no template, overlay,
or learned rule can produce an allow for remote git."""

from __future__ import annotations

from pathlib import Path

import pytest

from skep.supervisor.policy_schema import (
    LearnedRule,
    LearnedRuleRejected,
    PolicyDocument,
    decide,
    resolve,
)
from skep.supervisor.policy_templates import (
    TEMPLATE_NAMES,
    builtin_policy_templates,
    golden_bytes,
    load_policy_template,
)

GOLDEN_DIR = Path(__file__).parents[1] / "fixtures" / "policy_templates"


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_template_resolution_matches_its_golden(name: str) -> None:
    document = load_policy_template(name)
    golden = (GOLDEN_DIR / f"{name}.resolved.json").read_text(encoding="utf-8")
    assert golden_bytes(document) == golden, (
        f"{name} resolution drifted — if the change is intended, regenerate the "
        "golden AND update the documented character in the same commit"
    )


def test_all_four_load_and_reference_real_packs() -> None:
    templates = builtin_policy_templates()
    assert set(templates) == set(TEMPLATE_NAMES)
    assert templates["personal-dev"].pack == "trusted_local_dev"
    assert templates["homelab-ops"].pack == "trusted_local_ops"
    assert templates["locked-down"].pack is None
    assert templates["assistant"].pack is None


def test_locked_down_character_zero_allows_outside_read() -> None:
    document = load_policy_template("locked-down")
    for scope in document.scopes:
        for rule in scope.allow:
            assert rule.action == "read", f"locked-down allows {rule.rule_id}"
    resolved = resolve(document)
    gated = decide(resolved, "shell", "run", "echo hi", template="locked-down")
    assert gated.verdict == "require_approval"


def test_personal_dev_character_worktree_free_registries_only() -> None:
    document = load_policy_template("personal-dev")
    resolved = resolve(document)
    assert decide(resolved, "coding", "edit", "workspace", template="personal-dev").verdict == (
        "allow"
    )
    assert decide(resolved, "network", "connect", "pypi.org", template="personal-dev").verdict == (
        "allow"
    )
    assert decide(
        resolved, "network", "connect", "example.com", template="personal-dev"
    ).verdict == "require_approval"
    assert decide(
        resolved, "shell", "run", "cargo build", template="personal-dev"
    ).verdict == "require_approval"


def test_homelab_ops_character_declared_paths_mcp_gated() -> None:
    document = load_policy_template("homelab-ops")
    resolved = resolve(document)
    assert decide(
        resolved, "filesystem", "write", "/var/log/skep/x.log", template="homelab-ops"
    ).verdict == "allow"
    assert decide(
        resolved, "filesystem", "write", "/etc/passwd", template="homelab-ops"
    ).verdict == "require_approval"
    assert decide(
        resolved, "shell", "run", "systemctl status nginx", template="homelab-ops"
    ).verdict == "allow"
    assert decide(
        resolved, "mcp", "call", "anything:at_all", template="homelab-ops"
    ).verdict == "require_approval"
    # v41-F3: email mirrors the mcp-gated stance — nothing flows unasked.
    assert decide(
        resolved, "email", "read", "mail:read_inbox", template="homelab-ops"
    ).verdict == "require_approval"
    assert decide(
        resolved, "email", "send", "mail:send_message", template="homelab-ops"
    ).verdict == "require_approval"


def test_assistant_character_reads_free_mutations_carded() -> None:
    document = load_policy_template("assistant")
    resolved = resolve(document)
    assert decide(resolved, "filesystem", "read", "/notes.md", template="assistant").verdict == (
        "allow"
    )
    assert decide(
        resolved, "filesystem", "write", "/notes.md", template="assistant"
    ).verdict == "require_approval"
    # No explicit mcp OR email rules: the runtime risk ladder gives read-free /
    # mutation-carded (mcp_scope_decision), which IS the character — an
    # email-bound server's reads flow and its sends card (v41-F3).
    assert not any(scope.scope in {"mcp", "email"} for scope in document.scopes)


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_immutable_floor_no_template_can_allow_remote_git(name: str) -> None:
    """Asserted against the resolver, not the docs: a learned rule lifting
    `git push` is rejected under EVERY template."""
    document = load_policy_template(name)
    pushy = LearnedRule(rule_id="pushy", action="run", pattern="git push", scope="shell")
    with pytest.raises(LearnedRuleRejected) as excinfo:
        resolve(document, learned=(pushy,))
    assert excinfo.value.deny_rule_id.startswith("floor/")
    # And no template ships an allow whose pattern is a remote-git prefix.
    for scope in document.scopes:
        if scope.scope != "shell":
            continue
        for rule in scope.allow:
            assert not rule.pattern.startswith("git push"), rule.rule_id


def test_overlay_composition_tightens_a_template() -> None:
    """'personal-dev with stricter mcp' is an overlay, not a fork."""
    base = load_policy_template("personal-dev")
    overlay = PolicyDocument.model_validate(
        {
            "scopes": [
                {
                    "scope": "mcp",
                    "deny": [{"rule_id": "no-mcp", "action": "call", "pattern": "*"}],
                }
            ]
        }
    )
    resolved = resolve(base, overlays=(overlay,))
    assert decide(resolved, "mcp", "call", "srv:tool", template="personal-dev").verdict == "deny"
    # The base's own character is untouched.
    assert decide(resolved, "coding", "edit", "workspace", template="personal-dev").verdict == (
        "allow"
    )
