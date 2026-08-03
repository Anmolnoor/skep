"""v33: the Codex and Aider adapters (thin specs over the shared CLI adapter).

Driven against FAKE CLIs (tiny scripts that edit the workspace) — the real
binaries are not installed in CI, exactly like the Claude Code adapter test.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from skep.supervisor.contracts_io import DEFAULT_BUDGET, DEFAULT_PERMISSIONS, mint_task, read_result
from skep.worker_contract import CodingWorkerResult, Permissions, TaskState, VerificationOutcome
from skep.workers.aider import run_aider_task
from skep.workers.codex import run_codex_task


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _seed_repo(tmp_path: Path) -> Path:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "README.md").write_text("# target\n", encoding="utf-8")
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "test@example.com")
    _git(workspace, "config", "user.name", "Test")
    _git(workspace, "add", "README.md")
    _git(workspace, "commit", "-qm", "seed")
    return workspace


# (binary name, argv-guard the fake asserts, the file it writes, adapter runner)
_ADAPTERS = [
    (
        "codex",
        # codex sees: exec <instructions>
        "if sys.argv[1] != 'exec':\n    raise SystemExit(12)\n",
        "codex_made.py",
        run_codex_task,
        "codex-adapter-0.1.0",
    ),
    (
        "aider",
        # aider MUST be told not to auto-commit (patch-as-approval).
        "if '--no-auto-commit' not in sys.argv or '--message' not in sys.argv:\n"
        "    raise SystemExit(12)\n",
        "aider_made.py",
        run_aider_task,
        "aider-adapter-0.1.0",
    ),
]


@pytest.mark.parametrize(("binary", "guard", "made", "runner", "version"), _ADAPTERS)
def test_adapter_runs_its_cli_and_produces_a_verified_patch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    binary: str,
    guard: str,
    made: str,
    runner: object,
    version: str,
) -> None:
    workspace = _seed_repo(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / binary
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"{guard}"
        f"Path({made!r}).write_text('print(\"from {binary}\")\\n', encoding='utf-8')\n"
        "print('edited workspace')\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    task = mint_task(
        workspace=workspace,
        instructions=f"Create a file through {binary}.",
        permissions=DEFAULT_PERMISSIONS,
        budget=DEFAULT_BUDGET,
    )
    task_path = tmp_path / "task.json"
    task_path.write_text(task.model_dump_json(indent=2), encoding="utf-8")
    out_path = tmp_path / "result.json"

    assert runner(task_path, out_path) == 0  # type: ignore[operator]
    result = read_result(out_path)
    assert result.status is TaskState.COMPLETED
    assert result.verification.outcome is VerificationOutcome.PASSED
    assert result.changed_files == [made]
    patch = next(a for a in result.artifacts if a.kind == "patch")
    assert made in (workspace / patch.path).read_text(encoding="utf-8")
    # The adapter's own version is stamped on the TASK_START event.
    events = (workspace / ".events" / f"{result.task_id}.ndjson").read_text()
    assert version in events


def test_env_override_selects_a_custom_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _seed_repo(tmp_path)
    custom = tmp_path / "my-codex"
    custom.write_text(
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        "Path('via_env.py').write_text('print(1)\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    custom.chmod(0o755)
    monkeypatch.setenv("SKEP_CODEX_CMD", str(custom))

    task = mint_task(
        workspace=workspace,
        instructions="edit",
        permissions=Permissions(
            read=["workspace"],
            write=["workspace"],
            network=[],
            env_allowlist=["SKEP_CODEX_CMD"],
        ),
        budget=DEFAULT_BUDGET,
    )
    task_path = tmp_path / "task.json"
    task_path.write_text(task.model_dump_json(indent=2), encoding="utf-8")
    out_path = tmp_path / "result.json"
    assert run_codex_task(task_path, out_path) == 0
    assert read_result(out_path).changed_files == ["via_env.py"]


def test_adapter_rejects_a_non_coding_task(tmp_path: Path) -> None:
    workspace = _seed_repo(tmp_path)
    task = mint_task(
        workspace=workspace,
        instructions="audit",
        permissions=DEFAULT_PERMISSIONS,
        budget=DEFAULT_BUDGET,
        worker_kind="audit",
    )
    task_path = tmp_path / "task.json"
    task_path.write_text(task.model_dump_json(indent=2), encoding="utf-8")
    out_path = tmp_path / "result.json"
    assert run_codex_task(task_path, out_path) == 5  # EXIT_REJECTED
    assert read_result(out_path).status is TaskState.REJECTED


def _run_fake_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> CodingWorkerResult:
    """Drive the shared adapter against a fake CLI whose output we dictate."""
    workspace = _seed_repo(tmp_path)
    fake = tmp_path / "fake-codex"
    fake.write_text(f"#!/usr/bin/env python3\nimport sys\n{body}", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("SKEP_CODEX_CMD", str(fake))
    task = mint_task(
        workspace=workspace,
        instructions="do the thing",
        permissions=Permissions(
            read=["workspace"], write=["workspace"], network=[], env_allowlist=["SKEP_CODEX_CMD"]
        ),
        budget=DEFAULT_BUDGET,
    )
    task_path = tmp_path / "task.json"
    task_path.write_text(task.model_dump_json(indent=2), encoding="utf-8")
    out_path = tmp_path / "result.json"
    run_codex_task(task_path, out_path)
    return read_result(out_path)


def test_failure_details_carry_the_agents_own_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v100-F8: `agent exited 1` was true and useless. Two claude_code runs were
    blamed on the engine for two days while `API Error: Connection closed
    mid-response.` sat in the event log — the adapter captured it and then threw
    it away at the one place the operator reads (I9)."""
    result = _run_fake_codex(
        tmp_path,
        monkeypatch,
        "sys.stderr.write('boom: the agent could not start\\n')\nraise SystemExit(1)\n",
    )
    assert result.status is TaskState.FAILED
    details = result.verification.details
    assert details.startswith("agent exited 1: ")
    assert "boom: the agent could not start" in details


