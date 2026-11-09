"""first-run L1: the agent-presence table — ps rows → agent, state, governed.

No real processes: ``ps`` output is fixture text, logs are temp files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skep.supervisor import RunStore, SupervisorConfig, mint_task
from skep.supervisor import agent_presence as presence
from skep.supervisor.store import ProcessRecord

ROOT = Path(__file__).resolve().parents[2]

BLOCKED_CLAUDE_TAIL = (
    "\x1b[1m Bash command \x1b[0m\n"
    "   rm -rf build/\n"
    " Do you want to proceed?\n"
    " > 1. Yes\n"
    "   2. No\n"
    " Esc to cancel\n"
)


def test_parse_ps_reads_rows_and_skips_junk() -> None:
    procs = presence.parse_ps(
        "  PID  PPID  %CPU ARGS\n"
        "  101     1   0.0 /bin/zsh -l\n"
        "  202   101  12,5 claude --resume\n"
        "garbage line\n"
    )
    assert set(procs) == {101, 202}
    assert procs[202] == presence.Proc(202, 101, 12.5, "claude --resume")


@pytest.mark.parametrize(
    ("args", "agent"),
    [
        ("claude", "claude"),
        ("/opt/homebrew/bin/codex exec fix it", "codex"),
        ("node /usr/local/lib/node_modules/.bin/gemini", "gemini"),
        ("cursor-agent --print", "cursor"),
        ("vim codex", None),  # argv[1] only counts behind an interpreter
        ("python -m skep.workers.pi --headless", None),
        ("/bin/zsh -l", None),
    ],
)
def test_identify_by_executable_name(args: str, agent: str | None) -> None:
    row = presence.identify(args)
    assert (row["id"] if row else None) == agent


def test_classify_blocked_needs_output_working_falls_back_to_cpu() -> None:
    claude = presence.identify("claude")
    assert claude is not None
    assert presence.classify(claude, BLOCKED_CLAUDE_TAIL, 0.0) == "blocked"
    assert presence.classify(claude, "✻ Thinking… (esc to interrupt)", 0.0) == "working"
    assert presence.classify(claude, None, 40.0) == "working"
    assert presence.classify(claude, None, 0.1) == "idle"


def test_every_table_row_is_well_formed_and_names_are_unique() -> None:
    names = [name for row in presence.AGENT_PATTERNS for name in row["names"]]
    assert len(names) == len(set(names))
    for row in presence.AGENT_PATTERNS:
        assert row["id"] and row["names"]
        for group in [*row["blocked"], *row["working"]]:
            assert group and all(s == s.lower() for s in group), row["id"]


def test_patterns_carry_herdr_provenance_and_notice() -> None:
    """Apache-2.0 §4: the source, the commit read and the license travel with
    the data, and skep's NOTICE names herdr."""
    header = presence.PATTERNS_FILE.read_text(encoding="utf-8").split("[[agents]]")[0]
    assert "https://github.com/herdrdev/herdr" in header
    assert "ee5bfb5041bb3e4a49d591c72308aae3c81ecdde" in header
    assert "Apache License 2.0" in header
    license_copy = presence.PATTERNS_FILE.with_name("LICENSE-herdr.txt").read_text()
    assert "Apache License" in license_copy and "Version 2.0, January 2004" in license_copy
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    assert "herdr" in notice and "Apache-2.0" in notice


def seed_fixture(config: SupervisorConfig, tmp_path: Path) -> str:
    """Three agents: claude under a skep worker (governed, role builder),
    claude under a start_process row with a permission prompt in its log
    (blocked, ungoverned — the gap), and a codex the operator ran by hand.
    Returns the ps text."""
    workspace = tmp_path / "worktrees" / "run1"
    workspace.mkdir(parents=True)
    task = mint_task(workspace=workspace, instructions="fix the flaky test please")
    log = tmp_path / "proc" / "p1.log"
    log.parent.mkdir()
    log.write_text("starting\n" + BLOCKED_CLAUDE_TAIL)
    store = RunStore(config.db_path)
    try:
        store.create_run(task, repo=tmp_path, ref=None, execution_mode="workspace")
        store.transition(task.task_id, "running")
        chat = store.create_chat(title="t", model=None, role="builder")
        action = store.add_chat_action(chat.chat_id, tool="dispatch_run", args={})
        store.resolve_chat_action(action, status="confirmed", result={"task_id": task.task_id})
        store.add_process(
            ProcessRecord(
                proc_id="p1",
                command="claude",
                cwd=None,
                pid=400,
                status="running",
                exit_code=None,
                log_path=str(log),
                started_at="2026-11-09T00:00:00Z",
                ended_at=None,
            )
        )
    finally:
        store.close()
    worker = (
        f"python -m skep.workers.claude_code --headless --task-file {workspace}/.events/task.json"
    )
    return (
        "    1     0   0.0 /sbin/launchd\n"
        "  300     1   0.5 /usr/bin/python3 -m skep serve\n"
        f"  310   300   0.0 sandbox-exec -f /x/sandbox.profile.sb {worker}\n"
        f"  311   310   0.2 {worker}\n"
        "  312   311  30.0 claude -p fix the flaky test\n"
        "  400     1   0.0 /bin/sh -c claude\n"
        "  401   400   0.0 claude\n"
        "  500     1   0.0 codex\n"
    )


def test_rows_say_which_agents_skep_governs(config: SupervisorConfig, tmp_path: Path) -> None:
    ps_text = seed_fixture(config, tmp_path)
    store = RunStore(config.db_path)
    try:
        rows = {r["pid"]: r for r in presence.agent_rows(store, config.audit_dir, ps_text)}
        task_id = store.runs_with_states(["running"])[0].task_id
    finally:
        store.close()

    assert set(rows) == {312, 401, 500}
    governed = rows[312]
    assert governed["agent"] == "claude" and governed["governed"]
    assert governed["task_id"] == task_id and governed["role"] == "builder"
    assert governed["state"] == "working"  # 30% CPU, no blocked prompt in its log
    gap = rows[401]
    assert gap["state"] == "blocked" and not gap["governed"] and gap["gap"]
    assert gap["started_by_process"] == "p1" and gap["output_read"]
    manual = rows[500]
    assert manual["agent"] == "codex" and manual["state"] == "idle"
    assert not manual["governed"] and not manual["output_read"] and not manual["gap"]


def test_presence_view_summarizes_the_gap(
    config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ps_text = seed_fixture(config, tmp_path)
    monkeypatch.setattr(presence, "read_ps", lambda: ps_text)
    store = RunStore(config.db_path)
    try:
        view = presence.presence_view(store, config.audit_dir)
    finally:
        store.close()
    assert view["summary"].startswith("3 agent(s) running: 1 governed by skep, 2 not")
    assert "1 blocked and ungoverned" in view["summary"]
