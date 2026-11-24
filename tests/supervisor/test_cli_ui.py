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
