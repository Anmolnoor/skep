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
    RETIRED_CASTES,
    UNIMPLEMENTED_CASTES,
    caste_names,
    caste_worker_commands,
    resolve_caste,
)
from skep.worker_contract.task import KNOWN_WORKER_KINDS


def test_the_registry_covers_the_contract() -> None:
    """The contract is authoritative for WHICH names exist; the registry routes
    them. v101-F1 landed with `verifier` as a declared hole; v101-F2 wrote the
    worker; v127-F11 retired it (G10 already re-runs the pin on every patch)
    without removing it from the additive contract — so every contract name is
    routed, unimplemented or retired, and never two of those."""
    assert set(CASTES) | UNIMPLEMENTED_CASTES | set(RETIRED_CASTES) == set(KNOWN_WORKER_KINDS)
    assert set(CASTES).isdisjoint(UNIMPLEMENTED_CASTES)
    assert set(CASTES).isdisjoint(RETIRED_CASTES)
    assert not UNIMPLEMENTED_CASTES  # v101-F2 emptied it
    assert set(RETIRED_CASTES) == {"verifier"}


def test_a_retired_caste_is_refused_with_its_reason() -> None:
    """v127-F11 (I2): the verifier duplicated G10 and never ran in the field.
    Naming it is refused with the reason, never routed to the coding worker
    (the v42 fallback) and never run."""
    with pytest.raises(ValueError) as excinfo:
        resolve_caste("verifier")
    message = str(excinfo.value)
    assert "retired" in message
    assert "skep re-runs the project's pinned verify_command on every patch (G10)" in message
    assert "verifier" not in caste_worker_commands()


def test_a_retired_caste_never_reaches_a_worker(repo: Path, config: Any) -> None:
    """Every face that dispatches without submit_run (CLI run, scheduler tick,
    resume, batch) funnels through run_task; it refuses before any run row
    exists, and the scheduler records the refusal as the tick's state."""
    from skep.supervisor import RunStore, run_task

    with pytest.raises(ValueError, match="retired"):
        run_task(repo, "Verify.", config=config, worker_kind="verifier")
    store = RunStore(config.db_path)
    try:
        assert store.recent_runs() == []
    finally:
        store.close()


def test_a_stored_verifier_schedule_tick_records_the_refusal(repo: Path, config: Any) -> None:
    """A schedule stored before v127 still names `verifier`: its tick refuses
    and the schedule's state says why (I8), instead of running a coding worker."""
    from dataclasses import replace

    from skep.supervisor import RunStore
    from skep.supervisor.scheduler import make_schedule, run_due

    store = RunStore(config.db_path)
    try:
        stale = make_schedule(name="old-verify", repo=repo, instructions="x", interval_seconds=60)
        store.add_schedule(replace(stale, worker_kind="verifier"))
        results = run_due(store=store, config=config)
        assert [r.name for r in results] == ["old-verify"]
        assert results[0].task_id is None
        assert "retired" in results[0].state
        assert store.recent_runs() == []
    finally:
        store.close()


def test_a_retired_caste_is_refused_by_templates_and_schedules(tmp_path: Path) -> None:
    from skep.supervisor.scheduler import make_schedule
    from skep.supervisor.templates import TemplateError, WorkflowTemplate, validate_template

    with pytest.raises(TemplateError, match="retired"):
        validate_template(WorkflowTemplate(name="v", instructions="x", worker_kind="verifier"))
    with pytest.raises(ValueError, match="retired"):
        make_schedule(
            name="v", repo=tmp_path, instructions="x", interval_seconds=60, worker_kind="verifier"
        )


def test_the_envelope_verify_command_field_is_additive(tmp_path: Path) -> None:
    """Contract 0.3.4 (kept from the retired verifier's tests): `verify_command`
    is optional, so a 0.3.3 task without it parses unchanged."""
    import json

    from skep.supervisor.contracts_io import mint_task
    from skep.worker_contract import CodingWorkerTask

    task = mint_task(workspace=tmp_path, instructions="x", verify_command="uv run pytest")
    assert task.verify_command == "uv run pytest"
    raw = json.loads(task.model_dump_json())
    del raw["verify_command"]
    raw["contract_version"] = "0.3.3"
    assert CodingWorkerTask.model_validate(raw).verify_command == ""


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


ADVANCED = {"audit", "curator", "document", "researcher", "reviewer"}


