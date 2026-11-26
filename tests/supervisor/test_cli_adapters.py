"""v33: the Codex and Aider adapters (thin specs over the shared CLI adapter).

Driven against FAKE CLIs (tiny scripts that edit the workspace) — the real
binaries are not installed in CI, exactly like the Claude Code adapter test.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor.contracts_io import DEFAULT_BUDGET, DEFAULT_PERMISSIONS, mint_task, read_result
from skep.worker_contract import CodingWorkerResult, Permissions, TaskState, VerificationOutcome
from skep.workers.aider import run_aider_task
from skep.workers.claude_code import run_claude_code_task
from skep.workers.codex import run_codex_task
from skep.workers.pi import run_pi_task


@pytest.fixture(autouse=True)
def _inside_skeps_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """ES-F220: the codex worker refuses to start outside skep's sandbox; the
    spawner marks every sandboxed launch, so these direct drives say so too."""
    monkeypatch.setenv("SKEP_SANDBOX", "test")


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
    (
        "pi",
        # pi sees: -p <instructions> (its headless one-shot).
        "if sys.argv[1] != '-p':\n    raise SystemExit(12)\n",
        "pi_made.py",
        run_pi_task,
        "pi-adapter-0.1.0",
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, *, memory: Any = ()
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
        memory=list(memory),
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
    assert len(details) < 700  # v134-F16: ~600 chars of tail, up from 200


def test_all_cli_adapters_share_one_body() -> None:
    """v33: the shared cli_adapter is the single implementation; the specs only
    differ in the binary and argv."""
    from skep.workers.aider import AIDER_SPEC
    from skep.workers.claude_code.__main__ import CLAUDE_SPEC
    from skep.workers.codex import CODEX_SPEC
    from skep.workers.pi import PI_SPEC

    specs = [CLAUDE_SPEC, CODEX_SPEC, AIDER_SPEC, PI_SPEC]
    assert {s.caste for s in specs} == {"coding"}
    # Each carries a distinct binary + env override.
    assert {s.default_command for s in specs} == {("claude",), ("codex",), ("aider",), ("pi",)}
    assert len({s.command_env for s in specs}) == 4
    # Aider's argv must never auto-commit (patch-as-approval).
    assert "--no-auto-commit" in AIDER_SPEC.build_argv(["aider"], "do it")
    # pi's headless one-shot is -p; anything else would sit interactive forever.
    assert PI_SPEC.build_argv(["pi"], "do it") == ["pi", "-p", "do it"]


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
        "--settings",
        '{"sandbox": {"enabled": false}}',
        "--permission-mode",
        "bypassPermissions",
        "--print",
        "add multiply",
    ]


def test_claude_argv_turns_its_own_bash_sandbox_off() -> None:
    """ES-F221: Claude Code's Bash sandbox is bubblewrap on Linux. Off by
    default, but the operator's settings or the repo's own
    .claude/settings.json can turn it on, and inside skep's Landlock domain
    every Bash call then dies (`bwrap: Failed to make / slave: Operation not
    permitted`, reproduced 2026-11-13). --settings outranks user and project
    settings, so skep's sandbox stays the only wall (ADR 0047)."""
    import json

    from skep.workers.claude_code.__main__ import CLAUDE_SPEC

    argv = CLAUDE_SPEC.build_argv(["claude"], "x")
    assert json.loads(argv[argv.index("--settings") + 1]) == {"sandbox": {"enabled": False}}


def test_codex_argv_turns_its_own_sandbox_off() -> None:
    """ES-F128 → ES-F220: `codex exec` defaults to a READ-ONLY sandbox of its
    own (no edits), and workspace-write still starts codex's own bubblewrap
    for every shell call — which the kernel refuses inside skep's Landlock
    domain (T16: `bwrap: Failed to make / slave: Operation not permitted`,
    no patch). skep's sandbox is the wall (ADR 0047), so codex's is off."""
    from skep.workers.codex.__main__ import CODEX_SPEC

    argv = CODEX_SPEC.build_argv(["codex"], "add multiply")
    assert argv == ["codex", "exec", "--sandbox", "danger-full-access", "add multiply"]


