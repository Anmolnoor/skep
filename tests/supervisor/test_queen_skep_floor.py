"""D11 (operator decision 2026-11-13): the Queen's host shell never runs skep
itself and never names skep's own files — refused, no card, pointing at the
tool to use instead. The read_file lane is unchanged."""

from __future__ import annotations

from pathlib import Path

import pytest

from skep.supervisor import RunStore
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.serve.jobs import Dispatcher
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import mutation_execution_decision, queen_shell_decision


def _lane(tmp_path: Path) -> tuple[RunStore, ConfigHolder, Path]:
    config = build_config(tmp_path / "skephome", None)
    config.home.mkdir(parents=True)
    store = RunStore(config.db_path)
    return store, ConfigHolder(config, store), config.home


SKEP_SPELLINGS = [
    "skep status",
    "/opt/venv/bin/skep runs",
    "uv run skep doctor",
    "uvx skep doctor",
    "python3 -m skep status",
    "python3 -m skep --home /x policy why shell run ls",
    "env A=1 skep status",
    "timeout 5 skep status",
    "nice -n 5 skep status",
    "sh -c 'skep status'",
    "cd /x && skep status",
    "echo hi | skep chat c1",
    "SKEP",  # APFS runs SKEP as skep
]


@pytest.mark.parametrize("line", SKEP_SPELLINGS)
def test_the_queen_shell_never_runs_skep(tmp_path: Path, line: str) -> None:
    store, holder, _home = _lane(tmp_path)
    for background in (False, True):
        decision = queen_shell_decision(
            store, holder, command=line, cwd=str(tmp_path), background=background
        )
        assert decision is not None and decision.verdict == "deny", (line, decision)
        # `skep trust` / `skep chat` keep ES-F58's own words
        detail = str(decision.detail)
        assert ("get_run" in detail and "policy_why" in detail) or "/trust" in detail


@pytest.mark.parametrize(
    "template",
    [
        "sqlite3 {home}/supervisor.sqlite3 .tables",
        "cat {home}/audit/policy/decisions.jsonl",
        "cp {home}/supervisor.sqlite3 {tmp}/copy.db",
        "tail -n 5 {root}/serve.log",
        "ls {root}",
        "grep -r token {home}",
        "sh -c 'cat {home}/settings.json'",
        "python3 -c 'print(1)' {home}/x",
    ],
)
def test_the_queen_shell_never_names_skeps_own_files(tmp_path: Path, template: str) -> None:
    store, holder, home = _lane(tmp_path)
    line = template.format(home=home, root=home.parent, tmp=tmp_path)
    decision = queen_shell_decision(store, holder, command=line, cwd=str(tmp_path))
    assert decision is not None and decision.verdict == "deny", (line, decision)
    assert "read_file" in str(decision.detail)


def test_a_relative_name_inside_the_home_counts(tmp_path: Path) -> None:
    store, holder, home = _lane(tmp_path)
    (home / "settings.json").write_text("{}")
    lines = (("cat settings.json", home), ("cat supervisor/x", home.parent), ("ls .", home))
    for line, cwd in lines:
        decision = queen_shell_decision(store, holder, command=line, cwd=str(cwd))
        assert decision is not None and decision.verdict == "deny", (line, cwd, decision)


@pytest.mark.parametrize(
    "line", ["ls -la", "echo skep", "grep skep notes.txt", "git log --grep skep", "cat a/skep"]
)
def test_lines_that_do_not_run_skep_or_name_its_home_are_untouched(
    tmp_path: Path, line: str
) -> None:
    store, holder, _home = _lane(tmp_path)
    (tmp_path / "out").mkdir()
    decision = queen_shell_decision(store, holder, command=line, cwd=str(tmp_path / "out"))
    assert decision is None or decision.reason != "shell.deny.skep_self", (line, decision)


def test_registered_clones_under_the_skep_home_are_not_skeps_own_files(tmp_path: Path) -> None:
    """``<skep home>/repos`` holds the operator's clones (``/workon``), which
    the run_repo lane governs — not skep's own state."""
    store, holder, home = _lane(tmp_path)
    clone = home.parent / "repos" / "app"
    clone.mkdir(parents=True)
    decision = queen_shell_decision(store, holder, command=f"ls {clone}", cwd=str(tmp_path))
    assert decision is None or decision.reason != "shell.deny.skep_self", decision


@pytest.mark.parametrize("command", ["skep status", "cat {home}/audit/x.jsonl"])
def test_a_chat_cannot_schedule_skep_or_its_files(tmp_path: Path, command: str) -> None:
    from skep.supervisor.serve.tools import execute_mutation

    store, holder, home = _lane(tmp_path)
    args = {"name": "t", "caste": "script", "every": "1h",
            "instructions": command.format(home=home)}  # fmt: skip
    proposed = mutation_execution_decision("propose_schedule", args, store=store, holder=holder)
    assert proposed is not None and proposed.verdict == "deny", proposed
    with pytest.raises(ValueError, match="get_run"):
        execute_mutation(
            "propose_schedule", args, store=store, holder=holder,
            runner=Dispatcher(store=store, holder=holder),
            actor="chat-user", chat_id=None,
        )  # fmt: skip
    assert store.list_schedules() == []
