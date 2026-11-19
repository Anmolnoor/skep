"""v127-F5 round 2 (I2): a patch that rewrites what the pin runs never auto-lands.

G10 re-runs the project's pinned check on the PATCHED tree, so a patch that
edits `conftest.py`, the pytest/tox config or the pinned script itself gets
the check it wrote. The supervisor reads that from the patch artifact it is
about to apply (never the worker's claim) and sends the run to the normal
approval card. Dependency bumps in `pyproject.toml`/`package.json` still land:
those files are compared section by section, not "touched".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skep.supervisor import RunStore, SupervisorConfig, run_task
from skep.supervisor.policy import (
    VERIFIED_PATCH_RULE,
    AutoApprovalContext,
    pin_control_changes,
    rule_block_reason,
)

from .conftest import git

_PYPROJECT = """\
[project]
name = "toy"
dependencies = ["requests==2.31.0"]

[tool.pytest.ini_options]
addopts = "-q"
"""

_PACKAGE = {"name": "toy", "version": "1.0.0", "scripts": {"test": "jest"}}


def _changes(repo: Path, edits: dict[str, str], pin: str = "uv run pytest") -> tuple[str, ...]:
    """Commit nothing: write ``edits``, diff them into a patch, reset, judge."""
    base = git(repo, "rev-parse", "HEAD").stdout.strip()
    for name, text in edits.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git(repo, "add", "-A")
    patch = repo.parent / "change.patch"
    patch.write_text(git(repo, "diff", "--cached", "--binary", base).stdout)
    git(repo, "reset", "-q", "--hard", base)
    git(repo, "clean", "-qfd")
    changed = tuple(sorted(edits))
    return pin_control_changes(repo, patch, base, changed, (pin,))


@pytest.fixture()
def project_repo(repo: Path) -> Path:
    (repo / "pyproject.toml").write_text(_PYPROJECT)
    (repo / "package.json").write_text(json.dumps(_PACKAGE, indent=2) + "\n")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "check.py").write_text("print('ok')\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "project files")
    return repo


@pytest.mark.parametrize(
    "name",
    [
        "conftest.py",
        "tests/conftest.py",
        "pytest.ini",
        "tox.ini",
        "setup.cfg",
        "noxfile.py",
        "Makefile",
    ],
)
def test_a_test_control_file_blocks(project_repo: Path, name: str) -> None:
    assert _changes(project_repo, {name: "x = 1\n"}) == (name,)


def test_a_dependency_bump_in_pyproject_still_lands(project_repo: Path) -> None:
    bumped = _PYPROJECT.replace("2.31.0", "2.32.3")
    assert _changes(project_repo, {"pyproject.toml": bumped}) == ()


def test_pytest_config_in_pyproject_blocks(project_repo: Path) -> None:
    rigged = _PYPROJECT.replace('addopts = "-q"', 'addopts = "-q --co"')
    assert _changes(project_repo, {"pyproject.toml": rigged}) == ("pyproject.toml",)


def test_hatch_test_envs_in_pyproject_block(project_repo: Path) -> None:
    rigged = _PYPROJECT + '\n[tool.hatch.envs.test.scripts]\nrun = "true"\n'
    assert _changes(project_repo, {"pyproject.toml": rigged}) == ("pyproject.toml",)


def test_unparseable_pyproject_blocks(project_repo: Path) -> None:
    assert _changes(project_repo, {"pyproject.toml": "[tool\n"}) == ("pyproject.toml",)


def test_package_json_blocks_only_when_scripts_change(project_repo: Path) -> None:
    bumped = json.dumps({**_PACKAGE, "version": "1.0.1"}, indent=2) + "\n"
    assert _changes(project_repo, {"package.json": bumped}) == ()
    rigged = json.dumps({**_PACKAGE, "scripts": {"test": "true"}}, indent=2) + "\n"
    assert _changes(project_repo, {"package.json": rigged}) == ("package.json",)


def test_the_pinned_script_itself_blocks(project_repo: Path) -> None:
    edits = {"scripts/check.py": "raise SystemExit(0)\n"}
    assert _changes(project_repo, edits, pin="python scripts/check.py") == ("scripts/check.py",)
    assert _changes(project_repo, edits, pin="./scripts/check.py") == ("scripts/check.py",)
    # an ordinary source edit under a pin that names no path is fine
    assert _changes(project_repo, {"existing.py": "value = 2\n"}) == ()


def test_every_rule_refuses_a_patch_that_rewrites_the_check() -> None:
    ctx = AutoApprovalContext(
        verification_outcome="passed",
        reverify_confirmed=True,
        changed_files=("conftest.py",),
        verify_pinned=True,
        reverify_commands=("uv run pytest",),
        pin_control_files=("conftest.py",),
    )
    blocked = rule_block_reason(VERIFIED_PATCH_RULE, ctx)
    assert blocked is not None and "conftest.py" in blocked
    assert "landable by hand" in blocked


def test_a_maintain_run_that_edits_conftest_waits_for_a_human(
    repo: Path, config: SupervisorConfig
) -> None:
    (repo / "conftest.py").write_text("value = 0\n")
    git(repo, "add", "conftest.py")
    git(repo, "commit", "-qm", "conftest")
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="ctl",
            name="ctl",
            strategy="trusted_local_dev",
            phase="maintain",
            policy={
                "default_execution_mode": "workspace",
                "auto_apply_verified_patch": True,
                "auto_apply_branch": "skep/maintain",
                "verify_command": 'grep -q "value = 1" conftest.py',
            },
        )
        store.add_project_binding(
            project_id="ctl", binding_kind="repo_path", binding_value=str(repo)
        )
    finally:
        store.close()
    outcome = run_task(
        repo, "Fix. MODE:happy FILE:conftest.py", config=config, auto_apply_verified_patch=True
    )
    assert outcome.record.state == "completed"
    store = RunStore(config.db_path)
    try:
        assert store.reverification_for(outcome.record.task_id).confirmed  # type: ignore[union-attr]
        assert store.approvals_for(outcome.record.task_id) == [], "the run auto-landed"
        details = [d for _, d, _ in store.transitions_for(outcome.record.task_id) if d]
    finally:
        store.close()
    assert not git(repo, "branch", "--list", "skep/maintain").stdout.strip()
    blocked = next(
        json.loads(d)["auto_apply_blocked"] for d in details if "auto_apply_blocked" in d
    )
    assert "conftest.py" in " ".join(str(v) for v in blocked.values())