def test_both_chat_caste_enums_offer_the_castes_that_run() -> None:
    """v101-F12 made the enums read the registry instead of hardcoding two
    names — one source of truth, kept. v127-F12 changes WHAT they offer: the
    whole roster trained the Queen's small model to reach for castes that had
    never run in the field (O9). The enums now offer the non-advanced castes;
    advanced ones stay reachable from the CLI, templates and schedules."""
    from skep.supervisor.castes import chat_caste_names
    from skep.supervisor.serve.tools import TOOL_SPECS

    def params(tool_name: str) -> dict[str, Any]:
        spec = next(s for s in TOOL_SPECS if s["function"]["name"] == tool_name)
        return cast("dict[str, Any]", spec["function"]["parameters"]["properties"])

    assert {name for name, caste in CASTES.items() if caste.advanced} == ADVANCED
    assert chat_caste_names() == sorted(set(CASTES) - ADVANCED) == ["coding", "script"]
    assert params("dispatch_run")["caste"]["enum"] == chat_caste_names()
    batch = params("batch_dispatch")["tasks"]["items"]["properties"]
    assert batch["caste"]["enum"] == chat_caste_names()


def test_an_advanced_caste_says_so_and_still_dispatches_from_the_cli(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.cli import main
    from skep.supervisor import cli_cmds

    for name in ADVANCED:
        assert CASTES[name].summary.startswith("Advanced"), name
        assert resolve_caste(name).name == name
    seen: list[str] = []

    def spy(*_args: Any, worker_kind: str, **_kwargs: Any) -> Any:
        seen.append(worker_kind)
        raise ValueError("stop here")

    monkeypatch.setattr(cli_cmds, "run_task", spy)
    main(["--home", str(tmp_path / "home"), "run", str(repo), "Find X.", "--caste", "researcher",
          "--quiet", "--execution-mode", "workspace"])  # fmt: skip
    assert seen == ["researcher"]


def test_aider_and_pi_are_marked_advanced() -> None:
    from skep.supervisor.engines import CODING_ENGINES

    for name in ("aider", "pi"):
        assert "advanced" in CODING_ENGINES[name].summary.lower(), name
    for name in ("builtin", "claude_code", "codex"):
        assert "advanced" not in CODING_ENGINES[name].summary.lower(), name


def test_advanced_is_recorded_once_on_the_flag() -> None:
    """Round 2: the hand-written "Advanced:" prefix duplicated the flag; the
    summary every surface reads now renders it from ``advanced``."""
    from skep.supervisor.castes import Caste
    from skep.supervisor.engines import CODING_ENGINES, CodingEngine

    entries: list[Caste | CodingEngine] = [*CASTES.values(), *CODING_ENGINES.values()]
    for entry in entries:
        assert "advanced" not in entry.about.lower(), entry.name
        assert entry.summary.startswith("Advanced") is entry.advanced, entry.name
    assert {n for n, e in CODING_ENGINES.items() if e.advanced} == {"aider", "pi"}


def test_the_dispatch_description_carries_each_summary_exactly_once() -> None:
    """Hand-written prose about castes is the F1 defect one layer up: a new
    caste ships and the description is silently stale. Generated from the same
    `summary` the Settings roster and the Assign field help read. v127-F12: it
    describes only what the enum offers, and names the advanced castes as not
    offered — the small model skims it, so it must stay true."""
    from skep.supervisor.castes import chat_caste_names
    from skep.supervisor.serve.tools import _caste_guidance

    guidance = _caste_guidance()
    for name in chat_caste_names():
        assert guidance.count(CASTES[name].summary) == 1, name
        assert f"{name}: " in guidance
    for name in ADVANCED:
        assert CASTES[name].summary not in guidance, name
        assert name in guidance  # named as advanced, not described as offered
    assert "skep run --caste" in guidance


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


def test_the_cli_faces_refuse_a_retired_caste_with_its_reason(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from skep.cli import main

    home = ["--home", str(tmp_path / "home")]
    assert main([*home, "schedule", "add", "v", str(repo), "x", "--every", "1d",
                 "--caste", "verifier"]) != 0  # fmt: skip
    assert "pinned verify_command on every patch (G10)" in capsys.readouterr().err
    assert main([*home, "run", str(repo), "Verify.", "--caste", "verifier", "--quiet",
                 "--execution-mode", "workspace"]) != 0  # fmt: skip
    assert "pinned verify_command on every patch (G10)" in capsys.readouterr().err
