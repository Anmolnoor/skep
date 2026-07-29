"""v31: portable, signed skill bundles — export/import with a human gate.

A skill is a ``WorkflowTemplate``; its dangerous surface is the recipe's
grants (shell allowlist, git mutation, network, env). The ClawHub exfiltration
lesson: an imported skill must never enter the registry — or run — without a
human seeing exactly what it can do. Two defenses, in order:

1. Full disclosure + a mandatory human gate (``skill_grants``). This holds even
   for an unsigned or foreign bundle.
2. An integrity signature (HMAC-SHA256 over the canonical bytes, keyed by the
   operator's 0600 signing key). A valid signature proves the bundle was not
   tampered since THIS operator signed it — self-fleet provenance. It never
   skips the human gate for a skill carrying dangerous grants.

stdlib only — no crypto dependency.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from pathlib import Path
from typing import Any, Literal

from .templates import WorkflowTemplate, template_from_dict, template_to_dict, validate_template

BUNDLE_FORMAT = "skep-skill/1"
_SIGNING_KEY_FILE = "skill-signing-key"

# verified: signed by THIS operator's key, untampered — trusted provenance.
# tampered: claims this operator's key_id but the signature is invalid — a
#   bundle we signed was modified in transit. Hard-refuse.
# foreign:  signed by a DIFFERENT key (another operator) — HMAC cannot prove
#   authenticity across parties, so this needs the human gate + full disclosure.
# unsigned: no signature at all.
VerifyResult = Literal["verified", "tampered", "foreign", "unsigned"]


def bundle_skill(template: WorkflowTemplate) -> dict[str, Any]:
    """A deterministic, portable bundle for one skill (no signature yet)."""
    validate_template(template)
    return {"format": BUNDLE_FORMAT, "skill": template_to_dict(template)}


def canonical_bytes(bundle: dict[str, Any]) -> bytes:
    """The exact bytes a signature covers: the bundle minus its signature
    fields, JSON with sorted keys and no incidental whitespace."""
    payload = {k: v for k, v in bundle.items() if k not in ("signature", "key_id")}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:12]


def sign_bundle(bundle: dict[str, Any], key: bytes) -> dict[str, Any]:
    signature = hmac.new(key, canonical_bytes(bundle), hashlib.sha256).hexdigest()
    return {**bundle, "signature": signature, "key_id": key_id(key)}


def verify_bundle(bundle: dict[str, Any], key: bytes) -> VerifyResult:
    """Classify a bundle's signature against THIS operator's key.

    HMAC is symmetric, so a non-matching signature could be a tamperer OR a
    different legitimate signer. The bundle's ``key_id`` disambiguates: a
    bundle claiming OUR key_id whose signature is invalid was tampered; a
    different key_id is simply foreign (verify with the human gate)."""
    signature = bundle.get("signature")
    if not isinstance(signature, str) or not signature:
        return "unsigned"
    expected = hmac.new(key, canonical_bytes(bundle), hashlib.sha256).hexdigest()
    if hmac.compare_digest(expected, signature):
        return "verified"
    return "tampered" if bundle.get("key_id") == key_id(key) else "foreign"


def skill_grants(template: WorkflowTemplate) -> dict[str, Any]:
    """The FULL disclosure a human must see before importing — everything the
    skill could do once in the registry."""
    shell = [list(command) for command in template.shell_allowlist]
    network = list(template.network)
    env = list(template.env_allowlist)
    dangerous = bool(shell or network or env or template.allow_git_mutation)
    return {
        "worker_kind": template.worker_kind,
        "shell_commands": shell,
        "allow_git_mutation": template.allow_git_mutation,
        "network": network,
        "env_allowlist": env,
        "dangerous": dangerous,
    }


def grants_summary(grants: dict[str, Any]) -> str:
    """A one-line human summary of a skill's grants for a confirm card."""
    parts: list[str] = []
    if grants.get("shell_commands"):
        commands = ", ".join(" ".join(c) for c in grants["shell_commands"])
        parts.append(f"runs: {commands}")
    if grants.get("allow_git_mutation"):
        parts.append("mutates git")
    if grants.get("network"):
        parts.append(f"reaches: {', '.join(grants['network'])}")
    if grants.get("env_allowlist"):
        parts.append(f"reads env: {', '.join(grants['env_allowlist'])}")
    return "; ".join(parts) if parts else "no shell/git/network/env grants"


def skill_from_bundle(bundle: dict[str, Any]) -> WorkflowTemplate:
    """Reconstruct + validate the skill from a bundle. Raises on a bad format."""
    if bundle.get("format") != BUNDLE_FORMAT:
        raise ValueError(f"unknown skill bundle format: {bundle.get('format')!r}")
    skill = bundle.get("skill")
    if not isinstance(skill, dict):
        raise ValueError("skill bundle has no 'skill' recipe")
    template = template_from_dict(skill)
    validate_template(template)
    return template


def skill_signing_key(home: Path) -> bytes:
    """Read-or-mint the operator's skill-signing key (0600, beside the token)."""
    path = home / _SIGNING_KEY_FILE
    if path.is_file():
        return path.read_text(encoding="utf-8").strip().encode("utf-8")
    key = secrets.token_urlsafe(32)
    home.mkdir(parents=True, exist_ok=True)
    path.write_text(key + "\n", encoding="utf-8")
    path.chmod(0o600)
    return key.encode("utf-8")
