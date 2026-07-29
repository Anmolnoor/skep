"""v31-F1: signed skill bundles — sign/verify/disclose."""

from __future__ import annotations

from pathlib import Path

from skep.supervisor.skill_bundle import (
    bundle_skill,
    canonical_bytes,
    grants_summary,
    sign_bundle,
    skill_from_bundle,
    skill_grants,
    skill_signing_key,
    verify_bundle,
)
from skep.supervisor.templates import TemplateParam, WorkflowTemplate


def _skill(**overrides: object) -> WorkflowTemplate:
    base: dict[str, object] = {
        "name": "nightly-audit",
        "description": "run the audit",
        "instructions": "audit {{scope}}",
        "worker_kind": "audit",
        "params": (TemplateParam(name="scope", description="what to audit"),),
        "shell_allowlist": (("uv", "run", "pytest"),),
        "network": ("pypi.org",),
        "env_allowlist": ("CI",),
        "provenance": "learned",
    }
    base.update(overrides)
    return WorkflowTemplate(**base)  # type: ignore[arg-type]


def test_sign_then_verify_is_verified() -> None:
    key = b"operator-key"
    signed = sign_bundle(bundle_skill(_skill()), key)
    assert signed["signature"]
    assert verify_bundle(signed, key) == "verified"


def test_tampering_a_bundle_claiming_our_key_is_tampered() -> None:
    key = b"operator-key"
    signed = sign_bundle(bundle_skill(_skill()), key)
    # An attacker widens the shell allowlist after signing (key_id unchanged).
    signed["skill"]["shell_allowlist"] = [["curl", "https://evil.test"]]
    assert verify_bundle(signed, key) == "tampered"


def test_a_foreign_key_is_foreign_not_tampered() -> None:
    # HMAC can't prove authenticity across parties; a different key_id is foreign.
    signed = sign_bundle(bundle_skill(_skill()), b"their-key")
    assert verify_bundle(signed, b"my-key") == "foreign"


def test_an_unsigned_bundle_is_unsigned() -> None:
    assert verify_bundle(bundle_skill(_skill()), b"any-key") == "unsigned"


def test_skill_grants_disclose_the_full_surface() -> None:
    grants = skill_grants(_skill(allow_git_mutation=True))
    assert grants["dangerous"] is True
    assert ["uv", "run", "pytest"] in grants["shell_commands"]
    assert grants["network"] == ["pypi.org"]
    assert grants["env_allowlist"] == ["CI"]
    assert grants["allow_git_mutation"] is True
    summary = grants_summary(grants)
    assert "uv run pytest" in summary
    assert "pypi.org" in summary
    assert "mutates git" in summary

    benign = skill_grants(
        _skill(shell_allowlist=(), network=(), env_allowlist=(), allow_git_mutation=False)
    )
    assert benign["dangerous"] is False


def test_canonical_bytes_are_stable_regardless_of_ordering() -> None:
    bundle = bundle_skill(_skill())
    reordered = {k: bundle[k] for k in reversed(list(bundle))}
    assert canonical_bytes(bundle) == canonical_bytes(reordered)
    # The signature field is excluded from the canonical form.
    signed = sign_bundle(bundle, b"k")
    assert canonical_bytes(signed) == canonical_bytes(bundle)


def test_skill_round_trips_through_a_bundle() -> None:
    original = _skill()
    restored = skill_from_bundle(bundle_skill(original))
    assert restored.name == original.name
    assert restored.shell_allowlist == original.shell_allowlist
    assert restored.network == original.network


def test_signing_key_is_minted_once_and_0600(tmp_path: Path) -> None:
    home = tmp_path / "home"
    key1 = skill_signing_key(home)
    key2 = skill_signing_key(home)
    assert key1 == key2  # stable across reads
    path = home / "skill-signing-key"
    assert (path.stat().st_mode & 0o777) == 0o600
