"""Phase 10 10.2: the rich terminal face — and the script contract under it.

``cli_ui`` renders tables, cards, diffs and panels only on a terminal with
NO_COLOR unset. A pipe, a file, CI, NO_COLOR, ``--json`` and ``--md`` get the
plain bytes they always got (no ANSI, no prompt), so every output-pinning test
and ``| jq`` keeps working. ``skep review`` with no id walks what waits on the
operator; each verdict is an explicit key through the existing ``skep review
--approve/--deny`` verb, and EOF skips (I7).
"""

from __future__ import annotations

import ast
import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from skep import cli_ui
from skep.supervisor import RunStore, cli_cmds
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.policy_v3 import log_decision

from .conftest import git
from .test_cli import _only_task_id, _run_cli, _worker_cmd

ANSI = "\x1b["


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_importing_the_cli_loads_neither_httpx_nor_rich() -> None:
    """Every ``skep`` start pays for what ``skep.cli`` imports: httpx came in
    through provider_hosts -> serve/llm.py for one setting-key constant, and
    httpx loads its own CLI (rich) when rich is installed — ~30 ms on every
    command. Both now load only when a command uses them."""
    code = "import sys, skep.cli; print(sorted({'httpx', 'rich'} & set(sys.modules)))"
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout
    assert out.strip() == "[]"


def test_rich_only_on_a_terminal_without_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert cli_ui.rich_enabled(_Tty()) is True
    assert cli_ui.rich_enabled(io.StringIO()) is False  # a pipe / a file / CI
    monkeypatch.setenv("NO_COLOR", "1")
    assert cli_ui.rich_enabled(_Tty()) is False


def test_no_skep_module_imports_rich_at_import_time() -> None:
    """`skep --help` must not pay for rich: every skep import of it sits in a
    function body (or under TYPE_CHECKING). httpx loads rich on its own when
    it is installed (its optional CLI, ``httpx._main``) — that cost rides
    skep's eager httpx import and is measured, not hidden, in the PR."""
    root = Path(cli_ui.__file__).parent
    eager = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        body = list(tree.body)
        while body:
            node = body.pop()
            if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test):
                continue
            if isinstance(node, (ast.If, ast.Try, ast.With)):
                body.extend(ast.iter_child_nodes(node))  # type: ignore[arg-type]
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            if any(name == "rich" or name.startswith("rich.") for name in names):
                eager.append(f"{path.relative_to(root)}:{node.lineno}")
    assert eager == []


def _on_a_terminal(monkeypatch: pytest.MonkeyPatch, *, color: bool = False) -> None:
    """The rich path, captured: rich_enabled says yes; the console is forced to
    a terminal only when a test asserts the escapes themselves."""
    from rich.console import Console

    monkeypatch.setattr(cli_ui, "rich_enabled", lambda stream=None: True)
    if color:
        monkeypatch.setattr(
            cli_ui,
            "console",
            lambda: Console(
                file=sys.stdout, force_terminal=True, color_system="standard", highlight=False
            ),
        )


def _decisions(home: Path) -> None:
    audit = build_config(home, None).audit_dir
    for verdict, value in (("allow", "ls"), ("require_approval", "curl x"), ("deny", "rm -rf /")):
        log_decision(
            audit,
            {
                "at": "2026-11-24T10:00:00Z",
                "actor": "worker:t1",
                "scope": "shell",
                "action": "run",
                "value": value,
                "verdict": verdict,
                "task_id": "t1",
            },
        )


def test_policy_decisions_piped_is_the_same_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    _decisions(home)
    assert _run_cli(home, "policy", "decisions") == 0
    out = capsys.readouterr().out
    assert ANSI not in out
    view = json.loads(out)
    assert out == json.dumps(view, indent=2, sort_keys=True) + "\n"
    assert [row["verdict"] for row in view["rows"]] == ["deny", "require_approval", "allow"]