@pytest.mark.parametrize(
    ("run", "cmd_env"),
    [
        (run_codex_task, "SKEP_CODEX_CMD"),
        (run_aider_task, "SKEP_AIDER_CMD"),
        (run_pi_task, "SKEP_PI_CMD"),
        # ES-F255 (D36): claude_code too — external agents never run unwalled.
        (run_claude_code_task, "SKEP_CLAUDE_CODE_CMD"),
    ],
)
def test_an_external_agent_refuses_to_start_outside_skeps_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run: Any, cmd_env: str
) -> None:
    """ES-F220: with codex's own sandbox off, skep's is its only wall — so a
    codex worker the spawner did not mark as sandboxed never runs codex.
    Aider and pi have no sandbox of their own and skip the capability layer
    too (ADR 0047), so the same refusal binds them."""
    monkeypatch.delenv("SKEP_SANDBOX")
    workspace = _seed_repo(tmp_path)
    ran = tmp_path / "agent-ran"
    fake = tmp_path / "fake-agent"
    fake.write_text(f"#!/bin/sh\ntouch {ran}\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv(cmd_env, str(fake))
    task = mint_task(
        workspace=workspace,
        instructions="edit",
        permissions=DEFAULT_PERMISSIONS,
        budget=DEFAULT_BUDGET,
    )
    task_path = tmp_path / "task.json"
    task_path.write_text(task.model_dump_json(indent=2), encoding="utf-8")
    out_path = tmp_path / "result.json"
    assert run(task_path, out_path) == 5  # EXIT_REJECTED
    result = read_result(out_path)
    assert result.status is TaskState.REJECTED
    assert "outside skep's sandbox" in result.summary
    assert not ran.exists()


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


def test_unattended_preamble_rides_every_external_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Headless agents that ask questions die patchless (019fc70f) and ones
    that tidy the workspace delete the supervisor's bookkeeping (019fc719) —
    the standing rules ride every dispatch, not the operator's typing."""
    _run_fake_codex(
        tmp_path,
        monkeypatch,
        "from pathlib import Path\n"
        "Path('argv.txt').write_text(' '.join(sys.argv), encoding='utf-8')\n",
    )
    argv_text = (tmp_path / "repo" / "argv.txt").read_text(encoding="utf-8")
    assert "never ask questions" in argv_text
    assert "never modify or delete them" in argv_text
    assert "do the thing" in argv_text  # the operator's instructions still ride


def test_v126_f5_the_memory_block_rides_an_external_dispatch_and_the_audit_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v126-F5: a Claude/Codex run gets the memory the envelope carries, and
    its task_start event records which items reached the prompt."""
    import json

    from skep.worker_contract import MemoryContextEntry

    memory = [
        MemoryContextEntry(memory_id="m1", memory_class="durable_preference", content="use uv"),
        MemoryContextEntry(
            memory_id="m2", memory_class="observation",
            content='(unreviewed, expires) <untrusted source="worker:t1">x</untrusted>',
        ),
    ]  # fmt: skip
    _run_fake_codex(
        tmp_path,
        monkeypatch,
        "from pathlib import Path\n"
        "Path('argv.txt').write_text(' '.join(sys.argv), encoding='utf-8')\n",
        memory=memory,
    )
    argv_text = (tmp_path / "repo" / "argv.txt").read_text(encoding="utf-8")
    assert "do the thing" in argv_text
    assert "- [durable_preference] use uv" in argv_text
    assert '<untrusted source="worker:t1">x</untrusted>' in argv_text
    assert "NOT authority" in argv_text
    (events,) = (tmp_path / "repo" / ".events").glob("*.ndjson")
    start = next(
        json.loads(line)
        for line in events.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["type"] == "task.start"
    )
    assert start["payload"]["memory_delivered"] == ["m1", "m2"]
    # v126-F5 (review): the recorded command keeps no copy of the memory —
    # `skep memory forget` can't reach an event log.
    commands = [
        json.loads(line)["payload"]["command"]
        for line in events.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["type"] in ("command.start", "command.result")
    ]
    agent = [c for c in commands if "do the thing" in c]
    assert len(agent) == 2
    assert all("[memory: 2 items]" in c and "use uv" not in c for c in agent)
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert "use uv" not in json.dumps(result)


def test_v126_f5_castes_without_memory_in_their_prompt_say_not_delivered() -> None:
    """v126-F5 (review): the reviewer has a prompt that carries no memory, and
    the audit and script workers run no model prompt (the verifier caste is
    retired, v127-F11) — each audit says "not delivered" instead of leaving
    the question open."""
    from types import SimpleNamespace
    from typing import Any, cast

    from skep.workers import audit, reviewer, script_worker

    task = cast(
        Any, SimpleNamespace(project_context=None, dispatch_decision=None, landing_decision=None)
    )
    for module in (reviewer, audit, script_worker):
        payload = module._task_start_payload(task)
        assert str(payload["memory_delivered"]).startswith("not delivered"), module.__name__


def test_v134_f14_whitespace_only_findings_are_a_risk_note_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v134-F14: one trailing space failed a Claude Code run on the live test
    with a bare "git diff --check failed" — the findings go to stdout, which the
    details never read. The adapter's check is hygiene; G10 (the pinned
    verify_command) decides what is verified, so whitespace is a risk note and
    the run still completes with its patch."""
    result = _run_fake_codex(
        tmp_path,
        monkeypatch,
        "from pathlib import Path\nPath('spaced.py').write_text('x = 1 \\n', encoding='utf-8')\n",
    )
    assert result.status is TaskState.COMPLETED
    assert result.verification.outcome is VerificationOutcome.PASSED
    assert "spaced.py:1: trailing whitespace." in result.verification.details
    assert result.risk_flags == ["diff-check-whitespace"]
    assert any(a.kind == "patch" for a in result.artifacts)
    assert result.changed_files == ["spaced.py"]


def test_v134_f14_a_leftover_conflict_marker_still_fails_and_says_where(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run_fake_codex(
        tmp_path,
        monkeypatch,
        "from pathlib import Path\n"
        "Path('merged.py').write_text('x = 1 \\n<<<<<<< HEAD\\ny = 2\\n', encoding='utf-8')\n",
    )
    assert result.status is TaskState.FAILED
    assert result.verification.outcome is VerificationOutcome.FAILED
    assert "merged.py:2: leftover conflict marker" in result.verification.details


def _real_diff_check(tmp_path: Path, files: dict[str, str]) -> str:
    """What `git diff --check` really prints for ``files`` written over a
    committed baseline — the fixture is git's output, never a hand-typed guess."""
    workspace = _seed_repo(tmp_path)
    for name in files:
        (workspace / name).write_text("base\n", encoding="utf-8")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-qm", "baseline")
    for name, text in files.items():
        (workspace / name).write_text(text, encoding="utf-8")
    proc = subprocess.run(
        ["git", "-C", str(workspace), "diff", "--check"], capture_output=True, text=True
    )
    assert proc.returncode == 2, proc  # git's "the check found problems" code
    return proc.stdout


@pytest.mark.parametrize(
    ("files", "whitespace_only"),
    [
        ({"a.py": "x = 1 \n"}, True),
        ({"a.py": " \tx = 1\n"}, True),
        ({"a.py": "x = 1\n\n"}, True),  # a blank line at EOF
        ({"a.py": "x = 1 \n", "b.py": " \ty\n", "c.py": "z\n\n"}, True),
        # the echoed line can say anything; it is skipped by position, not read
        ({"a.py": "b.py:3: leftover conflict marker \n"}, True),
        # a header that starts with "+" is still a header (Sol's misread class)
        ({"+notes.txt": "x \n"}, True),
        ({"a.py": "<<<<<<< HEAD\n"}, False),
        ({"a.py": "x = 1 \n", "b.py": "<<<<<<< HEAD\ny\n"}, False),
    ],
)
def test_v134_f14_whitespace_classifier_reads_real_git_output(
    tmp_path: Path, files: dict[str, str], whitespace_only: bool
) -> None:
    """v134-F14 r1: git prints `path:line: message` and then echoes the
    offending `+line`; the classifier read the echo as content and misjudged
    real output. It now parses the headers and skips the echo under each."""
    from skep.workers.cli_adapter import _diff_check_findings, _whitespace_only

    findings = _diff_check_findings(_real_diff_check(tmp_path, files))
    assert findings and all(":" in line and not line.startswith("+x") for line in findings)
    assert _whitespace_only(findings) is whitespace_only


def test_v134_f14_unknown_or_empty_output_is_not_whitespace() -> None:
    from skep.workers.cli_adapter import _diff_check_findings, _whitespace_only

    assert _whitespace_only(_diff_check_findings("")) is False
    assert _whitespace_only(_diff_check_findings("a.py:1: a message git adds later\n")) is False
    assert _whitespace_only(_diff_check_findings("warning: something else\n")) is False


def test_v134_f14_a_timed_out_check_is_never_whitespace_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v134-F14 r1: a check killed at its timeout has partial output — the
    conflict marker may sit past the cut. Only a completed check classifies."""
    from skep.workers import cli_adapter

    real_run = cli_adapter._run

    def run(argv: list[str], **kwargs: Any) -> Any:
        proc, record = real_run(argv, **kwargs)
        if kwargs["purpose"] == "verify":
            proc = subprocess.CompletedProcess(argv, -1, proc.stdout, "command timed out after 1s")
        return proc, record

    monkeypatch.setattr(cli_adapter, "_run", run)
    result = _run_fake_codex(
        tmp_path,
        monkeypatch,
        "from pathlib import Path\nPath('spaced.py').write_text('x = 1 \\n', encoding='utf-8')\n",
    )
    assert result.status is TaskState.FAILED
    assert result.risk_flags == []
    assert "spaced.py:1: trailing whitespace." in result.verification.details


def test_v134_f14_details_keep_the_finding_lines_not_the_echoed_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v134-F14 r1: the echoed `+line` is the file's content — a `.env` value
    rode into verification_details. Only `path:line: message` is kept."""
    value = "v" * 20  # built at runtime: no credential-shaped literal in git
    result = _run_fake_codex(
        tmp_path,
        monkeypatch,
        "from pathlib import Path\n"
        f"Path('settings.py').write_text('MODE = \"{value}\" \\n', encoding='utf-8')\n",
    )
    assert result.status is TaskState.COMPLETED
    assert "settings.py:1: trailing whitespace." in result.verification.details
    assert value not in result.verification.details


def test_v134_f14_the_agent_tail_in_the_details_is_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "p" * 24  # built at runtime
    result = _run_fake_codex(
        tmp_path,
        monkeypatch,
        f"print('push failed for https://{secret}@git.example/x.git')\nraise SystemExit(1)\n",
    )
    assert result.status is TaskState.FAILED
    assert "https://***@git.example/x.git" in result.verification.details
    assert secret not in result.verification.details


def test_v134_f14_a_credential_the_tail_cut_would_split_is_masked_first() -> None:
    secret = "p" * 24  # built at runtime
    words = "I pushed the branch and it failed. " * 15
    tail = _tail_of(f"see https://{secret}@git.example/x.git " + words[:560])
    assert "p" * 6 not in tail


def test_v134_f14_the_whitespace_risk_flag_blocks_the_default_auto_landing_rules() -> None:
    """The flag is what keeps a declarative rule from landing the patch
    unattended (forbid_risk_flags=True on every default rule)."""
    from skep.supervisor.policy import (
        SAFE_DEPENDENCY_RULE,
        VERIFIED_PATCH_RULE,
        AutoApprovalContext,
        evaluate,
    )

    def ctx(risk_flags: tuple[str, ...]) -> AutoApprovalContext:
        return AutoApprovalContext(
            verification_outcome="passed",
            reverify_confirmed=True,
            risk_flags=risk_flags,
            changed_files=("uv.lock",),
            verify_pinned=True,
            reverify_commands=("uv run pytest",),
        )

    rules = (VERIFIED_PATCH_RULE, SAFE_DEPENDENCY_RULE)
    assert evaluate(rules, ctx(())) is not None  # the control: it would land
    assert evaluate(rules, ctx(("diff-check-whitespace",))) is None


def test_v134_f15_a_timed_out_agent_keeps_its_partial_output(tmp_path: Path) -> None:
    """v134-F15: on POSIX TimeoutExpired carries bytes even under text=True, and
    the adapter kept only str — a run killed at its wall clock (01a2123b, mid
    `npm install`) left no trace of what it was doing."""
    import json

    from skep.workers.cli_adapter import _agent_tail, _EventStream, _run

    stream = _EventStream(tmp_path / "events.ndjson", task_id="t", trace_id="tr")
    script = "import sys,time; print('installing deps', flush=True); "
    script += "sys.stderr.write('npm WARN caf\\u00e9\\n'); sys.stderr.flush(); time.sleep(30)"
    proc, _record = _run(
        [sys.executable, "-c", script], cwd=tmp_path, timeout=1, stream=stream, purpose="agent"
    )
    assert proc.returncode == -1
    assert "installing deps" in proc.stdout
    assert "npm WARN café" in proc.stderr
    assert _agent_tail(proc).endswith("npm WARN café\ncommand timed out after 1s")
    result = json.loads((tmp_path / "events.ndjson").read_text().splitlines()[-1])
    assert "installing deps" in result["payload"]["stdout"]


def test_v134_f15_a_timed_out_agent_that_wrote_only_stdout_keeps_its_words(
    tmp_path: Path,
) -> None:
    """v134-F15 r1: the timeout note filled stderr, which the tail reads
    first, so an agent that writes to stdout (claude --print, codex) and hung
    was summed up as just "command timed out". Its own words come first."""
    from skep.workers.cli_adapter import _agent_tail, _EventStream, _run

    stream = _EventStream(tmp_path / "events.ndjson", task_id="t", trace_id="tr")
    script = "import time; print('I was halfway through editing parser.py', flush=True); "
    script += "time.sleep(30)"
    proc, _record = _run(
        [sys.executable, "-c", script], cwd=tmp_path, timeout=1, stream=stream, purpose="agent"
    )
    assert proc.returncode == -1
    assert proc.stderr == "command timed out after 1s"  # the event still says why
    assert _agent_tail(proc) == (
        "I was halfway through editing parser.py\ncommand timed out after 1s"
    )


def _tail_of(stdout: str) -> str:
    from skep.workers.cli_adapter import _agent_tail

    return _agent_tail(subprocess.CompletedProcess(["agent"], 0, stdout, ""))


def test_v134_f16_the_agent_tail_starts_at_a_sentence() -> None:
    """v134-F16: the last words were cut to 200 chars mid-word ("ts in the
    worktree…"). Keep about 600 and start where a sentence starts."""
    words = "I edited the parser and ran the tests in the worktree. " * 20
    tail = _tail_of(words + "All tests pass.")
    assert 400 < len(tail) <= 600
    assert tail.startswith("I edited the parser")
    assert tail.endswith("All tests pass.")


def test_v134_f16_without_a_sentence_the_tail_starts_at_a_word() -> None:
    tail = _tail_of(" ".join(f"word{n}" for n in range(400)))
    assert 400 < len(tail) <= 600
    first = tail.split()[0]
    assert first.startswith("word") and first[4:].isdigit()
    assert tail.endswith("word399")
