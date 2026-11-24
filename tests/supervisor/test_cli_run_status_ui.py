"""Phase 10 R1 (spec 10.2): the live phase spinner on ``skep run`` and the
rich runs table on ``skep status --personal`` — terminal only.

Off a terminal, under NO_COLOR and with --quiet the output is what it always
was, byte for byte: the spinner is never built, the table is the plain one.
"""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path
from typing import ClassVar

import pytest
from rich.console import Console

from skep import cli_ui
from skep.supervisor import RunStore, mint_task
from skep.supervisor.cli_cmds import _PhaseTail, build_config

from .test_cli import _run_cli, _worker_cmd
from .test_cli_ui import ANSI, _on_a_terminal


class _NoSpinner:
    def __init__(self, *args: object) -> None:
        raise AssertionError("the spinner was built off a terminal")


class _FakeSpinner:
    built: ClassVar[list[_FakeSpinner]] = []

    def __init__(self, text: str = "dispatching") -> None:
        self.phases: list[str] = [text]
        self.stopped = False
        _FakeSpinner.built.append(self)

    def phase(self, text: str) -> None:
        self.phases.append(text)

    def stop(self) -> None:
        self.stopped = True


def _happy_run(repo: Path, home: Path, *extra: str) -> int:
    return _run_cli(
        home,
        "run",
        str(repo),
        "Fix the bug. MODE:happy",
        "--execution-mode",
        "workspace",
        "--worker-cmd",
        _worker_cmd(),
        *extra,
    )


def _normalized(out: str) -> str:
    """A run's output with the parts that differ run to run blanked."""
    out = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27}\b|\btask-[\w-]+", "<task>", out)
    out = re.sub(r"\(\d+\.\d+s\)", "(<s>)", out)
    return re.sub(r"evidence:\s+\S+", "evidence: <dir>", out)


def test_run_off_a_terminal_never_builds_the_spinner_and_prints_what_it_did(
    repo: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_ui, "PhaseSpinner", _NoSpinner, raising=False)
    assert _happy_run(repo, tmp_path / "piped") == 0
    piped = capsys.readouterr().out
    assert ANSI not in piped
    lines = piped.splitlines()
    assert lines[0] == f"dispatching task against {repo}"
    result = next(i for i, line in enumerate(lines) if line.startswith("task "))
    assert all(line.startswith("  → ") for line in lines[1:result])  # the plain tail
    assert "  state:        completed (" in piped
    # NO_COLOR on a real terminal is the pipe's output, byte for byte.
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("NO_COLOR", "1")
    assert _happy_run(repo, tmp_path / "no-color") == 0
    no_color = capsys.readouterr().out
    assert _normalized(no_color) == _normalized(piped).replace(
        str(tmp_path / "piped"), str(tmp_path / "no-color")
    )
    # --quiet on a terminal: no tail, no spinner.
    monkeypatch.delenv("NO_COLOR")
    assert _happy_run(repo, tmp_path / "quiet", "--quiet") == 0
    quiet = capsys.readouterr().out
    assert ANSI not in quiet and "dispatching" not in quiet and "  → " not in quiet
    # --json belongs to forensics: still refused, still before anything runs.
    assert _happy_run(repo, tmp_path / "json", "--json") == 2