def test_policy_decisions_on_a_terminal_is_a_badged_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _decisions(home)
    _on_a_terminal(monkeypatch, color=True)
    assert _run_cli(home, "policy", "decisions") == 0
    out = capsys.readouterr().out
    assert ANSI in out
    for word in ("ALLOW", "ASK", "DENY", "rm -rf /", "shell.run", "total 3"):
        assert word in out
    # --json on a terminal is still the script contract.
    assert _run_cli(home, "policy", "decisions", "--json") == 0
    out = capsys.readouterr().out
    assert ANSI not in out and json.loads(out)["total"] == 3


def test_badges_cover_the_four_verdicts() -> None:
    labels = {cli_ui.badge(v).plain for v in ("allow", "require_approval", "deny", "tampered")}
    assert labels == {"ALLOW", "ASK", "DENY", "TAMPER"}
    assert cli_ui.badge("observed").plain == "OBSERVED"


def test_brackets_print_literally_and_never_raise(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 10 R1 D5: values are data, never rich markup — `[id]`, `[/tmp]`
    and `[b]` print as typed, and a stray closing tag cannot crash a table."""
    _on_a_terminal(monkeypatch)
    cli_ui.print_table(("id", "value"), [("[id]", "[/tmp] [b]x")], title="[t]")
    cli_ui.print_panel("[/tmp] in [b]a panel", title="[review] card", style="green")
    out = capsys.readouterr().out
    for literal in ("[id]", "[/tmp] [b]x", "[t]", "[/tmp] in [b]a panel", "[review] card"):
        assert literal in out, literal
    _on_a_terminal(monkeypatch, color=True)  # a console built with markup on
    cli_ui.print_table(("id",), [("[/tmp]",)])
    assert "[/tmp]" in capsys.readouterr().out


def test_decisions_badge_the_verdict_and_note_rows_their_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """D5: a decision row's badge is its verdict — a row that also carries a
    source (a run's dispatch row says source="run") is still DENY; only the
    note rows (observed / forwarded / delegated / alarm) badge their source,
    as actions.py and policies.js read them."""
    home = tmp_path / "home"
    audit = build_config(home, None).audit_dir
    base = {"at": "2026-11-24T10:00:00Z", "actor": "worker:t1", "scope": "shell",
            "action": "run", "task_id": "t1"}  # fmt: skip
    log_decision(audit, {**base, "value": "[/tmp]/rm", "verdict": "deny", "source": "run"})
    log_decision(audit, {**base, "value": "ls", "verdict": "allow", "source": "observed"})
    _on_a_terminal(monkeypatch)
    assert _run_cli(home, "policy", "decisions") == 0
    out = capsys.readouterr().out
    assert "DENY" in out and "OBSERVED" in out and "RUN" not in out
    assert "[/tmp]/rm" in out


def test_skep_chat_help_keeps_its_brackets_on_a_terminal(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep import cli_chat

    _on_a_terminal(monkeypatch)
    cli_chat._print_help()
    out = capsys.readouterr().out
    assert "/runs [n]" in out and "/policy [repo]" in out


def test_doctor_json_stays_plain_and_the_panel_carries_the_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _run_cli(home, "doctor", "--offline", "--json")
    out = capsys.readouterr().out
    assert ANSI not in out and json.loads(out)["overall"]
    _run_cli(home, "doctor", "--offline")
    plain = capsys.readouterr().out
    assert plain.startswith("Skep: ") and ANSI not in plain
    _on_a_terminal(monkeypatch)
    _run_cli(home, "doctor", "--offline")
    panel = capsys.readouterr().out
    assert "skep doctor" in panel and plain.splitlines()[0] in panel
    assert panel != plain


def _completed_run(repo: Path, home: Path, capsys: pytest.CaptureFixture[str]) -> str:
    code = _run_cli(
        home,
        "run",
        str(repo),
        "Fix the bug. MODE:happy",
        "--execution-mode",
        "workspace",
        "--worker-cmd",
        _worker_cmd(),
        "--quiet",
    )
    assert code == 0
    capsys.readouterr()
    return _only_task_id(home)


def test_forensics_is_the_run_forensics_shortcut(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    task_id = _completed_run(repo, home, capsys)
    for flags in ([], ["--md"], ["--json"]):
        assert _run_cli(home, "forensics", task_id, *flags) == 0
        short = capsys.readouterr().out
        assert _run_cli(home, "run", "forensics", task_id, *flags) == 0
        assert capsys.readouterr().out == short and ANSI not in short
    _on_a_terminal(monkeypatch, color=True)
    assert _run_cli(home, "forensics", task_id) == 0
    assert ANSI in capsys.readouterr().out
    assert _run_cli(home, "forensics", task_id, "--md") == 0  # --md stays paste-ready
    assert ANSI not in capsys.readouterr().out


def _no_prompt() -> str:
    raise AssertionError("prompted without a terminal")


def test_review_with_no_id_off_a_terminal_lists_and_never_prompts(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    assert _run_cli(home, "review") == 2  # no store yet: the same teaching error
    capsys.readouterr()
    task_id = _completed_run(repo, home, capsys)
    monkeypatch.setattr(cli_cmds, "_read_approval_choice", _no_prompt)
    assert _run_cli(home, "review") == 0
    out = capsys.readouterr().out
    assert task_id in out and "CONFIRMED" in out and "next: skep review <task_id>" in out
    assert ANSI not in out
    assert _run_cli(home, "review", "--approve") == 2  # a verdict needs a named run
    assert "needs a task id" in capsys.readouterr().err


def _interactive(monkeypatch: pytest.MonkeyPatch, keys: list[str]) -> list[str]:
    pressed = iter(keys)
    monkeypatch.setattr(cli_cmds, "_review_interactive", lambda: True)
    monkeypatch.setattr(cli_cmds, "_read_approval_choice", lambda: next(pressed, "s"))
    return keys


def test_interactive_review_shows_forensics_receipt_then_lands(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    task_id = _completed_run(repo, home, capsys)
    _on_a_terminal(monkeypatch)
    _interactive(monkeypatch, ["f", "p", "a"])
    assert _run_cli(home, "review") == 0
    out = capsys.readouterr().out
    assert f"CONFIRMED · task {task_id}" in out
    assert "+value = 1" in out  # the exact diff
    assert "[a] approve & land" in out and "[q] quit" in out
    assert f"run {task_id}" in out  # forensics
    assert "receipt" in out.lower()
    assert f"branch skep/{task_id}" in out  # landed through `skep review --approve`
    assert git(repo, "show", f"skep/{task_id}:existing.py").stdout == "value = 1\n"
    assert _run_cli(home, "review") == 0  # landed: nothing waits now
    assert "nothing waits on you" in capsys.readouterr().out


@pytest.mark.parametrize(("keys", "status"), [(["d"], "denied"), ([], None), (["q"], None)])
def test_interactive_review_deny_skip_and_quit(
    repo: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    keys: list[str],
    status: str | None,
) -> None:
    """EOF (no key) and q decide nothing; d denies through the existing verb."""
    home = tmp_path / "home"
    task_id = _completed_run(repo, home, capsys)
    _interactive(monkeypatch, keys)
    assert _run_cli(home, "review") == 0
    out = capsys.readouterr().out
    assert ANSI not in out  # the plain card off rich (NO_COLOR or a dumb face)
    store = RunStore(build_config(home, None).db_path)
    try:
        approvals: list[Any] = store.approvals_for(task_id)
    finally:
        store.close()
    assert [a.status for a in approvals] == ([status] if status else [])
    if not keys:
        assert f"skipped: task {task_id} still waits" in out


def _press(monkeypatch: pytest.MonkeyPatch, key: str, meanwhile: Any) -> None:
    """One key, after something changed on another surface while the card
    was on screen (D4: the verdict binds to what was shown)."""
    monkeypatch.setattr(cli_cmds, "_review_interactive", lambda: True)

    def read() -> str:
        meanwhile()
        return key

    monkeypatch.setattr(cli_cmds, "_read_approval_choice", read)


def _patch_of(home: Path, task_id: str) -> Path:
    store = RunStore(build_config(home, None).db_path, readonly=True)
    try:
        return next(Path(p) for kind, p, _ in store.artifacts_for(task_id) if kind == "patch")
    finally:
        store.close()


def test_interactive_review_refuses_a_verdict_on_a_patch_that_changed(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 10 R1 D4 (I7, I13): `a` lands the patch that was SHOWN — a patch
    rewritten while the card was on screen is refused, and nothing lands."""
    home = tmp_path / "home"
    task_id = _completed_run(repo, home, capsys)
    patch = _patch_of(home, task_id)
    _press(monkeypatch, "a", lambda: patch.write_text(patch.read_text() + "+sneaked = 1\n"))
    assert _run_cli(home, "review") == 2
    err = capsys.readouterr().err
    assert "the patch changed since you read it" in err and "nothing done" in err
    assert git(repo, "branch", "--list", f"skep/{task_id}").stdout == ""
    store = RunStore(build_config(home, None).db_path)
    try:
        assert store.approvals_for(task_id) == []
    finally:
        store.close()


def test_interactive_review_refuses_when_another_review_opened(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """D4: the card showed no open review; one opened elsewhere before the key
    — the verdict would have gone to an approval nobody here read."""
    home = tmp_path / "home"
    task_id = _completed_run(repo, home, capsys)
    db = build_config(home, None).db_path

    def open_review() -> None:
        store = RunStore(db)
        try:
            store.enqueue_approval(task_id, action="apply_patch", reason="from the web")
        finally:
            store.close()

    _press(monkeypatch, "d", open_review)
    assert _run_cli(home, "review") == 2
    assert "is waiting on this run since you read it" in capsys.readouterr().err
    store = RunStore(db)
    try:
        assert [a.status for a in store.approvals_for(task_id)] == ["pending"]
    finally:
        store.close()


def test_interactive_review_refuses_a_gate_answered_elsewhere(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """D4: the shown gate was denied from the web while the card was up — the
    key gives no second verdict, on it or on anything else."""
    home = tmp_path / "home"
    _run_cli(home, "run", str(repo), "Fix the bug. MODE:pending", "--execution-mode",
             "workspace", "--worker-cmd", _worker_cmd(), "--quiet")  # fmt: skip
    capsys.readouterr()
    task_id = _only_task_id(home)
    db = build_config(home, None).db_path
    store = RunStore(db)
    try:
        (gate,) = [a for a in store.approvals_for(task_id) if a.status == "pending"]
    finally:
        store.close()

    def denied_on_the_web() -> None:
        store = RunStore(db)
        try:
            store.resolve_approval(gate.review_id, approved=False, actor="web", note="no")
        finally:
            store.close()

    _press(monkeypatch, "a", denied_on_the_web)
    assert _run_cli(home, "review") == 2
    err = capsys.readouterr().err
    assert f"review {gate.review_id} is no longer pending (denied" in err
    store = RunStore(db)
    try:
        assert [a.status for a in store.approvals_for(task_id)] == ["denied"]
        assert len(store.recent_runs(10)) == 1  # nothing resumed
    finally:
        store.close()


def _pty_stdin(monkeypatch: pytest.MonkeyPatch) -> int:
    """A real terminal on stdin; returns the master end to type into. Echo is
    off: nothing reads the master here, and an echoed key would sit in the
    output queue the mode switch waits to drain."""
    import pty
    import termios

    master, slave = pty.openpty()
    attrs = termios.tcgetattr(slave)
    attrs[3] &= ~termios.ECHO
    termios.tcsetattr(slave, termios.TCSANOW, attrs)
    stdin = open(slave, encoding="utf-8")  # noqa: SIM115 - closed by the monkeypatch's owner
    monkeypatch.setattr(sys, "stdin", stdin)
    return master


def test_a_single_key_never_answers_with_what_was_typed_before_the_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D4 (I7): pending input is flushed before the key is read, and no key
    within the timeout reads as nothing (the caller's skip)."""
    import os
    import threading

    master = _pty_stdin(monkeypatch)
    os.write(master, b"a")  # typed before the card was drawn
    assert cli_cmds._read_single_key(timeout=0.3) == ""
    threading.Timer(0.2, lambda: os.write(master, b"d")).start()
    assert cli_cmds._read_single_key(timeout=5) == "d"
    assert cli_cmds._read_single_key(timeout=0.1) == ""  # the timeout skips


def test_a_terminal_that_refuses_raw_mode_falls_back_to_a_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """D4: termios.error is an OSError to the caller, so it reads a line."""
    import os
    import termios

    def refuse(fd: int) -> Any:
        raise termios.error(25, "Inappropriate ioctl for device")

    master = _pty_stdin(monkeypatch)
    monkeypatch.setattr(termios, "tcgetattr", refuse)
    with pytest.raises(OSError):
        cli_cmds._read_single_key()
    os.write(master, b"d\n")
    assert cli_cmds._read_approval_choice() == "d"


def test_review_reads_a_patch_that_is_not_utf8_and_lights_it_on_a_terminal(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """D4: a patch with a non-UTF-8 byte never crashes a review; on a terminal
    `skep review <id>` shows the diff lit by rich, plain bytes otherwise."""
    home = tmp_path / "home"
    task_id = _completed_run(repo, home, capsys)
    patch = _patch_of(home, task_id)
    patch.write_bytes(patch.read_bytes() + b"+latin = '\xe9'\n")
    store = RunStore(build_config(home, None).db_path, readonly=True)
    try:
        (card,) = cli_cmds._review_cards(store)
    finally:
        store.close()
    assert "+latin = '\ufffd'" in (card.patch or "")
    assert _run_cli(home, "review", task_id) == 0
    plain = capsys.readouterr().out
    assert "+value = 1" in plain and ANSI not in plain
    _on_a_terminal(monkeypatch, color=True)
    assert _run_cli(home, "review", task_id) == 0
    lit = capsys.readouterr().out
    assert ANSI in lit and "value" in lit and f"task {task_id}" in lit


def test_review_cards_say_what_g10_ran_whose_command_and_the_counts(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Phase 10 R1 D3 (I2, I8): "re-ran … → exit" only when a command ran;
    a recorded verify nothing executed is said as that; the command's source
    and the test counts are on the card; nothing-to-re-verify never reads as
    NOT CONFIRMED."""
    home = tmp_path / "home"
    task_id = _completed_run(repo, home, capsys)
    db = build_config(home, None).db_path

    def card(**record: Any) -> tuple[str, str]:
        store = RunStore(db)
        try:
            store.record_reverification(task_id, worker_outcome="passed", **record)
            (found,) = cli_cmds._review_cards(store)
        finally:
            store.close()
        return found.label, "\n".join(found.lines)

    counts = {"total_executed": 4, "passed": 4, "failed": 0, "errors": 0}
    label, ran = card(
        outcome="passed",
        confirmed=True,
        commands=["uv run pytest"],
        exit_codes=[0],
        detail="ok [command from the project's pinned verify_command]",
        test_counts={
            "status": "ok",
            "runner": "pytest",
            "baseline": counts,
            "patched": {**counts, "total_executed": 5, "passed": 5},
        },
    )
    assert label == "CONFIRMED"
    assert "G10:      passed confirmed=True" in ran
    assert "re-ran ['uv run pytest'] → exit [0]" in ran
    assert "command from the project pin" in ran
    assert "tests:    baseline 4 executed → patched 5 executed" in ran
    label, unrun = card(
        outcome="unavailable",
        confirmed=False,
        commands=["uv run pytest"],
        exit_codes=[],
        detail="the sandbox could not start [command from the worker's own verify step]",
    )
    assert label == "NOT CONFIRMED"
    assert "re-ran" not in unrun and "exit" not in unrun
    assert "recorded verify: ['uv run pytest'] — nothing executed" in unrun
    assert "command from the worker's own verify step (unpinned fallback)" in unrun
    label, benign = card(
        outcome="not_applicable",
        confirmed=False,
        commands=[],
        exit_codes=[],
        detail="no patch was produced — nothing to re-verify",
    )
    assert label == "NOTHING TO RE-VERIFY"
    assert "G10:      nothing to re-verify: no patch was produced" in benign
    assert "confirmed=False" not in benign and "command " not in benign


def test_review_finds_a_waiting_run_older_than_any_display_window(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Phase 10 R1 D6 (I13): the walk queries what waits — gated runs,
    unlanded patches, G10 disagreements — before any limit, so a patch that
    waited behind 250 newer runs is still on it."""
    import sqlite3
    from contextlib import closing

    home = tmp_path / "home"
    task_id = _completed_run(repo, home, capsys)
    db = build_config(home, None).db_path
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.executemany(
            "INSERT INTO runs (task_id, trace_id, repo, ref, workspace, execution_mode,"
            " instructions, state, created_at, updated_at)"
            " VALUES (?, ?, ?, NULL, '/w', 'workspace', 'later', 'failed', ?, ?)",
            [
                (f"later-{n:03d}", f"t-{n}", str(repo), f"2999-01-01T00:00:{n:03d}Z", "2999")
                for n in range(250)
            ],
        )
    assert _run_cli(home, "review") == 0
    assert task_id in capsys.readouterr().out


def test_review_cards_name_the_gate_the_disagreement_and_the_tamper(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3
    from contextlib import closing

    home = tmp_path / "home"
    for mode in ("happy", "liar", "pending", "crash"):
        _run_cli(home, "run", str(repo), f"Fix the bug. MODE:{mode}", "--execution-mode",
                 "workspace", "--worker-cmd", _worker_cmd(), "--quiet")  # fmt: skip
    db = build_config(home, None).db_path
    store = RunStore(db)
    try:
        by_mode = {r.instructions.split("MODE:")[1]: r.task_id for r in store.recent_runs(10)}
    finally:
        store.close()
    # The happy run's G10 rewritten the way V1 records a tamper.
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(
            "UPDATE reverifications SET outcome = 'tampered', confirmed = 0,"
            " tamper_flags_json = '[\"deleted_test:test_x.py\"]' WHERE task_id = ?",
            (by_mode["happy"],),
        )
    capsys.readouterr()
    assert _run_cli(home, "review") == 0
    listed = capsys.readouterr().out
    assert f"{by_mode['happy']}  TAMPER" in listed
    assert f"{by_mode['liar']}  NOT CONFIRMED" in listed
    assert f"{by_mode['pending']}  ASK" in listed
    assert by_mode["crash"] not in listed  # failed: nothing to approve here
    _interactive(monkeypatch, [])  # EOF on every card: all skipped
    assert _run_cli(home, "review") == 0
    out = capsys.readouterr().out
    assert "[a] approve & resume" in out and "card:     review" in out
    assert "V1:       " in out and out.count("still waits") == 3


def _after_stale_check(monkeypatch: pytest.MonkeyPatch, meanwhile: Any) -> None:
    """Phase 10 R2 I1: the other surface acts AFTER the stale check passed —
    between the check and the verdict, where a re-lookup minted a fresh review."""
    check = cli_cmds._stale_verdict

    def then(*args: Any) -> str | None:
        found = check(*args)
        meanwhile()
        return found

    monkeypatch.setattr(cli_cmds, "_stale_verdict", then)


def test_interactive_approve_never_lands_a_review_denied_after_the_stale_check(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2 I1 (I7, I13): the card showed review X; the web denied X after the
    stale check — `a` claims exactly X, finds it answered, and lands nothing
    (no fresh review is minted and approved behind the web's deny)."""
    home = tmp_path / "home"
    task_id = _completed_run(repo, home, capsys)
    db = build_config(home, None).db_path
    store = RunStore(db)
    try:
        shown = store.enqueue_approval(task_id, action="apply_patch", reason="land it")
    finally:
        store.close()

    def denied_on_the_web() -> None:
        store = RunStore(db)
        try:
            store.resolve_approval(shown, approved=False, actor="web", note="no")
        finally:
            store.close()

    _interactive(monkeypatch, ["a"])
    _after_stale_check(monkeypatch, denied_on_the_web)
    assert _run_cli(home, "review") == 2
    err = capsys.readouterr().err
    assert f"review {shown} is no longer pending (denied by web)" in err
    assert "nothing done" in err
    assert git(repo, "branch", "--list", f"skep/{task_id}").stdout == ""
    store = RunStore(db)
    try:
        assert [(a.review_id, a.status) for a in store.approvals_for(task_id)] == [
            (shown, "denied")
        ]
    finally:
        store.close()


def test_interactive_approve_refuses_when_a_review_came_and_went_since_the_card(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2 I1: the card showed no review; after the stale check the web minted
    one and denied it — the CLI mints nothing to approve in its place."""
    home = tmp_path / "home"
    task_id = _completed_run(repo, home, capsys)
    db = build_config(home, None).db_path

    def minted_and_denied() -> None:
        store = RunStore(db)
        try:
            review = store.enqueue_approval(task_id, action="apply_patch", reason="web")
            store.resolve_approval(review, approved=False, actor="web", note="no")
        finally:
            store.close()

    _interactive(monkeypatch, ["a"])
    _after_stale_check(monkeypatch, minted_and_denied)
    assert _run_cli(home, "review") == 2
    err = capsys.readouterr().err
    assert "an approval was recorded on this run since you read it; nothing done" in err
    assert git(repo, "branch", "--list", f"skep/{task_id}").stdout == ""
    store = RunStore(db)
    try:
        assert [a.status for a in store.approvals_for(task_id)] == ["denied"]
    finally:
        store.close()


def test_interactive_approve_rebinds_the_patch_after_claiming_the_review(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2 I1: the patch's bytes are checked again once the review is claimed —
    a rewrite that slipped in before the claim is refused and the claim is
    handed back, pending."""
    home = tmp_path / "home"
    task_id = _completed_run(repo, home, capsys)
    patch = _patch_of(home, task_id)
    claim = RunStore.claim_approval

    def claim_then_rewrite(self: RunStore, review_id: str, **kw: Any) -> bool:
        won = claim(self, review_id, **kw)
        patch.write_text(patch.read_text() + "+sneaked = 1\n")
        return won

    monkeypatch.setattr(RunStore, "claim_approval", claim_then_rewrite)
    _interactive(monkeypatch, ["a"])
    assert _run_cli(home, "review") == 2
    err = capsys.readouterr().err
    assert "the patch changed since you read it" in err and "nothing done" in err
    assert git(repo, "branch", "--list", f"skep/{task_id}").stdout == ""
    store = RunStore(build_config(home, None).db_path)
    try:
        assert [a.status for a in store.approvals_for(task_id)] == ["pending"]
    finally:
        store.close()


def test_interactive_resume_claims_the_shown_gate_only(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2 I1: an ASK card's gate denied after the stale check resumes nothing."""
    home = tmp_path / "home"
    _run_cli(home, "run", str(repo), "Fix the bug. MODE:pending", "--execution-mode",
             "workspace", "--worker-cmd", _worker_cmd(), "--quiet")  # fmt: skip
    capsys.readouterr()
    task_id = _only_task_id(home)
    db = build_config(home, None).db_path
    store = RunStore(db)
    try:
        (gate,) = [a for a in store.approvals_for(task_id) if a.status == "pending"]
    finally:
        store.close()

    def denied_on_the_web() -> None:
        store = RunStore(db)
        try:
            store.resolve_approval(gate.review_id, approved=False, actor="web", note="no")
        finally:
            store.close()

    _interactive(monkeypatch, ["a"])
    _after_stale_check(monkeypatch, denied_on_the_web)
    assert _run_cli(home, "review") == 2
    assert f"review {gate.review_id} is no longer pending (denied by web)" in (
        capsys.readouterr().err
    )
    store = RunStore(db)
    try:
        assert [a.status for a in store.approvals_for(task_id)] == ["denied"]
        assert len(store.recent_runs(10)) == 1  # nothing resumed
    finally:
        store.close()
