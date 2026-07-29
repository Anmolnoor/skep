"""Stage A: the workflow-template model + instantiate-and-fill (v3.5).

The load-bearing claim: a filled-in template is just a regular task — instantiate
produces the exact arguments a normal ``CodingWorkerTask`` is minted from, so the
whole feature needs **zero contract change**.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skep.supervisor.contracts_io import mint_task
from skep.supervisor.templates import (
    TemplateError,
    TemplateParam,
    WorkflowTemplate,
    instantiate,
    load_template_file,
    placeholder_names,
    template_from_dict,
    template_to_dict,
)
from skep.worker_contract import CodingWorkerTask


def _audit_template() -> WorkflowTemplate:
    return WorkflowTemplate(
        name="dep-audit",
        description="Nightly dependency audit",
        worker_kind="audit",
        instructions="Audit dependencies in {{ project }} and bump anything with a known advisory.",
        params=(TemplateParam(name="project", description="human label"),),
        network=("pypi.org",),
        shell_allowlist=(("python", "-m", "pytest"),),
        max_provider_calls=0,
    )


def test_placeholders_are_double_brace() -> None:
    assert placeholder_names("fix {{ a }} and {{b}} but not $shell or {plain}") == {"a", "b"}


def test_instantiate_fills_instructions() -> None:
    inst = instantiate(_audit_template(), {"project": "acme-api"}, repo="/repos/acme")
    assert (
        inst.instructions
        == "Audit dependencies in acme-api and bump anything with a known advisory."
    )
    assert inst.worker_kind == "audit"
    assert inst.repo == "/repos/acme"
    assert list(inst.permissions.network) == ["pypi.org"]
    assert inst.permissions.shell_allowlist == [["python", "-m", "pytest"]]
    assert inst.budget.max_provider_calls == 0


def test_instantiate_carries_git_mutation_permission() -> None:
    template = WorkflowTemplate(
        name="release",
        instructions="prepare a release commit",
        repo="/repos/acme",
        allow_git_mutation=True,
    )

    inst = instantiate(template)

    assert inst.permissions.allow_git_mutation is True


def test_default_used_when_param_absent() -> None:
    template = WorkflowTemplate(
        name="t",
        instructions="hello {{ who }}",
        params=(TemplateParam(name="who", default="world"),),
        repo="/r",
    )
    assert instantiate(template).instructions == "hello world"
    assert instantiate(template, {"who": "skep"}).instructions == "hello skep"


def test_missing_required_param_errors() -> None:
    with pytest.raises(TemplateError, match="missing required parameter"):
        instantiate(_audit_template(), {}, repo="/r")


def test_unknown_param_errors() -> None:
    with pytest.raises(TemplateError, match="unknown parameter"):
        instantiate(_audit_template(), {"project": "x", "bogus": "y"}, repo="/r")


def test_undeclared_placeholder_errors() -> None:
    template = WorkflowTemplate(name="t", instructions="hi {{ missing }}", repo="/r")
    with pytest.raises(TemplateError, match="undeclared parameter"):
        instantiate(template, {})


def test_duplicate_param_errors() -> None:
    template = WorkflowTemplate(
        name="t",
        instructions="{{ a }}",
        params=(TemplateParam(name="a"), TemplateParam(name="a")),
        repo="/r",
    )
    with pytest.raises(TemplateError, match="duplicate parameter"):
        instantiate(template, {"a": "1"})


def test_unknown_caste_errors() -> None:
    template = WorkflowTemplate(name="t", instructions="x", worker_kind="wizard", repo="/r")
    with pytest.raises(TemplateError, match="unknown worker_kind"):
        instantiate(template, {})


def test_no_repo_errors_but_override_supplies_one() -> None:
    template = WorkflowTemplate(name="t", instructions="x")
    with pytest.raises(TemplateError, match="no target repo"):
        instantiate(template, {})
    assert instantiate(template, {}, repo="/given").repo == "/given"


def test_repo_override_beats_template_default() -> None:
    template = WorkflowTemplate(name="t", instructions="x", repo="/default", ref="main")
    inst = instantiate(template, {}, repo="/override", ref="dev")
    assert inst.repo == "/override"
    assert inst.ref == "dev"
    # and the template default is used when nothing overrides
    assert instantiate(template, {}).repo == "/default"
    assert instantiate(template, {}).ref == "main"


def test_instance_mints_a_completely_normal_task() -> None:
    """The whole point: a filled template is just a regular CodingWorkerTask."""
    inst = instantiate(_audit_template(), {"project": "acme"}, repo="/repos/acme")
    task = mint_task(
        workspace=Path("/ws"),
        instructions=inst.instructions,
        worker_kind=inst.worker_kind,
        permissions=inst.permissions,
        budget=inst.budget,
    )
    assert isinstance(task, CodingWorkerTask)
    assert task.worker_kind == "audit"
    assert (
        task.instructions == "Audit dependencies in acme and bump anything with a known advisory."
    )
    assert task.permissions.network == ["pypi.org"]
    assert task.permissions.shell_allowlist == [["python", "-m", "pytest"]]
    assert task.budget.max_provider_calls == 0
    # round-trips through the contract validator unchanged — zero schema change
    assert CodingWorkerTask.model_validate_json(task.model_dump_json()) == task


def test_file_authoring_toml(tmp_path: Path) -> None:
    path = tmp_path / "audit.toml"
    path.write_text(
        """
name = "dep-audit"
description = "Nightly dependency audit"
worker_kind = "audit"
instructions = "Audit {{ project }} dependencies."
network = ["pypi.org"]
shell_allowlist = [["python", "-m", "pytest"]]
allow_git_mutation = true

[budget]
max_provider_calls = 0
wall_clock_seconds = 600

[[params]]
name = "project"
description = "human label"
""",
        encoding="utf-8",
    )
    template = load_template_file(path)
    assert template.name == "dep-audit"
    assert template.worker_kind == "audit"
    assert template.network == ("pypi.org",)
    assert template.shell_allowlist == (("python", "-m", "pytest"),)
    assert template.allow_git_mutation is True
    assert template.max_provider_calls == 0
    assert template.wall_clock_seconds == 600
    assert template.params == (TemplateParam(name="project", description="human label"),)


def test_file_authoring_json_round_trips_through_dict(tmp_path: Path) -> None:
    template = _audit_template()
    path = tmp_path / "audit.json"
    path.write_text(json.dumps(template_to_dict(template)), encoding="utf-8")
    loaded = load_template_file(path)
    assert loaded.name == template.name
    assert loaded.instructions == template.instructions
    assert loaded.worker_kind == template.worker_kind
    assert loaded.network == template.network
    assert loaded.shell_allowlist == template.shell_allowlist
    assert loaded.params == template.params
    assert loaded.max_provider_calls == 0


def test_file_authoring_rejects_unknown_extension(tmp_path: Path) -> None:
    path = tmp_path / "audit.yaml"
    path.write_text("name: x", encoding="utf-8")
    with pytest.raises(TemplateError, match=r"must be \.toml or \.json"):
        load_template_file(path)


def test_template_from_dict_requires_name_and_instructions() -> None:
    with pytest.raises(TemplateError, match="'name' is required"):
        template_from_dict({"instructions": "x"})
    with pytest.raises(TemplateError, match="'instructions' is required"):
        template_from_dict({"name": "x"})
