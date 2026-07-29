"""Stage C: the template CLI — author (CLI + file), list, show, remove, run.

``run --template`` instantiates a saved template and dispatches a completely
normal task; here it runs the real (deterministic, offline) audit worker end to
end, and the stored run proves the parameter was substituted into the task.
"""

from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest

from skep.cli import main
from skep.supervisor import RunRecord, RunStore, WorkflowTemplate, cli_cmds, mint_task
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.ingest import IngestOutcome
from skep.worker_contract import Budget, Permissions
from tests.fixtures.toy_repo import create_audit_toy_repo


def _run_cli(home: Path, *args: str) -> int:
    return main(["--home", str(home), *args])


def _only_task_id(home: Path) -> str:
    store = RunStore(home / "supervisor" / "supervisor.sqlite3")
    try:
        runs = store.recent_runs(10)
        assert len(runs) == 1
        return runs[0].task_id
    finally:
        store.close()


def test_template_crud_via_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "home"
    code = _run_cli(
        home,
        "template",
        "add",
        "dep-audit",
        "--caste",
        "audit",
        "--instructions",
        "Audit {{ project }} dependencies.",
        "--param",
        "project",
        "--network",
        "pypi.org",
        "--shell-allow",
        "python -m pytest",
        "--allow-git-mutation",
        "--budget-max-provider-calls",
        "0",
        "--description",
        "Nightly dep audit",
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "template 'dep-audit' saved" in out
    assert "--param project=..." in out  # the run hint names the required param

    assert _run_cli(home, "template", "list") == 0
    out = capsys.readouterr().out
    assert "dep-audit" in out
    assert "audit" in out
    assert "project" in out

    assert _run_cli(home, "template", "show", "dep-audit") == 0
    out = capsys.readouterr().out
    assert "caste:        audit" in out
    assert "Audit {{ project }} dependencies." in out
    assert "project" in out
    assert "required" in out
    assert "pypi.org" in out
    assert "python -m pytest" in out
    assert "git mutation: yes" in out

    assert _run_cli(home, "template", "remove", "dep-audit") == 0
    assert "removed template 'dep-audit'" in capsys.readouterr().out
    assert _run_cli(home, "template", "show", "dep-audit") == 2  # gone


def test_template_delete_alias_removes_template(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    assert (
        _run_cli(
            home,
            "template",
            "add",
            "dep-audit",
            "--caste",
            "audit",
            "--instructions",
            "Audit dependencies.",
        )
        == 0
    )
    capsys.readouterr()

    assert _run_cli(home, "template", "delete", "dep-audit") == 0
    assert "removed template 'dep-audit'" in capsys.readouterr().out
    assert _run_cli(home, "template", "show", "dep-audit") == 2


def test_template_rename_via_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "home"
    assert (
        _run_cli(
            home,
            "template",
            "add",
            "dep-audit",
            "--caste",
            "audit",
            "--instructions",
            "Audit dependencies.",
        )
        == 0
    )
    capsys.readouterr()

    assert _run_cli(home, "template", "rename", "dep-audit", "dependency-audit") == 0
    assert "renamed template 'dep-audit' -> 'dependency-audit'" in capsys.readouterr().out
    assert _run_cli(home, "template", "show", "dep-audit") == 2
    assert _run_cli(home, "template", "show", "dependency-audit") == 0
    assert "Audit dependencies." in capsys.readouterr().out


def test_template_add_from_toml_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "home"
    spec = tmp_path / "audit.toml"
    spec.write_text(
        """
name = "from-file"
description = "authored from a file"
worker_kind = "audit"
instructions = "Audit {{ project }} dependencies."
network = ["pypi.org"]

[budget]
max_provider_calls = 0

[[params]]
name = "project"
description = "human label"
""",
        encoding="utf-8",
    )
    assert _run_cli(home, "template", "add", "--from", str(spec)) == 0
    assert "template 'from-file' saved" in capsys.readouterr().out
    assert _run_cli(home, "template", "show", "from-file") == 0
    out = capsys.readouterr().out
    assert "caste:        audit" in out
    assert "human label" in out


def test_template_suggest_previews_and_saves_from_remembered_approvals(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    config = build_config(home, None)
    store = RunStore(config.db_path)
    try:
        task = mint_task(workspace=repo, instructions="Add a login page with JWT support")
        store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
        store.transition(task.task_id, "completed")
        store.record_approval_ledger(
            task_id=task.task_id,
            action="network.fetch",
            resource="https://pypi.org/simple/pyjwt/",
            reason="install PyJWT",
            approved_by="tester",
            remembered=True,
        )
        store.record_approval_ledger(
            task_id=task.task_id,
            action="shell.run",
            resource="python -m pytest",
            reason="run tests",
            approved_by="tester",
            remembered=True,
        )
        store.record_approval_ledger(
            task_id=task.task_id,
            action="git.commit",
            resource="git commit",
            reason="commit verified patch",
            approved_by="tester",
            remembered=True,
        )
    finally:
        store.close()

    code = _run_cli(home, "template", "suggest", "web-feature", str(repo), "Add a signup page")
    out = capsys.readouterr().out
    assert code == 0, out
    assert "suggested template 'web-feature'" in out
    assert "pypi.org" in out
    assert "python -m pytest" in out
    assert "git mutation: yes" in out
    assert "--save" in out

    store = RunStore(config.db_path)
    try:
        assert store.get_template("web-feature") is None
    finally:
        store.close()

    code = _run_cli(
        home,
        "template",
        "suggest",
        "web-feature",
        str(repo),
        "Add a signup page",
        "--save",
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "template 'web-feature' saved from 3 remembered approval(s)" in out

    store = RunStore(config.db_path)
    try:
        template = store.get_template("web-feature")
        assert template is not None
        assert template.provenance == "learned"
        assert template.repo == str(repo)
        assert template.instructions == "Add a signup page"
        assert template.network == ("pypi.org",)
        assert template.shell_allowlist == (("python", "-m", "pytest"),)
        assert template.allow_git_mutation is True
    finally:
        store.close()


def test_run_template_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "home"
    repo = create_audit_toy_repo(tmp_path / "repo")
    assert (
        _run_cli(
            home,
            "template",
            "add",
            "nightly-audit",
            "--caste",
            "audit",
            "--instructions",
            "Audit {{ project }} dependencies and bump known advisories.",
            "--param",
            "project",
            "--budget-max-provider-calls",
            "0",
        )
        == 0
    )
    capsys.readouterr()

    code = _run_cli(
        home, "run", "--template", "nightly-audit", str(repo), "--param", "project=acme", "--quiet"
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "state:        completed" in out

    task_id = _only_task_id(home)
    store = RunStore(home / "supervisor" / "supervisor.sqlite3")
    try:
        reverify = store.reverification_for(task_id)
        assert reverify is not None and reverify.confirmed  # G10 confirms the template run
        run = store.get_run(task_id)
        assert run is not None
        # the {{ project }} parameter was substituted into the real, normal task
        assert run.instructions == "Audit acme dependencies and bump known advisories."
    finally:
        store.close()


def test_run_auto_matches_saved_template_permissions(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    repo = create_audit_toy_repo(tmp_path / "repo")
    config = build_config(home, None)
    store = RunStore(config.db_path)
    try:
        store.add_template(
            WorkflowTemplate(
                name="web-feature",
                instructions="Add a login page",
                repo=str(repo),
                network=("pypi.org",),
                shell_allowlist=(("python", "-m", "pytest"),),
                allow_git_mutation=True,
                max_provider_calls=7,
                provenance="learned",
            )
        )
    finally:
        store.close()

    observed: dict[str, object] = {}

    def fake_run_task(*args: object, **kwargs: object) -> IngestOutcome:
        observed["instructions"] = args[1]
        observed["permissions"] = kwargs["permissions"]
        observed["budget"] = kwargs["budget"]
        observed["worker_kind"] = kwargs["worker_kind"]
        return IngestOutcome(
            record=RunRecord(
                task_id="matched-run",
                trace_id="trace-matched-run",
                repo=str(repo),
                ref=None,
                workspace=str(repo),
                execution_mode="workspace",
                instructions=str(args[1]),
                state="completed",
                summary="matched",
                verification_outcome="passed",
                verification_details="ok",
                worker_version="fake",
                manifest_fingerprint="f" * 64,
                resume_of=None,
                created_at="2026-06-26T00:00:00Z",
                updated_at="2026-06-26T00:00:00Z",
            ),
            review_id=None,
        )

    monkeypatch.setattr(cli_cmds, "run_task", fake_run_task)

    code = _run_cli(
        home,
        "run",
        str(repo),
        "Add a signup page",
        "--execution-mode",
        "workspace",
        "--quiet",
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert (
        "matched template: web-feature (network: pypi.org; shell: python -m pytest; git: yes)"
    ) in out

    permissions = observed["permissions"]
    budget = observed["budget"]
    assert isinstance(permissions, Permissions)
    assert isinstance(budget, Budget)
    assert observed["instructions"] == "Add a signup page"
    assert observed["worker_kind"] == "coding"
    assert permissions.network == ["pypi.org"]
    assert permissions.shell_allowlist == [["python", "-m", "pytest"]]
    assert permissions.allow_git_mutation is True
    assert budget.max_provider_calls == 7


def test_run_quick_picks_when_multiple_templates_match(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    repo = create_audit_toy_repo(tmp_path / "repo")
    config = build_config(home, None)
    store = RunStore(config.db_path)
    try:
        store.add_template(
            WorkflowTemplate(
                name="auth-page",
                instructions="Update auth module page",
                repo=str(repo),
                network=("pypi.org",),
                provenance="learned",
            )
        )
        store.add_template(
            WorkflowTemplate(
                name="auth-tests",
                instructions="Update auth module tests",
                repo=str(repo),
                shell_allowlist=(("python", "-m", "pytest"),),
                provenance="learned",
            )
        )
    finally:
        store.close()

    observed: dict[str, object] = {}

    def fake_run_task(*args: object, **kwargs: object) -> IngestOutcome:
        observed["permissions"] = kwargs["permissions"]
        return IngestOutcome(
            record=RunRecord(
                task_id="picked-run",
                trace_id="trace-picked-run",
                repo=str(repo),
                ref=None,
                workspace=str(repo),
                execution_mode="workspace",
                instructions=str(args[1]),
                state="completed",
                summary="picked",
                verification_outcome="passed",
                verification_details="ok",
                worker_version="fake",
                manifest_fingerprint="f" * 64,
                resume_of=None,
                created_at="2026-06-26T00:00:00Z",
                updated_at="2026-06-26T00:00:00Z",
            ),
            review_id=None,
        )

    monkeypatch.setattr(cli_cmds, "_stdin_is_interactive", lambda: True, raising=False)
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "2")
    monkeypatch.setattr(cli_cmds, "run_task", fake_run_task)

    code = _run_cli(
        home,
        "run",
        str(repo),
        "Update auth module",
        "--execution-mode",
        "workspace",
        "--quiet",
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "multiple templates match this task" in out
    assert "[1] auth-page (network: pypi.org)" in out
    assert "[2] auth-tests (shell: python -m pytest)" in out
    assert "[3] no template (start minimal)" in out
    assert "matched template: auth-tests (shell: python -m pytest)" in out

    permissions = observed["permissions"]
    assert isinstance(permissions, Permissions)
    assert permissions.network == []
    assert permissions.shell_allowlist == [["python", "-m", "pytest"]]


def test_run_quick_pick_no_template_starts_minimal(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    repo = create_audit_toy_repo(tmp_path / "repo")
    config = build_config(home, None)
    store = RunStore(config.db_path)
    try:
        store.set_setting("default_network", ["example.com"])
        store.set_setting("default_env_allowlist", ["EXAMPLE_TOKEN"])
        store.add_template(
            WorkflowTemplate(
                name="auth-page",
                instructions="Update auth module page",
                repo=str(repo),
                network=("pypi.org",),
                provenance="learned",
            )
        )
        store.add_template(
            WorkflowTemplate(
                name="auth-tests",
                instructions="Update auth module tests",
                repo=str(repo),
                shell_allowlist=(("python", "-m", "pytest"),),
                provenance="learned",
            )
        )
    finally:
        store.close()

    observed: dict[str, object] = {}

    def fake_run_task(*args: object, **kwargs: object) -> IngestOutcome:
        observed["permissions"] = kwargs["permissions"]
        return IngestOutcome(
            record=RunRecord(
                task_id="picked-minimal-run",
                trace_id="trace-picked-minimal-run",
                repo=str(repo),
                ref=None,
                workspace=str(repo),
                execution_mode="workspace",
                instructions=str(args[1]),
                state="completed",
                summary="picked minimal",
                verification_outcome="passed",
                verification_details="ok",
                worker_version="fake",
                manifest_fingerprint="f" * 64,
                resume_of=None,
                created_at="2026-06-26T00:00:00Z",
                updated_at="2026-06-26T00:00:00Z",
            ),
            review_id=None,
        )

    monkeypatch.setattr(cli_cmds, "_stdin_is_interactive", lambda: True, raising=False)
    monkeypatch.setattr(cli_cmds, "_read_single_key", lambda: "3", raising=False)
    monkeypatch.setattr(cli_cmds, "run_task", fake_run_task)

    code = _run_cli(
        home,
        "run",
        str(repo),
        "Update auth module",
        "--execution-mode",
        "workspace",
        "--quiet",
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "[3] no template (start minimal)" in out
    assert "matched template" not in out

    permissions = observed["permissions"]
    assert isinstance(permissions, Permissions)
    assert permissions.network == []
    assert permissions.env_allowlist == []
    assert permissions.shell_allowlist == []
    assert permissions.allow_git_mutation is False


def test_run_no_template_skips_auto_match(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    repo = create_audit_toy_repo(tmp_path / "repo")
    config = build_config(home, None)
    store = RunStore(config.db_path)
    try:
        store.add_template(
            WorkflowTemplate(
                name="web-feature",
                instructions="Add a login page",
                repo=str(repo),
                network=("pypi.org",),
                provenance="learned",
            )
        )
    finally:
        store.close()

    observed: dict[str, object] = {}

    def fake_run_task(*args: object, **kwargs: object) -> IngestOutcome:
        observed["permissions"] = kwargs["permissions"]
        return IngestOutcome(
            record=RunRecord(
                task_id="plain-run",
                trace_id="trace-plain-run",
                repo=str(repo),
                ref=None,
                workspace=str(repo),
                execution_mode="workspace",
                instructions=str(args[1]),
                state="completed",
                summary="plain",
                verification_outcome="passed",
                verification_details="ok",
                worker_version="fake",
                manifest_fingerprint="f" * 64,
                resume_of=None,
                created_at="2026-06-26T00:00:00Z",
                updated_at="2026-06-26T00:00:00Z",
            ),
            review_id=None,
        )

    monkeypatch.setattr(cli_cmds, "run_task", fake_run_task)

    code = _run_cli(
        home,
        "run",
        str(repo),
        "Add a signup page",
        "--no-template",
        "--execution-mode",
        "workspace",
        "--quiet",
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "matched template" not in out

    permissions = observed["permissions"]
    assert isinstance(permissions, Permissions)
    assert permissions.network == []


def test_run_minimal_forces_deny_all_permissions(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    repo = create_audit_toy_repo(tmp_path / "repo")
    config = build_config(home, None)
    store = RunStore(config.db_path)
    try:
        store.add_template(
            WorkflowTemplate(
                name="web-feature",
                instructions="Add a login page",
                repo=str(repo),
                network=("pypi.org",),
                shell_allowlist=(("python", "-m", "pytest"),),
                allow_git_mutation=True,
                provenance="learned",
            )
        )
    finally:
        store.close()

    observed: dict[str, object] = {}

    def fake_run_task(*args: object, **kwargs: object) -> IngestOutcome:
        observed["permissions"] = kwargs["permissions"]
        return IngestOutcome(
            record=RunRecord(
                task_id="minimal-run",
                trace_id="trace-minimal-run",
                repo=str(repo),
                ref=None,
                workspace=str(repo),
                execution_mode="workspace",
                instructions=str(args[1]),
                state="completed",
                summary="minimal",
                verification_outcome="passed",
                verification_details="ok",
                worker_version="fake",
                manifest_fingerprint="f" * 64,
                resume_of=None,
                created_at="2026-06-26T00:00:00Z",
                updated_at="2026-06-26T00:00:00Z",
            ),
            review_id=None,
        )

    monkeypatch.setattr(cli_cmds, "run_task", fake_run_task)

    code = _run_cli(
        home,
        "run",
        str(repo),
        "Add a signup page",
        "--minimal",
        "--execution-mode",
        "workspace",
        "--quiet",
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "matched template" not in out

    permissions = observed["permissions"]
    assert isinstance(permissions, Permissions)
    assert permissions.network == []
    assert permissions.env_allowlist == []
    assert permissions.shell_allowlist == []
    assert permissions.allow_git_mutation is False


def test_run_template_inherits_project_policy_bound_by_template_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    repo = create_audit_toy_repo(tmp_path / "repo")
    assert (
        _run_cli(
            home,
            "template",
            "add",
            "nightly-audit",
            "--caste",
            "audit",
            "--instructions",
            "Audit {{ project }} dependencies and bump known advisories.",
            "--param",
            "project",
            "--budget-max-provider-calls",
            "0",
        )
        == 0
    )
    capsys.readouterr()

    config = build_config(home, None)
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="project-1",
            name="template-bound project",
            strategy="trusted_local_dev",
            phase="build",
            policy={
                "default_execution_mode": "workspace",
                "default_wall_clock_seconds": 321,
            },
        )
        store.add_project_binding(
            project_id="project-1",
            binding_kind="template_name",
            binding_value="nightly-audit",
        )
    finally:
        store.close()

    code = _run_cli(
        home, "run", "--template", "nightly-audit", str(repo), "--param", "project=acme", "--quiet"
    )
    out = capsys.readouterr().out
    assert code == 0, out

    task_id = _only_task_id(home)
    store = RunStore(home / "supervisor" / "supervisor.sqlite3")
    try:
        run = store.get_run(task_id)
        assert run is not None
        assert run.execution_mode == "workspace"
    finally:
        store.close()

    task = json.loads((config.audit_dir / task_id / "task.json").read_text())
    assert task["budget"]["wall_clock_seconds"] == 900
    assert task["budget"]["max_provider_calls"] == 0


def test_run_template_unknown_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = _run_cli(tmp_path / "home", "run", "--template", "nope")
    captured = capsys.readouterr()
    assert code == 2
    assert "no template named 'nope'" in captured.err
    assert "skep template list" in captured.err


def test_run_template_missing_required_param_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    repo = create_audit_toy_repo(tmp_path / "repo")
    _run_cli(
        home,
        "template",
        "add",
        "t",
        "--caste",
        "audit",
        "--instructions",
        "do {{ x }}",
        "--param",
        "x",
    )
    capsys.readouterr()
    code = _run_cli(home, "run", "--template", "t", str(repo), "--quiet")
    captured = capsys.readouterr()
    assert code == 2
    assert "missing required parameter" in captured.err


def test_run_template_rejects_inline_instructions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    repo = create_audit_toy_repo(tmp_path / "repo")
    _run_cli(home, "template", "add", "t", "--caste", "audit", "--instructions", "do it")
    capsys.readouterr()
    code = _run_cli(home, "run", "--template", "t", str(repo), "also do this")
    captured = capsys.readouterr()
    assert code == 2
    assert "instructions come from the template" in captured.err


def test_run_without_template_requires_repo_and_instructions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _run_cli(tmp_path / "home", "run")
    captured = capsys.readouterr()
    assert code == 2
    assert "needs a repo and instructions" in captured.err


def test_template_add_rejects_unknown_caste(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _run_cli(
        tmp_path / "home", "template", "add", "t", "--caste", "wizard", "--instructions", "x"
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "unknown worker_kind" in captured.err


def test_schedule_add_template_then_tick(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Author once, then bind a schedule to the template and tick it (the round-trip)."""
    home = tmp_path / "home"
    repo = create_audit_toy_repo(tmp_path / "repo")
    assert (
        _run_cli(
            home,
            "template",
            "add",
            "nightly-audit",
            "--caste",
            "audit",
            "--instructions",
            "Audit {{ project }} dependencies.",
            "--param",
            "project",
            "--budget-max-provider-calls",
            "0",
        )
        == 0
    )
    capsys.readouterr()

    # bind a schedule to the template
    assert (
        _run_cli(
            home,
            "schedule",
            "add",
            "job",
            str(repo),
            "--template",
            "nightly-audit",
            "--param",
            "project=acme",
            "--every",
            "1d",
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "template 'nightly-audit'" in out

    # the list names the template binding as the source
    assert _run_cli(home, "schedule", "list") == 0
    out = capsys.readouterr().out
    assert "template nightly-audit" in out

    # tick dispatches the bound schedule through the normal spine
    assert _run_cli(home, "tick") == 0
    out = capsys.readouterr().out
    assert "ran 'job'" in out
    assert "completed" in out

    task_id = _only_task_id(home)
    store = RunStore(home / "supervisor" / "supervisor.sqlite3")
    try:
        run = store.get_run(task_id)
        assert run is not None
        assert run.instructions == "Audit acme dependencies."  # param flowed through the binding
        reverify = store.reverification_for(task_id)
        assert reverify is not None and reverify.confirmed
    finally:
        store.close()
