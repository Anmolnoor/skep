"""D13 (operator decision 2026-11-13): a standing ("always", global) shell
allow is an allow-list — accepted only when every segment's program is a
known-safe one — with the old refusals kept as a second check. Grants a home
already holds keep working; doctor and `policy migrate --dry-run` name them."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from skep.cli import main
from skep.supervisor import RunStore
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.policy_schema import (
    POLICY_DOCUMENT_SETTINGS_KEY,
    LearnedRule,
    PolicyDocument,
    document_from_settings,
)
from skep.supervisor.serve.actions import (
    keep_options,
    queen_shell_allow_refusal,
    set_operator_policy_rule,
    standing_keep_refusal,
)

SAFE = [
    "ls -la", "cat README.md", "head -n 5 x", "tail x", "wc -l x", "grep -n foo x", "rg foo",
    "stat x", "file x", "du -sh .", "df -h", "pwd", "which git", "date", "uname -a",
    "find . -name '*.py'", "git status", "git log --oneline", "git diff HEAD", "git show HEAD",
    "git rev-parse HEAD", "git ls-files", "git blame x", "git branch", "git branch --list",
    "pytest -q", "uv run pytest -q", "npm test", "cargo test", "go test ./...", "ls && pwd",
    "cat < x", "rg --pre-glob '*.gz' foo",
]  # fmt: skip
UNSAFE = [
    "chmod 777 x", "git clean -fdx", "env", "printenv", "make", "touch x", "mkdir y",
    "git branch -D x", "git branch new", "git stash", "git diff --output=/tmp/x",
    "git diff --ext-diff", "ls && touch x", "echo hi", "find . -delete", "npm run build",
    "python3 -m pytest",  # listed by D13 but refused before it: nothing may widen
    # ES-F82 review: a listed program that writes a file or runs another one
    "cat a > b", "grep -r x . >> out", "ls 2>err", "find . -fprint out", "find . -fls out",
    "find . -fprintf out %p", "rg --pre ./x foo", "rg --pre=./x foo",
]  # fmt: skip


@pytest.mark.parametrize("line", SAFE)
def test_a_known_safe_line_is_a_standing_allow(line: str) -> None:
    found = queen_shell_allow_refusal(line, "run")
    assert isinstance(found, list), (line, found)


@pytest.mark.parametrize("line", UNSAFE)
def test_anything_else_is_not_and_the_refusal_says_once_or_session(line: str) -> None:
    found = queen_shell_allow_refusal(line, "run")
    assert isinstance(found, str), (line, found)


def test_a_listed_program_with_a_refused_shape_still_fails() -> None:
    assert isinstance(queen_shell_allow_refusal("cat ~/.ssh/id_rsa", "run"), str)
    assert isinstance(queen_shell_allow_refusal("find . -exec rm {} ;", "run"), str)
    assert isinstance(queen_shell_allow_refusal("git -c core.pager=sh log", "run"), str)


def test_set_operator_policy_and_a_kept_card_share_the_list(tmp_path: Path) -> None:
    config = build_config(tmp_path / "h", None)
    config.home.mkdir(parents=True)
    store = RunStore(config.db_path)
    try:
        with pytest.raises(HTTPException) as refused:
            set_operator_policy_rule(
                store, scope="shell", action="run", pattern="chmod 777 x", verdict="allow"
            )
        assert "this session" in str(refused.value.detail)
        assert set_operator_policy_rule(
            store, scope="shell", action="run", pattern="git status", verdict="allow"
        )
    finally:
        store.close()
    for tool in ("run_shell", "start_process"):
        args = {"command": "chmod 777 x"}
        why = standing_keep_refusal(tool, args)
        assert why is not None and "this session" in why, (tool, why)
        assert keep_options(tool, args) == ["once", "session"], tool
        assert "always" in keep_options(tool, {"command": "git status"}), tool


def test_held_standing_grants_keep_working_and_doctor_and_migrate_name_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "h"
    config = build_config(home, None)
    config.home.mkdir(parents=True)
    store = RunStore(config.db_path)
    try:
        document = document_from_settings(store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY))
        document = document or PolicyDocument()
        held = [
            LearnedRule(rule_id=f"shell:run:{p}", scope="shell", action="run", pattern=p,
                        provenance="allow-always:op", lifetime="global", matcher="exact_argv")
            for p in ("chmod 777 x", "git status")
        ]  # fmt: skip
        document = document.model_copy(update={"learned": [*document.learned, *held]})
        store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, document.model_dump_json())
    finally:
        store.close()
    from skep.status import build_status

    advisories = " ".join(build_status(home)["advisories"])
    assert "chmod 777 x" in advisories and "git status" not in advisories, advisories
    capsys.readouterr()
    assert main(["--home", str(home), "policy", "migrate", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "chmod 777 x" in out and "`git status`" not in out, out
    assert main(["--home", str(home), "policy", "decide", "shell", "run", "chmod 777 x"]) in (0, 1)
    decided = capsys.readouterr().out
    assert "grant:global" in decided, decided  # still held: nothing deleted or disabled


def test_a_run_shell_confirm_naming_always_on_an_unlisted_line_is_refused(tmp_path: Path) -> None:
    from typing import Any, cast

    from skep.supervisor.serve.chat import ChatEngine
    from skep.supervisor.serve.settings import ConfigHolder

    config = build_config(tmp_path / "h", None)
    config.home.mkdir(parents=True)
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        chat = store.create_chat(title="c", model=None, role="builder").chat_id
        marker = tmp_path / "ran"
        args = {"command": f"touch {marker}", "cwd": str(tmp_path)}
        action_id = store.add_chat_action(chat, tool="run_shell", args=args)
        engine = ChatEngine(store=store, holder=holder, runner=cast(Any, None), home=config.home)
        with pytest.raises(HTTPException) as refused:
            engine.verdict_events(
                chat, store.get_chat(chat), action_id, confirm=True, keep="always"
            )
        assert refused.value.status_code == 400 and "this session" in str(refused.value.detail)
        assert not marker.exists()
    finally:
        store.close()
