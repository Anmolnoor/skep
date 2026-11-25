"""v133 B1: the floor and policy rules (plans/v133, findings A1, A3, A6, C2,
F-I1, J9)."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from skep.supervisor.secret_floor import shell_secret_floor_reason
from skep.supervisor.shell_prefixes import SKEP_SELF_REFUSAL, queen_skep_refusal

# --- F1 (A1): a word too long to be a file name is not a path ---------------

_LONG = "word" * 75  # 300 bytes: one component over NAME_MAX (255)


@pytest.fixture
def py312_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    """``Path.exists`` as Python 3.12/3.13 (CI, Alfin) has it: an errno it
    does not ignore (ENAMETOOLONG) raises. 3.14 swallows every OSError, so
    without this the crash only shows on the older interpreters."""

    def exists(self: Path, *, follow_symlinks: bool = True) -> bool:
        try:
            os.stat(self, follow_symlinks=follow_symlinks)
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP):
                return False
            raise
        except ValueError:
            return False
        return True

    monkeypatch.setattr(Path, "exists", exists)


def _skep_home(tmp_path: Path) -> Path:
    home = tmp_path / "skep" / "supervisor"
    home.mkdir(parents=True)
    return home


@pytest.mark.usefixtures("py312_exists")
@pytest.mark.parametrize(
    "command",
    [
        f'claude -p "{_LONG}"',
        f"cat >> pr73.md <<'BODYEOF'\nsome prose {_LONG}\nBODYEOF",
        f'claude -p "{_LONG}*"',  # a glob too: iglob over the long word
    ],
    ids=["prompt", "heredoc", "glob"],
)
def test_f1_long_token_is_not_a_path(tmp_path: Path, command: str) -> None:
    home = _skep_home(tmp_path)
    assert queen_skep_refusal(command, home=home, cwd=str(tmp_path)) is None
    assert shell_secret_floor_reason(command, cwd=str(tmp_path)) is None


@pytest.mark.usefixtures("py312_exists")
def test_f1_protected_path_beside_long_token_still_refused(tmp_path: Path) -> None:
    home = _skep_home(tmp_path)
    serve_env = tmp_path / "skep" / "serve.env"
    command = f'echo "{_LONG}" {serve_env}'
    assert queen_skep_refusal(command, home=home, cwd=str(tmp_path)) == SKEP_SELF_REFUSAL
    reason = shell_secret_floor_reason(command, cwd=str(tmp_path), skep_home=home)
    assert reason is not None and "skep's own secrets" in reason


def test_f1_other_probe_error_keeps_the_word_judged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe that fails for any other reason never raises and never lets
    the word go unjudged: it is judged as if it exists."""
    home = _skep_home(tmp_path)
    real_stat = os.stat

    def stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if str(path).endswith("serve.env"):
            raise PermissionError(errno.EACCES, "denied", str(path))
        return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "stat", stat)
    # a bare relative word, run from the skep home's parent
    assert queen_skep_refusal("cat serve.env", home=home, cwd=str(home.parent)) == (
        SKEP_SELF_REFUSAL
    )


# --- F2 (A6): no standing grant for a bare interpreter or its eval flag -----

from skep.supervisor import RunStore  # noqa: E402
from skep.supervisor.cli_cmds import build_config  # noqa: E402
from skep.supervisor.policy_resolver import grant_repo, worker_shell_grants  # noqa: E402
from skep.supervisor.shell_prefixes import dangerous_prefix_reason  # noqa: E402


@pytest.mark.parametrize(
    "prefix",
    [
        ["node"],
        ["/usr/local/bin/node"],
        ["node", "-e"],
        ["node", "--eval"],
        ["node", "-p"],
        ["node", "--print"],
        ["deno"],
        ["deno", "eval"],
        ["bun"],
        ["bun", "-e"],
        ["ruby"],
        ["ruby", "-e"],
        ["perl"],
        ["perl", "-e"],
        ["perl", "-E"],
        ["php"],
        ["php", "-r"],
        ["lua"],
        ["lua", "-e"],
        ["pwsh"],
        ["pwsh", "-c"],
        ["pwsh", "-Command"],
        ["pwsh", "-EncodedCommand"],
        ["awk"],
    ],
)
def test_f2_interpreter_and_eval_flag_refused(prefix: list[str]) -> None:
    reason = dangerous_prefix_reason(prefix)
    assert reason is not None and "too broad" in reason, (prefix, reason)


@pytest.mark.parametrize(
    "prefix",
    [["node", "script.cjs"], ["node", ".skep-smoke.cjs"], ["ruby", "bin/setup"], ["deno", "test"]],
)
def test_f2_interpreter_with_a_script_still_grantable(prefix: list[str]) -> None:
    assert dangerous_prefix_reason(prefix) is None


