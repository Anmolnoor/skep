"""v101-F1 (ADR 0049): the caste roster is a registry, not five dict literals.

The pins: the registry covers the contract's vocabulary (minus the one hole it
declares out loud), every entry routes at a module that can actually be
imported, an unknown name is refused rather than silently run as a coding
worker (v42), and `build_config` installs exactly what the registry says.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, cast

import pytest

from skep.supervisor.castes import (
    CASTES,
    DEFAULT_CASTE,
    UNIMPLEMENTED_CASTES,
    caste_names,
    caste_worker_commands,
    resolve_caste,
)
from skep.worker_contract.task import KNOWN_WORKER_KINDS


def test_the_registry_covers_the_contract() -> None:
    """The contract is authoritative for WHICH names exist; the registry routes
    them. v101-F1 landed with `verifier` as a declared hole; v101-F2 wrote the
    worker, so the sets are equal and declaring a caste without registering it
    now fails the gates instead of failing a field run."""
    assert set(CASTES) | UNIMPLEMENTED_CASTES == set(KNOWN_WORKER_KINDS)
    assert set(CASTES).isdisjoint(UNIMPLEMENTED_CASTES)
    assert not UNIMPLEMENTED_CASTES  # v101-F2 emptied it
    assert set(CASTES) == set(KNOWN_WORKER_KINDS)


def test_every_registered_caste_routes_somewhere_importable() -> None:
    """A registry entry pointing at nothing is the same defect wearing a
    registry. `coding` is the one empty argv: it defers to config.command_for,
    which is what SKEP_WORKER_CMD and the test fake worker override."""
    assert CASTES[DEFAULT_CASTE].argv == ()
    for caste in CASTES.values():
        if not caste.argv:
            assert caste.name == DEFAULT_CASTE
            continue
        assert caste.argv[1] == "-m"
        module = caste.argv[2]
        assert importlib.util.find_spec(module) is not None, f"{caste.name} → {module}"


def test_every_caste_describes_itself_once() -> None:
    """The Settings roster and the tool schema both read `summary` — a caste
    described in two places drifts in two places."""
    for caste in CASTES.values():
        assert caste.summary.strip()
        assert caste.summary != caste.name
    assert len({c.summary for c in CASTES.values()}) == len(CASTES)


def test_an_unknown_caste_is_refused_naming_the_known_set() -> None:
    """v42: an unregistered caste silently ran the coding worker and the run was
    rejected downstream with no useful reason. Never a silent fallback (I9)."""
    assert resolve_caste(None).name == DEFAULT_CASTE
    assert resolve_caste("audit").name == "audit"
    with pytest.raises(ValueError) as excinfo:
        resolve_caste("nope")
    message = str(excinfo.value)
    assert "nope" in message
    for name in caste_names():
        assert name in message


def test_build_config_installs_exactly_the_registry(tmp_path: Path) -> None:
    """The literal that used to live in build_config is now the registry — and
    `coding` stays OUT of the table, or an empty argv would override the
    configured worker command."""
    from skep.supervisor.cli_cmds import build_config

    config = build_config(tmp_path / "home", None)
    assert config.caste_worker_commands == caste_worker_commands()
    assert DEFAULT_CASTE not in config.caste_worker_commands
    # The fallback still reaches the configured worker for the default caste.
    assert config.command_for(DEFAULT_CASTE) == config.worker_command
    assert config.command_for("audit") == CASTES["audit"].argv


def test_both_chat_caste_enums_offer_the_whole_roster() -> None:
    """v101-F12: the enums hardcoded ["coding", "audit"] and ["coding", "audit",
    "document"], so even after F2 and F3 the Queen could not ask for a verifier,
    a reviewer, a researcher or a script run. CLAUDE.md's standing rule applies
    — the Queen runs a small model and skims tool descriptions, so a schema
    omitting half the roster trains it never to use them."""
    from skep.supervisor.serve.tools import TOOL_SPECS

    def params(tool_name: str) -> dict[str, Any]:
        spec = next(s for s in TOOL_SPECS if s["function"]["name"] == tool_name)
        return cast("dict[str, Any]", spec["function"]["parameters"]["properties"])

    assert params("dispatch_run")["caste"]["enum"] == caste_names()
    batch = params("batch_dispatch")["tasks"]["items"]["properties"]
    assert batch["caste"]["enum"] == caste_names()


def test_the_dispatch_description_carries_each_summary_exactly_once() -> None:
    """Hand-written prose about castes is the F1 defect one layer up: a new
    caste ships and the description is silently stale. Generated from the same
    `summary` the Settings roster and the Assign field help read, so the
    operator and the model are told the same thing in the same words."""
    from skep.supervisor.serve.tools import _caste_guidance

    guidance = _caste_guidance()
    for name, caste in CASTES.items():
        assert guidance.count(caste.summary) == 1, name
        assert f"{name}: " in guidance


def test_the_schedules_enum_is_still_not_wired_to_the_registry() -> None:
    """Named in F1's docstring and worth a gate: the schedules enum mixes worker
    castes with supervisor-side schedule kinds and carries a real collision — a
    schedule of kind `script` runs a shell command on the SUPERVISOR host, which
    is not the `script` worker caste. Feeding the registry in would silently
    redefine an existing verb."""
    from skep.supervisor.serve.tools import TOOL_SPECS

    spec = next(s for s in TOOL_SPECS if s["function"]["name"] == "propose_schedule")
    kinds = spec["function"]["parameters"]["properties"]["caste"]["enum"]
    assert kinds != caste_names()
    assert "note" in kinds and "digest" in kinds  # schedule kinds, not castes
    assert "reviewer" not in kinds
