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

    text = json.loads(
        (Path(policy_templates.__file__).parent / "descriptions.json").read_text()
    )["builder"]["shell-learnable"]
    assert "prefix" in text, text