def test_f2_a_stored_bare_interpreter_grant_stops_matching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare ``node`` grant remembered before v133 is dropped on read, so the
    next ``node -e`` cards again; a script grant beside it still holds."""
    from skep.supervisor import policy_engine, shell_prefixes
    from skep.supervisor.serve import actions

    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    home, repo = tmp_path / "home", tmp_path / "repo"
    repo.mkdir()
    config = build_config(home, None)
    store = RunStore(config.db_path)
    try:
        with monkeypatch.context() as old_build:  # the write as a pre-v133 build made it
            for module in (shell_prefixes, policy_engine):
                old_build.setattr(module, "dangerous_prefix_reason", lambda _p: None)
            for command in (["node"], ["node", "-e"], ["node", "script.cjs"]):
                grant = actions._worker_grant(command, "project", {"repo": grant_repo(repo)})
                actions.store_grant(store, grant)
        grants = worker_shell_grants(store, config.home, repo, None)
    finally:
        store.close()
    assert "node script.cjs" in grants
    assert "node" not in grants and "node -e" not in grants, grants


def test_f2_learnable_description_says_prefix() -> None:
    import json

    from skep.supervisor import policy_templates

    text = json.loads((Path(policy_templates.__file__).parent / "descriptions.json").read_text())[
        "builder"
    ]["shell-learnable"]
    assert "prefix" in text, text


# --- F3 (A3): a vendored manifest is not test tampering, and still blocks ---

import json  # noqa: E402

from skep.supervisor.policy import (  # noqa: E402
    VERIFIED_PATCH_RULE,
    AutoApprovalContext,
    rule_block_reason,
)
from skep.supervisor.policy import (  # noqa: E402
    test_config_changes as runner_config_changes,  # not collected as a test
)

from .conftest import git  # noqa: E402

_PKG = {"name": "dep", "version": "1.0.0", "scripts": {"test": "mocha"}}


def _config_changes(repo: Path, edits: dict[str, str]) -> tuple[str, ...]:
    (repo / "web").mkdir()
    (repo / "web" / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD").stdout.strip()
    for name, text in edits.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git(repo, "add", "-A")
    patch = repo.parent / "change.patch"
    patch.write_text(git(repo, "diff", "--cached", "--binary", base).stdout)
    git(repo, "reset", "-q", "--hard", base)
    git(repo, "clean", "-qfd")
    return runner_config_changes(repo, patch, base, tuple(sorted(edits)))


@pytest.mark.parametrize(
    "path",
    [
        "node_modules/a/package.json",
        "web/node_modules/@scope/b/package.json",
        "vendor/c/setup.cfg",
        ".venv/lib/python3.12/site-packages/d/pyproject.toml",
        "lib/site-packages/e/tox.ini",
    ],
)
def test_f3_vendored_manifest_is_not_tampering(repo: Path, path: str) -> None:
    section = "tool:pytest" if path.endswith("setup.cfg") else "pytest"
    body = json.dumps(_PKG) if path.endswith(".json") else f"[{section}]\naddopts = -q\n"
    assert _config_changes(repo, {path: body}) == ()


def test_f3_project_manifest_test_script_still_tampered(repo: Path) -> None:
    edited = json.dumps({"scripts": {"test": "true"}})
    assert _config_changes(repo, {"web/package.json": edited}) == ("web/package.json",)


def test_f3_vendored_runner_config_still_tampered(repo: Path) -> None:
    """Only manifests are exempt: a conftest a runner may collect is not."""
    assert _config_changes(repo, {"vendor/conftest.py": "x = 1\n"}) == ("vendor/conftest.py",)


def test_f3_vendored_files_block_auto_landing() -> None:
    files = ("node_modules/a/package.json", "node_modules/a/index.js", "src/app.js")
    ctx = AutoApprovalContext(
        verification_outcome="passed",
        reverify_confirmed=True,
        changed_files=files,
        verify_pinned=True,
        reverify_commands=("npm test",),
        pin_control_files=("node_modules/a/package.json",),
    )
    blocked = rule_block_reason(VERIFIED_PATCH_RULE, ctx)
    assert blocked is not None and "vendors 2 dependency files" in blocked, blocked
    assert "tamper" not in blocked


def test_f3_no_vendored_files_no_vendored_reason() -> None:
    ctx = AutoApprovalContext(
        verification_outcome="passed",
        reverify_confirmed=True,
        changed_files=("src/vendors.js",),
        verify_pinned=True,
        reverify_commands=("npm test",),
    )
    blocked = rule_block_reason(VERIFIED_PATCH_RULE, ctx)
    assert blocked is None or "vendors" not in blocked, blocked


# --- F4 (F-I1): a syntax-only or version-only pin never auto-lands ----------

from fastapi import HTTPException  # noqa: E402

from skep.supervisor import SupervisorConfig  # noqa: E402
from skep.supervisor.policy import (  # noqa: E402
    known_vacuous_verify,
    syntax_only_verify,
)
from skep.supervisor.serve.registry import (  # noqa: E402
    _gate_auto_apply,
    unlocked_block_reason,
)

SYNTAX_ONLY = [
    "node --check f.js",
    "node -c 1-node-farm/final/index.js",
    "nodejs --check f.js",
    "python -m py_compile app.py",
    "python3 -m compileall src",
    "uv run python -m py_compile app.py",
    "bash -n deploy.sh",
    "sh -n x.sh",
    "ruby -c app.rb",
    "perl -c app.pl",
    "php -l index.php",
    "node --version",
    "uv --help",
    "node --check a.js && node --check b.js",
    "npm test; node --check f.js",  # the exit status is the last list's
    "sh -c 'node --check f.js'",
    "env CI=1 node --check f.js",
]
REAL_CHECKS = [
    "uv run pytest",
    "npm test",
    "tsc --noEmit",
    "npx tsc --noEmit",
    "npx eslint .",
    "ruff check .",
    "cargo test",
    "go test ./...",
    "node test.js",
    "node --test",
    "node --check f.js && npm test",
    "node --check f.js; npm test",
    "python -m pytest",
    "bash -n x.sh && bash run-tests.sh",
]


@pytest.mark.parametrize("command", SYNTAX_ONLY)
def test_f4_syntax_only_pin_is_named(command: str) -> None:
    reason = syntax_only_verify(command)
    assert reason is not None and "syntax-only" in reason, (command, reason)
    assert "G10" not in reason


@pytest.mark.parametrize("command", REAL_CHECKS)
def test_f4_real_checks_are_not_syntax_only(command: str) -> None:
    assert syntax_only_verify(command) is None, command
    assert known_vacuous_verify(command) is None, command


def test_f4_a_vacuous_pin_is_not_called_syntax_only() -> None:
    """Each pin gets one name: ``true`` stays known-vacuous."""
    assert syntax_only_verify("true") is None
    assert syntax_only_verify("git diff --check") is None


def test_f4_gate_refuses_auto_landing_on_a_syntax_only_pin() -> None:
    policy: dict[str, object] = {
        "auto_apply_verified_patch": True,
        "verify_command": "node --check f.js",
    }
    with pytest.raises(HTTPException) as refused:
        _gate_auto_apply(dict(policy), requested=True, notes=[])
    assert refused.value.status_code == 400 and "syntax-only" in str(refused.value.detail)
    notes: list[str] = []
    _gate_auto_apply(policy, requested=False, notes=notes)
    assert policy["auto_apply_verified_patch"] is False
    assert any("syntax-only" in note for note in notes), notes


def test_f4_gate_keeps_a_real_pin() -> None:
    policy: dict[str, object] = {"auto_apply_verified_patch": True, "verify_command": "npm test"}
    _gate_auto_apply(policy, requested=True, notes=[])
    assert policy["auto_apply_verified_patch"] is True


def _landing_ctx(*commands: str) -> AutoApprovalContext:
    return AutoApprovalContext(
        verification_outcome="passed",
        reverify_confirmed=True,
        changed_files=("src/app.js",),
        verify_pinned=True,
        reverify_commands=commands,
    )


def test_f4_rule_blocks_a_landing_g10_passed_on_a_syntax_only_pin() -> None:
    blocked = rule_block_reason(VERIFIED_PATCH_RULE, _landing_ctx("node --check f.js"))
    assert blocked is not None and "syntax-only" in blocked, blocked
    assert "landable by hand" in blocked


@pytest.mark.parametrize(
    "commands", [("npm test",), ("uv run pytest",), ("node --check f.js", "npm test")]
)
def test_f4_rule_lands_on_a_real_check(commands: tuple[str, ...]) -> None:
    assert rule_block_reason(VERIFIED_PATCH_RULE, _landing_ctx(*commands)) is None


def test_f4_unlocked_needs_more_than_a_syntax_check(config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        reason = unlocked_block_reason(
            store, {"unlocked": True, "verify_command": "node --check f.js"}
        )
    finally:
        store.close()
    assert reason is not None and "syntax-only" in reason, reason


def test_f4_setup_names_a_syntax_only_pin(repo: Path, config: SupervisorConfig) -> None:
    from .conftest import serve_client

    response = serve_client(config).post(
        "/api/projects/setup",
        json={
            "project_id": "syn", "name": "syn", "strategy": "trusted_local_dev",
            "repo_path": str(repo), "phase": "build",
            "policy_overrides": {"verify_command": "node --check index.js"},
        },
    )  # fmt: skip
    assert response.status_code == 201, response.text
    assert any("syntax-only" in note for note in response.json()["notes"]), response.json()
