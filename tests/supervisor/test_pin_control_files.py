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

_PACKAGE = {
    "name": "toy",
    "version": "1.0.0",
    "scripts": {"test": "node test/run.js", "check": "node tools/check.js --ci"},
    "devDependencies": {"jest": "29.0.0"},
}


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
        # round 3: runner config the blocklist missed (`.pytest.ini` wins over
        # pytest.ini; `.npmrc` `script-shell=true` makes `npm test` exit 0)
        ".pytest.ini",
        ".npmrc",
        "web/.npmrc",
        ".yarnrc",
        ".yarnrc.yml",
        "jest.config.js",
        "vitest.config.ts",
        ".mocharc.yml",
        ".coveragerc",
        # round 4: pytest 9's own config files win over pytest.ini/pyproject;
        # vitest reads vite.config's `test` block when no vitest.config exists;
        # setup.py is the project's build script (it runs during the re-prime)
        "pytest.toml",
        ".pytest.toml",
        "vite.config.ts",
        "vitest.workspace.ts",
        "setup.py",
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


@pytest.mark.parametrize(
    "extra",
    [
        '\n[project.entry-points.pytest11]\nrigged = "rigged_plugin"\n',
        '\n[tool.coverage.run]\nomit = ["*"]\n',
        '\n[tool.pytest_env]\nCI = "1"\n',
        # round 4: a patch-chosen build backend runs during the re-prime and,
        # once landed, during every baseline prime
        '\n[build-system]\nrequires = []\nbuild-backend = "evil"\nbackend-path = ["."]\n',
        # round 5: a console script named like the pin's tool replaces it
        # (`[project.scripts] pytest = "victim:ok"` made `uv run pytest` pass red)
        '\n[project.scripts]\npytest = "victim:ok"\n',
        '\n[project.gui-scripts]\npytest = "victim:ok"\n',
        '\n[project.entry-points.console_scripts]\npytest = "victim:ok"\n',
    ],
)
def test_pytest_plugins_and_coverage_in_pyproject_block(project_repo: Path, extra: str) -> None:
    assert _changes(project_repo, {"pyproject.toml": _PYPROJECT + extra}) == ("pyproject.toml",)


def test_unparseable_pyproject_blocks(project_repo: Path) -> None:
    assert _changes(project_repo, {"pyproject.toml": "[tool\n"}) == ("pyproject.toml",)


def _package(**changes: object) -> str:
    return json.dumps({**_PACKAGE, **changes}, indent=2) + "\n"


@pytest.mark.parametrize(
    "changes",
    [
        {"version": "1.0.1"},
        {"devDependencies": {"jest": "29.7.0"}},
        {"dependencies": {"left-pad": "1.3.0"}},
        {"overrides": {"semver": "7.5.4"}},
    ],
)
def test_a_package_json_dependency_bump_still_lands(
    project_repo: Path, changes: dict[str, object]
) -> None:
    assert _changes(project_repo, {"package.json": _package(**changes)}) == ()


@pytest.mark.parametrize(
    "changes",
    [
        {"scripts": {"test": "true"}},
        {"jest": {"testMatch": ["nothing"]}},
        {"mocha": {"spec": "none"}},
        {"ava": {"files": []}},
        {"type": "module"},
    ],
)
def test_any_other_package_json_change_blocks(
    project_repo: Path, changes: dict[str, object]
) -> None:
    assert _changes(project_repo, {"package.json": _package(**changes)}) == ("package.json",)


def test_the_pinned_script_itself_blocks(project_repo: Path) -> None:
    edits = {"scripts/check.py": "raise SystemExit(0)\n"}
    assert _changes(project_repo, edits, pin="python scripts/check.py") == ("scripts/check.py",)
    assert _changes(project_repo, edits, pin="./scripts/check.py") == ("scripts/check.py",)
    # round 3: every word of the pin, not the first two
    assert _changes(project_repo, edits, pin="uv run python scripts/check.py") == (
        "scripts/check.py",
    )
    # ... and inside an `sh -c` program
    assert _changes(project_repo, edits, pin="sh -c 'python scripts/check.py'") == (
        "scripts/check.py",
    )
    # a pin that reads a file the patch edits: G10's answer is the patch's to give
    pin = 'grep -q "value = 1" existing.py'
    assert _changes(project_repo, {"existing.py": "value = 1\n"}, pin=pin) == ("existing.py",)
    # an ordinary source edit under a pin that names no path is fine
    assert _changes(project_repo, {"existing.py": "value = 2\n"}) == ()


@pytest.mark.parametrize(
    ("pin", "script"),
    [
        ("npm test", "test/run.js"),
        ("npm run test", "test/run.js"),
        ("cd . && yarn test", "test/run.js"),
        ("pnpm run check", "tools/check.js"),
        ("npm run-script check -- --x", "tools/check.js"),
    ],
)
def test_the_file_a_package_script_runs_blocks(project_repo: Path, pin: str, script: str) -> None:
    """`npm test` → scripts.test → `node test/run.js`: that file is the check."""
    assert _changes(project_repo, {script: "process.exit(0)\n"}, pin=pin) == (script,)


def test_a_patch_that_edits_test_files_still_lands(project_repo: Path) -> None:
    """Only global switches block: G10 proves the pin passes on the patched
    tree, and a patch's own tests are what a pin like `uv run pytest` runs."""
    edits = {"tests/test_toy.py": "def test_ok():\n    assert True\n"}
    assert _changes(project_repo, edits) == ()
    assert _changes(project_repo, {"test/unit.js": "ok\n"}, pin="npm test") == ()


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
    assert "so skep can't land it by itself" in blocked
    assert "G10" not in blocked and "own test config" not in blocked


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