def test_the_plain_phase_tail_is_pinned(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = build_config(tmp_path / "home", None)
    events = config.worktrees_root / "task-1" / ".events"
    events.mkdir(parents=True)
    rows = [
        {"type": "heartbeat", "payload": {"phase": "planning"}},
        {"type": "command.start", "payload": {"command": "pytest -q"}},
        {"type": "verify.result", "payload": {"outcome": "passed"}},
        {"type": "task.terminal", "payload": {"status": "completed"}},
    ]
    (events / "task-1.ndjson").write_text("".join(json.dumps(r) + "\n" for r in rows))
    _PhaseTail(config)._drain()
    assert capsys.readouterr().out == (
        "  → … planning\n  → run: pytest -q\n  → verification: passed\n  → terminal: completed\n"
    )


def test_run_on_a_terminal_drives_the_spinner_from_the_phase_tail(
    repo: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeSpinner.built.clear()
    monkeypatch.setattr(cli_ui, "rich_enabled", lambda stream=None: True)
    monkeypatch.setattr(cli_ui, "PhaseSpinner", _FakeSpinner, raising=False)
    assert _happy_run(repo, tmp_path / "home") == 0
    (spinner,) = _FakeSpinner.built
    assert spinner.stopped  # never left spinning over the result block
    assert "state:        completed" in capsys.readouterr().out
    # The tail feeds the spinner, not stdout (deterministic: one drain).
    config = build_config(tmp_path / "seeded", None)
    events = config.worktrees_root / "task-1" / ".events"
    events.mkdir(parents=True)
    (events / "task-1.ndjson").write_text(
        json.dumps({"type": "verify.result", "payload": {"outcome": "passed"}}) + "\n"
    )
    fed = _FakeSpinner("dispatching")
    _PhaseTail(config, emit=fed.phase)._drain()
    assert fed.phases == ["dispatching", "verification: passed"]
    assert capsys.readouterr().out == ""


def test_the_spinner_prints_each_phase_verbatim_and_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    screen = io.StringIO()
    monkeypatch.setattr(
        cli_ui, "console", lambda: Console(file=screen, force_terminal=True, highlight=False)
    )
    spinner = cli_ui.PhaseSpinner("dispatching")
    spinner.phase("run: echo [bold]x[/bold]")  # a command is text, never markup
    spinner.stop()
    assert "  → run: echo [bold]x[/bold]" in screen.getvalue()


def _seed_runs(home: Path, repo: Path) -> tuple[str, str]:
    store = RunStore(build_config(home, None).db_path)
    try:
        done = mint_task(workspace=repo, instructions="x")
        store.create_run(done, repo=repo, ref=None, execution_mode="sandbox")
        store.transition(done.task_id, "completed", "fixed the bug")
        old = mint_task(workspace=repo, instructions="y")
        store.create_run(old, repo=repo, ref=None, execution_mode="sandbox")
        store.transition(old.task_id, "superseded", "resumed as succ-1")
    finally:
        store.close()
    return done.task_id, old.task_id


def test_status_personal_off_a_terminal_is_the_plain_table_byte_for_byte(
    repo: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    done, old = _seed_runs(tmp_path / "home", repo)
    assert _run_cli(tmp_path / "home", "status", "--personal") == 0
    piped = capsys.readouterr().out
    header = (
        f"{'task':<14} {'state':<17} {'project':<28} {'autonomy':<58} {'verify':<12} "
        f"{'re-verify':<14} {'usage':<9} summary"
    )
    row = f"{done[:12]:<14} {'completed':<17} {'-':<28} {'-':<58} {'-':<12} {'-':<14} {'-':<9} -"
    assert piped == (
        f"{header}\n{row}\n"
        "\n1 superseded run(s) (resumed as a successor; no action):\n"
        f"  {old[:12]}  superseded\n"
    )
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("NO_COLOR", "1")
    assert _run_cli(tmp_path / "home", "status", "--personal") == 0
    assert capsys.readouterr().out == piped


def test_status_personal_on_a_terminal_is_a_rich_table(
    repo: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    done, old = _seed_runs(tmp_path / "home", repo)
    _on_a_terminal(monkeypatch)
    monkeypatch.delenv("TERM", raising=False)  # R2 I12: rich pins a dumb TERM to 80 columns
    monkeypatch.setattr(  # a wide terminal: rich wraps a cell rather than cut it
        cli_ui,
        "console",
        # R2 I12: a colour system named, so TERM unset or dumb still draws ANSI
        lambda: Console(
            file=sys.stdout,
            force_terminal=True,
            color_system="standard",
            width=240,
            highlight=False,
        ),
    )
    assert _run_cli(tmp_path / "home", "status", "--personal") == 0
    out = capsys.readouterr().out
    assert ANSI in out and "┃" in out  # a drawn table, not the padded columns
    for word in ("task", "re-verify", done[:12], "completed"):
        assert word in out
    assert f"  {old[:12]}  superseded" in out  # the sections below stay as they are