def test_failure_details_fall_back_to_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The field-test shape exactly: `claude --print` reports API errors on
    STDOUT and leaves stderr empty."""
    result = _run_fake_codex(
        tmp_path,
        monkeypatch,
        "print('API Error: Connection closed mid-response.')\nraise SystemExit(1)\n",
    )
    details = result.verification.details
    assert "API Error: Connection closed mid-response." in details


def test_no_patch_branch_carries_the_tail_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit 0 and no patch: the agent's last words are the only clue why it
    wrote nothing."""
    result = _run_fake_codex(
        tmp_path, monkeypatch, "print('I could not find the file you meant.')\n"
    )
    details = result.verification.details
    assert details.startswith("agent produced no workspace patch: ")
    assert "I could not find the file you meant." in details


def test_failure_details_stay_bounded_for_a_chatty_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tail, not the transcript — details is rendered in cards and CLI output.
    The full text stays in the COMMAND_RESULT event."""
    result = _run_fake_codex(
        tmp_path,
        monkeypatch,
        "sys.stderr.write('x' * 50_000 + 'THE LAST WORDS')\nraise SystemExit(1)\n",
    )
    details = result.verification.details
    assert "THE LAST WORDS" in details  # the *end*, which is where the error is
    assert len(details) < 300


def test_all_three_adapters_share_one_body() -> None:
    """v33: the shared cli_adapter is the single implementation; the specs only
    differ in the binary and argv."""
    from skep.workers.aider import AIDER_SPEC
    from skep.workers.claude_code.__main__ import CLAUDE_SPEC
    from skep.workers.codex import CODEX_SPEC

    specs = [CLAUDE_SPEC, CODEX_SPEC, AIDER_SPEC]
    assert {s.caste for s in specs} == {"coding"}
    # Each carries a distinct binary + env override.
    assert {s.default_command for s in specs} == {("claude",), ("codex",), ("aider",)}
    assert len({s.command_env for s in specs}) == 3
    # Aider's argv must never auto-commit (patch-as-approval).
    assert "--no-auto-commit" in AIDER_SPEC.build_argv(["aider"], "do it")


def test_agent_command_heartbeats_while_it_runs(tmp_path: Path) -> None:
    """v94-F2: the monitor kills a worker after 3x10s without event-log growth
    (field run 019f9e9f: SIGKILL at 30.3s mid-edit). A minutes-long agent call
    must emit heartbeats while it thinks, so silent-but-alive is
    distinguishable from hung."""
    import json

    from skep.workers.cli_adapter import _EventStream, _run

    stream = _EventStream(tmp_path / "events.ndjson", task_id="t", trace_id="tr")
    proc, _record = _run(
        ["/bin/sh", "-c", "sleep 0.5"],
        cwd=tmp_path,
        timeout=10,
        stream=stream,
        purpose="agent",
        heartbeat_seconds=0.1,
    )
    assert proc.returncode == 0
    lines = [json.loads(line) for line in (tmp_path / "events.ndjson").read_text().splitlines()]
    beats = [line for line in lines if line["type"] == "heartbeat"]
    assert len(beats) >= 2
    # Concurrent emits must not corrupt the log: seqs strictly increase.
    seqs = [line["seq"] for line in lines]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


def test_claude_argv_grants_headless_edit_permission() -> None:
    """--print cannot prompt, so anything short of bypassPermissions silently
    denies un-promptable tools. v94-F1's acceptEdits let edits flow but every
    Bash call was denied (authwapi acceptance, task 019fc711 — the agent
    could not run yarn at all). The sandbox is the wall, not Claude's
    prompts (ADR 0047); external engines are forced into sandbox execution
    (v94-F4)."""
    from skep.workers.claude_code.__main__ import CLAUDE_SPEC

    argv = CLAUDE_SPEC.build_argv(["claude"], "add multiply")
    assert argv == [
        "claude",
        "--permission-mode",
        "bypassPermissions",
        "--print",
        "add multiply",
    ]


def test_event_streams_survive_the_agent_deleting_the_events_dir(tmp_path: Path) -> None:
    """The event channel lives inside the agent-writable workspace; a tidy
    `git clean -fd` deletes it mid-run (authwapi acceptance, 019fc719 — the
    beat thread died on FileNotFoundError and the monitor reaped a healthy
    run 30s later). emit() must recreate the dir, in both stream impls."""
    import shutil

    from skep.worker_contract import EventType
    from skep.workers.cli_adapter import _EventStream
    from skep.workers.worker_runtime import EventStream

    for stream_cls in (EventStream, _EventStream):
        path = tmp_path / stream_cls.__name__ / ".events" / "t.ndjson"
        stream = stream_cls(path, task_id="t", trace_id="tr")
        stream.emit(EventType.HEARTBEAT, {"phase": "one"})
        shutil.rmtree(path.parent)
        stream.emit(EventType.HEARTBEAT, {"phase": "two"})  # must not raise
        assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_heartbeat_loop_outlives_a_failing_emit() -> None:
    """One transient emit failure must not kill the beat thread — a dead
    beat thread kills the whole run at the monitor's 3x window."""
    import time

    from skep.worker_contract import EventType
    from skep.workers.worker_runtime import Heartbeat

    class Flaky:
        def __init__(self) -> None:
            self.calls = 0

        def emit(self, event_type: EventType, payload: dict[str, object]) -> None:
            self.calls += 1
            if self.calls == 1:
                raise OSError("events dir vanished")

    flaky = Flaky()
    with Heartbeat(flaky, "x", interval_seconds=0.01, emit_immediately=False):
        deadline = time.monotonic() + 2.0
        while flaky.calls < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
    assert flaky.calls >= 3
