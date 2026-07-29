"""CLI loop tests (G6): run → status → review → approve/deny, all hermetic."""

from __future__ import annotations

import builtins
import json
import shlex
import sys
from pathlib import Path

import pytest

from skep import __version__
from skep.cli import main
from skep.supervisor import RunStore, WorkflowTemplate, cli_cmds, mint_task
from skep.supervisor.autonomy import AutonomyDecision
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.ingest import IngestOutcome
from skep.supervisor.scheduler import make_schedule, run_due
from skep.supervisor.store import RunRecord
from skep.worker_contract import (
    CONTRACT_VERSION,
    ApprovalVerdict,
    AutonomyDecisionPayload,
    Event,
    Permissions,
    ProjectContextPayload,
)

from .conftest import FAKE_WORKER, git


def _worker_cmd() -> str:
    return shlex.join([sys.executable, str(FAKE_WORKER)])


def _project_dispatch_decision(
    *, reason: str, project_id: str, phase: str, strategy: str = "trusted_local_dev"
) -> dict[str, object]:
    return {
        "verdict": "allow",
        "reason": reason,
        "detail": None,
        "decided_by": None,  # v40-F8 additive field
        "project_id": project_id,
        "strategy": strategy,
        "phase": phase,
        "policy_source": "project_policy",
        # v23-F5: trusted dev workspace runs with no explicit network resolve
        # the package-registry hosts into the audit constraints.
        "constraints": {
            "network_requested": None,
            "network_resolved": [
                "files.pythonhosted.org",
                "proxy.golang.org",
                "pypi.org",
                "registry.npmjs.org",
            ],
        },
    }


def _run_cli(home: Path, *args: str) -> int:
    return main(["--home", str(home), *args])


def test_cli_version_reports_worker_contract(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0

    assert capsys.readouterr().out.strip() == (
        f"skep {__version__} (worker contract {CONTRACT_VERSION})"
    )


def test_setup_prints_the_next_steps(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """v27-F5: setup ends by saying what comes next, not just what happened."""
    assert _run_cli(tmp_path / "home", "setup", "--personal") == 0
    out = capsys.readouterr().out
    assert "next steps:" in out
    assert "skep serve" in out
    assert "http://127.0.0.1:8765/" in out
    assert "skep doctor" in out


def test_setup_wizard_prompts_on_tty_and_fills_the_profile(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v37-F3: a fresh TTY setup reaches a provider config with no flags typed."""
    import types

    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    answers = iter(["ollama", "qwen3:14b", "", "OLLAMA_API_KEY"])
    monkeypatch.setattr(builtins, "input", lambda prompt: next(answers))

    home = tmp_path / "home"
    assert _run_cli(home, "setup", "--personal") == 0

    profile = json.loads((home / "profile.json").read_text(encoding="utf-8"))
    assert profile["provider"]["name"] == "ollama"
    assert profile["provider"]["model"] == "qwen3:14b"
    assert profile["provider"]["endpoint"] == "http://localhost:11434"
    assert profile["provider"]["api_key_env"] == "OLLAMA_API_KEY"
    out = capsys.readouterr().out
    assert "next steps:" in out  # the v27-F5 epilogue survives the wizard


def test_setup_wizard_silent_without_tty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-TTY stdin is byte-identical to the flag-driven path: no prompts."""
    import types

    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: False))

    def refuse(prompt: str) -> str:
        raise AssertionError("wizard must not prompt without a TTY")

    monkeypatch.setattr(builtins, "input", refuse)
    home = tmp_path / "home"
    assert _run_cli(home, "setup", "--personal") == 0

    profile = json.loads((home / "profile.json").read_text(encoding="utf-8"))
    assert profile["provider"]["name"] == "unconfigured"


def test_setup_wizard_suppressed_by_provider_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flags always win: no prompts even on a TTY."""
    import types

    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: True))

    def refuse(prompt: str) -> str:
        raise AssertionError("wizard must not prompt when flags are given")

    monkeypatch.setattr(builtins, "input", refuse)
    home = tmp_path / "home"
    assert _run_cli(home, "setup", "--personal", "--provider", "mock") == 0

    profile = json.loads((home / "profile.json").read_text(encoding="utf-8"))
    assert profile["provider"]["name"] == "mock"


def test_top_level_help_describes_launch_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--home HOME" in out
    assert "supervisor home directory" in out
    assert "setup" in out
    assert "create or update the local personal profile" in out
    assert "doctor" in out
    assert "check local configuration and runtime readiness" in out
    assert "status" in out
    assert "show setup, provider, sandbox, and approval status" in out
    assert "start" in out
    assert "start the local status dashboard" in out


def test_serve_help_describes_bind_options(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["serve", "--help"])

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--host HOST" in out
    assert "HTTP bind host" in out
    assert "--port PORT" in out
    assert "HTTP bind port" in out


def test_build_config_resolves_relative_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    config = build_config(Path("home"), None)

    assert config.home.is_absolute()
    assert config.home == (tmp_path / "home" / "supervisor").resolve()


def _only_task_id(home: Path) -> str:
    store = RunStore(home / "supervisor" / "supervisor.sqlite3")
    try:
        runs = store.recent_runs(10)
        assert len(runs) == 1
        return runs[0].task_id
    finally:
        store.close()


def test_run_status_review_approve_full_loop(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"

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
    out = capsys.readouterr().out
    assert code == 0
    assert "state:        completed" in out
    assert "verification: passed" in out

    task_id = _only_task_id(home)

    assert _run_cli(home, "status", "--personal") == 0
    out = capsys.readouterr().out
    assert task_id[:12] in out
    assert "completed" in out

    # Plain review shows the evidence (pager falls back to print when not a tty).
    assert _run_cli(home, "review", task_id) == 0
    out = capsys.readouterr().out
    assert "verification: passed" in out
    assert "+value = 1" in out, "patch text missing from review output"

    head_before = git(repo, "rev-parse", "HEAD").stdout.strip()
    assert _run_cli(home, "review", task_id, "--approve", "--actor", "tester") == 0
    out = capsys.readouterr().out
    assert f"branch skep/{task_id}" in out

    # The patch landed on the branch, never on the current branch (Q5).
    assert git(repo, "rev-parse", "--verify", f"refs/heads/skep/{task_id}")
    branch_content = git(repo, "show", f"skep/{task_id}:existing.py").stdout
    assert branch_content == "value = 1\n"
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == head_before
    assert (repo / "existing.py").read_text() == "value = 0\n"

    # Verdict + actor recorded in the approval queue.
    store = RunStore(home / "supervisor" / "supervisor.sqlite3")
    try:
        approvals = store.approvals_for(task_id)
        assert len(approvals) == 1
        assert approvals[0].status == "approved"
        assert approvals[0].resolved_by == "tester"
    finally:
        store.close()

    # Approving twice fails loudly: the branch already exists.
    code = _run_cli(home, "review", task_id, "--approve", "--actor", "tester")
    captured = capsys.readouterr()
    assert code == 2
    assert "already exists" in captured.err


def test_review_approve_lands_on_named_branch(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """v20-F5: `skep review --approve --branch <name>` lands on the named branch."""
    home = tmp_path / "home"
    assert (
        _run_cli(
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
        == 0
    )
    capsys.readouterr()
    task_id = _only_task_id(home)

    assert (
        _run_cli(home, "review", task_id, "--approve", "--actor", "tester", "--branch", "sci-cal")
        == 0
    )
    out = capsys.readouterr().out
    assert "branch sci-cal" in out
    assert git(repo, "show", "sci-cal:existing.py").stdout == "value = 1\n"
    # The default skep/<task_id> branch was NOT created.
    assert git(repo, "branch", "--list", f"skep/{task_id}").stdout.strip() == ""

    # An invalid branch name is refused before anything is applied.
    code = _run_cli(
        home, "review", task_id, "--approve", "--actor", "tester", "--branch", "../evil"
    )
    assert code == 2


def test_doctor_reports_stale_pending_runs(tmp_path: Path) -> None:
    """v20-F6: doctor lists a >7-day pending_approval run with its deny command."""
    from datetime import UTC, datetime

    from skep.status import build_status, format_doctor_report

    home = tmp_path / "personal"
    db_path = home / "supervisor" / "supervisor.sqlite3"
    db_path.parent.mkdir(parents=True)
    store = RunStore(db_path)
    try:
        task = mint_task(workspace=tmp_path / "ws", instructions="Investigate the thing.")
        store.create_run(task, repo=tmp_path / "repo", ref=None, execution_mode="workspace")
        store.transition(task.task_id, "pending_approval", "waiting on approval")
    finally:
        store.close()

    # Viewed from far in the future the fresh gate is > 7 days stale.
    report = format_doctor_report(build_status(home, now=datetime(2099, 1, 1, tzinfo=UTC)))
    assert "Stale approvals" in report
    assert task.task_id in report
    assert f"skep review {task.task_id} --deny" in report

    # With the real clock nothing is stale yet.
    assert build_status(home)["stale_pending"] == []


def test_doctor_flags_deprecated_global_auto_approve(tmp_path: Path) -> None:
    """v23-F6 → v81-F14: a store carrying auto_approve=true is told the toggle
    is inert now, and how to clear it."""
    from skep.status import build_status

    home = tmp_path / "personal"
    db_path = home / "supervisor" / "supervisor.sqlite3"
    db_path.parent.mkdir(parents=True)
    store = RunStore(db_path)
    try:
        store.set_setting("auto_approve", True)
    finally:
        store.close()

    advisories = build_status(home)["advisories"]
    assert any("INERT" in advisory and "set-phase" in advisory for advisory in advisories)

    store = RunStore(db_path)
    try:
        store.set_setting("auto_approve", False)
    finally:
        store.close()
    assert not any("DEPRECATED" in a for a in build_status(home)["advisories"])


def test_cli_run_merges_configured_provider_host_into_network(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """v19-F2: a run minted via the CLI carries the configured provider host."""
    from skep.profile import run_personal_setup

    home = tmp_path / "home"
    run_personal_setup(
        home,
        provider="openai-compat",
        model="gpt-oss",
        endpoint="http://provider.example:11434",
        api_key_env=None,
    )

    code = _run_cli(
        home,
        "run",
        str(repo),
        "Fix the bug. MODE:happy",
        "--no-template",
        "--execution-mode",
        "workspace",
        "--worker-cmd",
        _worker_cmd(),
        "--quiet",
    )
    assert code == 0, capsys.readouterr()
    task_id = _only_task_id(home)
    task = json.loads(
        (home / "supervisor" / "audit" / task_id / "task.json").read_text(encoding="utf-8")
    )
    assert "provider.example" in task["permissions"]["network"]


def test_status_personal_moves_superseded_runs_out_of_the_table(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """v19-F8: a superseded run is listed under a 'no action' section, not the
    main table (so it does not read as needing attention)."""
    home = tmp_path / "home"
    config = build_config(home, None)
    store = RunStore(config.db_path)
    try:
        task = mint_task(workspace=repo, instructions="x")
        store.create_run(task, repo=repo, ref=None, execution_mode="sandbox")
        store.transition(task.task_id, "superseded", "resumed as succ-123")
    finally:
        store.close()

    assert _run_cli(home, "status", "--personal") == 0
    out = capsys.readouterr().out
    assert "superseded run(s) (resumed as a successor; no action)" in out
    assert f"{task.task_id[:12]}  superseded" in out


def test_pending_run_can_be_denied(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    code = _run_cli(
        home,
        "run",
        str(repo),
        "Commit it. MODE:pending",
        "--execution-mode",
        "workspace",
        "--worker-cmd",
        _worker_cmd(),
        "--quiet",
    )
    assert code == 4  # exit code mirrors pending_approval
    out = capsys.readouterr().out
    assert "--approve | --deny" in out

    task_id = _only_task_id(home)
    assert _run_cli(home, "review", task_id, "--deny", "--actor", "tester", "--note", "no") == 0
    store = RunStore(home / "supervisor" / "supervisor.sqlite3")
    try:
        approvals = store.approvals_for(task_id)
        assert [a.status for a in approvals] == ["denied"]
        assert approvals[0].resolved_by == "tester"
        assert approvals[0].resolution_note == "no"
    finally:
        store.close()


def test_run_can_approve_pending_gate_inline(
    repo: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(cli_cmds, "_stdin_is_interactive", lambda: True, raising=False)
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "a")

    code = _run_cli(
        home,
        "run",
        str(repo),
        "Commit it. MODE:pending",
        "--execution-mode",
        "workspace",
        "--worker-cmd",
        _worker_cmd(),
    )
    out = capsys.readouterr().out

    assert code == 0
    assert "approval needed: git_commit" in out
    assert "resumed:" in out

    store = RunStore(home / "supervisor" / "supervisor.sqlite3")
    try:
        runs = store.recent_runs(10)
        resumed = next(run for run in runs if run.resume_of is not None)
        original = next(run for run in runs if run.task_id == resumed.resume_of)
        approvals = store.approvals_for(original.task_id)
        assert original.state == "pending_approval"
        assert resumed.state == "completed"
        assert [approval.status for approval in approvals] == ["approved"]
    finally:
        store.close()


def test_pending_shell_run_can_be_allowed_and_resumed(
    repo: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    config = build_config(home, None)
    write_argv = [
        sys.executable,
        "-c",
        "from pathlib import Path; Path('generated.py').write_text(\"print('from shell')\\n\")",
    ]
    approval_reason = f"shell.run requires approval for command: {shlex.join(write_argv)}"
    task = mint_task(
        workspace=repo,
        instructions="Use a shell command that needs approval.",
        permissions=Permissions(
            read=["workspace"],
            write=["workspace"],
            network=["*"],
            env_allowlist=[],
        ),
    )
    audit_dir = config.audit_dir / task.task_id
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "task.json").write_text(task.model_dump_json(indent=2) + "\n", encoding="utf-8")

    store = RunStore(config.db_path)
    try:
        store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
        store.transition(task.task_id, "pending_approval")
        review_id = store.enqueue_approval(task.task_id, action="shell.run", reason=approval_reason)
        store.ingest_events(
            [
                Event.model_validate(
                    {
                        "contract_version": task.contract_version,
                        "event_id": "approval-requested-1",
                        "seq": 1,
                        "task_id": task.task_id,
                        "trace_id": task.trace_id,
                        "ts": "2026-06-16T00:00:00Z",
                        "type": "approval.requested",
                        "payload": {
                            "action": "shell.run",
                            "reason": approval_reason,
                            "decision": {
                                "verdict": "require_approval",
                                "reason": (
                                    "capability.require_approval.shell_nonverify_not_allowlisted"
                                ),
                                "detail": shlex.join(write_argv),
                            },
                        },
                    }
                )
            ]
        )
    finally:
        store.close()

    observed: dict[str, object] = {}

    def fake_run_task(*args: object, **kwargs: object) -> IngestOutcome:
        observed["resume_of"] = kwargs["resume_of"]
        observed["approval_verdict"] = kwargs["approval_verdict"]
        observed["dispatch_decision"] = kwargs["dispatch_decision"]
        record = RunRecord(
            task_id="resumed-1",
            trace_id="trace-resumed-1",
            repo=str(repo),
            ref=None,
            workspace=str(repo),
            execution_mode="workspace",
            instructions=str(kwargs["instructions"] if "instructions" in kwargs else args[1]),
            state="completed",
            summary="completed after command allow",
            verification_outcome="passed",
            verification_details="ok",
            worker_version="fake",
            manifest_fingerprint="f" * 64,
            resume_of=task.task_id,
            created_at="2026-06-16T00:00:01Z",
            updated_at="2026-06-16T00:00:01Z",
        )
        return IngestOutcome(record=record, review_id=None)

    monkeypatch.setattr(cli_cmds, "run_task", fake_run_task)

    code = _run_cli(home, "review", task.task_id, "--allow-command", "--actor", "tester")
    out = capsys.readouterr().out
    assert code == 0, out
    assert "resumed: " in out
    assert "resumed-1" in out

    store = RunStore(config.db_path)
    try:
        assert store.get_setting("allowed_shell_commands") == [write_argv]
        approvals = store.approvals_for(task.task_id)
        assert len(approvals) == 1
        assert approvals[0].status == "approved"
        assert approvals[0].resolved_by == "tester"
        assert approvals[0].resolution_note == "resumed as resumed-1 (completed)"
        ledger = store.ledger_for_repo(repo)
        assert len(ledger) == 1
        assert ledger[0].review_id == review_id
        assert ledger[0].resource == shlex.join(write_argv)
        assert ledger[0].remembered is True
    finally:
        store.close()

    approval_verdict = observed["approval_verdict"]
    dispatch_decision = observed["dispatch_decision"]
    assert observed["resume_of"] == task.task_id
    assert isinstance(approval_verdict, ApprovalVerdict)
    assert isinstance(dispatch_decision, AutonomyDecision)
    assert approval_verdict.action == "shell.run"
    assert approval_verdict.reason == approval_reason
    assert approval_verdict.decision is not None
    assert approval_verdict.decision.model_dump() == {
        "verdict": "require_approval",
        "reason": "capability.require_approval.shell_nonverify_not_allowlisted",
        "detail": shlex.join(write_argv),
        "decided_by": None,  # v40-F8 additive field
    }
    assert dispatch_decision.reason == "dispatch.allow.resume_after_approval"
    assert dispatch_decision.detail == task.task_id


def test_inline_shell_approval_can_be_remembered(
    repo: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    config = build_config(home, None)
    write_argv = [
        sys.executable,
        "-c",
        "from pathlib import Path; Path('generated.py').write_text(\"print('from shell')\\n\")",
    ]
    approval_reason = f"shell.run requires approval for command: {shlex.join(write_argv)}"
    task = mint_task(
        workspace=repo,
        instructions="Use a shell command that needs approval.",
        permissions=Permissions(
            read=["workspace"],
            write=["workspace"],
            network=["*"],
            env_allowlist=[],
        ),
    )
    audit_dir = config.audit_dir / task.task_id
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "task.json").write_text(task.model_dump_json(indent=2) + "\n", encoding="utf-8")

    store = RunStore(config.db_path)
    try:
        store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
        store.transition(task.task_id, "pending_approval")
        review_id = store.enqueue_approval(task.task_id, action="shell.run", reason=approval_reason)
        store.ingest_events(
            [
                Event.model_validate(
                    {
                        "contract_version": task.contract_version,
                        "event_id": "approval-requested-1",
                        "seq": 1,
                        "task_id": task.task_id,
                        "trace_id": task.trace_id,
                        "ts": "2026-06-16T00:00:00Z",
                        "type": "approval.requested",
                        "payload": {
                            "action": "shell.run",
                            "reason": approval_reason,
                            "decision": {
                                "verdict": "require_approval",
                                "reason": (
                                    "capability.require_approval.shell_nonverify_not_allowlisted"
                                ),
                                "detail": shlex.join(write_argv),
                            },
                        },
                    }
                )
            ]
        )
        record = store.get_run(task.task_id)
        assert record is not None
    finally:
        store.close()

    observed: dict[str, object] = {}

    def fake_run_task(*args: object, **kwargs: object) -> IngestOutcome:
        observed["approval_verdict"] = kwargs["approval_verdict"]
        record = RunRecord(
            task_id="resumed-1",
            trace_id="trace-resumed-1",
            repo=str(repo),
            ref=None,
            workspace=str(repo),
            execution_mode="workspace",
            instructions=str(kwargs["instructions"] if "instructions" in kwargs else args[1]),
            state="completed",
            summary="completed after remembered command",
            verification_outcome="passed",
            verification_details="ok",
            worker_version="fake",
            manifest_fingerprint="f" * 64,
            resume_of=task.task_id,
            created_at="2026-06-16T00:00:01Z",
            updated_at="2026-06-16T00:00:01Z",
        )
        return IngestOutcome(record=record, review_id=None)

    monkeypatch.setattr(cli_cmds, "_stdin_is_interactive", lambda: True, raising=False)
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "b")
    monkeypatch.setattr(cli_cmds, "run_task", fake_run_task)

    assert cli_cmds._prompt_inline_approval(config, record) == 0
    out = capsys.readouterr().out
    assert "[b] approve + remember" in out
    assert "resumed: " in out
    assert "saved template" in out

    store = RunStore(config.db_path)
    try:
        assert store.get_setting("allowed_shell_commands") == [write_argv]
        approvals = store.approvals_for(task.task_id)
        assert [approval.status for approval in approvals] == ["approved"]
        ledger = store.ledger_for_repo(repo)
        assert len(ledger) == 1
        assert ledger[0].review_id == review_id
        assert ledger[0].remembered is True
        assert ledger[0].task_outcome == "completed"
        templates = store.list_templates()
        assert len(templates) == 1
        assert templates[0].provenance == "learned"
        assert templates[0].repo == str(repo)
        assert templates[0].instructions == task.instructions
        assert templates[0].shell_allowlist == (tuple(write_argv),)
    finally:
        store.close()

    approval_verdict = observed["approval_verdict"]
    assert isinstance(approval_verdict, ApprovalVerdict)
    assert approval_verdict.action == "shell.run"


def test_inline_approval_choice_reads_single_interactive_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_cmds, "_stdin_is_interactive", lambda: True, raising=False)
    monkeypatch.setattr(cli_cmds, "_read_single_key", lambda: "b", raising=False)
    monkeypatch.setattr(
        builtins,
        "input",
        lambda _prompt="": pytest.fail("interactive approval should not require Enter"),
    )

    assert cli_cmds._read_approval_choice() == "b"


def test_inline_network_approval_can_be_remembered(
    repo: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    config = build_config(home, None)
    approval_reason = "network.fetch requires approval with a task network allowlist"
    task = mint_task(
        workspace=repo,
        instructions="Add a login page that needs package metadata.",
        permissions=Permissions(
            read=["workspace"],
            write=["workspace"],
            network=[],
            env_allowlist=[],
        ),
    )
    audit_dir = config.audit_dir / task.task_id
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "task.json").write_text(task.model_dump_json(indent=2) + "\n", encoding="utf-8")

    store = RunStore(config.db_path)
    try:
        store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
        store.transition(task.task_id, "pending_approval")
        review_id = store.enqueue_approval(
            task.task_id, action="network.fetch", reason=approval_reason
        )
        store.ingest_events(
            [
                Event.model_validate(
                    {
                        "contract_version": task.contract_version,
                        "event_id": "approval-requested-network",
                        "seq": 1,
                        "task_id": task.task_id,
                        "trace_id": task.trace_id,
                        "ts": "2026-06-26T00:00:00Z",
                        "type": "approval.requested",
                        "payload": {
                            "action": "network.fetch",
                            "reason": approval_reason,
                            "decision": {
                                "verdict": "require_approval",
                                "reason": "capability.require_approval.network_allowlist_missing",
                                "detail": "https://pypi.org/simple/pyjwt/",
                                "decided_by": None,  # v40-F8 additive field
                            },
                        },
                    }
                )
            ]
        )
        record = store.get_run(task.task_id)
        assert record is not None
    finally:
        store.close()

    def fake_run_task(*args: object, **kwargs: object) -> IngestOutcome:
        return IngestOutcome(
            record=RunRecord(
                task_id="resumed-network",
                trace_id="trace-resumed-network",
                repo=str(repo),
                ref=None,
                workspace=str(repo),
                execution_mode="workspace",
                instructions=str(kwargs["instructions"] if "instructions" in kwargs else args[1]),
                state="completed",
                summary="completed after remembered network approval",
                verification_outcome="passed",
                verification_details="ok",
                worker_version="fake",
                manifest_fingerprint="f" * 64,
                resume_of=task.task_id,
                created_at="2026-06-26T00:00:01Z",
                updated_at="2026-06-26T00:00:01Z",
            ),
            review_id=None,
        )

    monkeypatch.setattr(cli_cmds, "_stdin_is_interactive", lambda: True, raising=False)
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "b")
    monkeypatch.setattr(cli_cmds, "run_task", fake_run_task)

    assert cli_cmds._prompt_inline_approval(config, record) == 0
    out = capsys.readouterr().out
    assert "[b] approve + remember" in out
    assert "update template" not in out
    assert "saved template" in out

    store = RunStore(config.db_path)
    try:
        ledger = store.ledger_for_repo(repo)
        assert len(ledger) == 1
        assert ledger[0].review_id == review_id
        assert ledger[0].resource == "https://pypi.org/simple/pyjwt/"
        assert ledger[0].remembered is True
        assert ledger[0].task_outcome == "completed"
        templates = store.list_templates()
        assert len(templates) == 1
        assert templates[0].provenance == "learned"
        assert templates[0].repo == str(repo)
        assert templates[0].network == ("pypi.org",)
    finally:
        store.close()


def test_inline_approval_prompts_again_when_resume_hits_another_gate(
    repo: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    config = build_config(home, None)
    original = mint_task(
        workspace=repo,
        instructions="Commit in two gated steps.",
        permissions=Permissions(
            read=["workspace"],
            write=["workspace"],
            network=[],
            env_allowlist=[],
        ),
    )
    audit_dir = config.audit_dir / original.task_id
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "task.json").write_text(
        original.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )

    store = RunStore(config.db_path)
    try:
        store.create_run(original, repo=repo, ref=None, execution_mode="workspace")
        store.transition(original.task_id, "pending_approval")
        store.enqueue_approval(
            original.task_id,
            action="git.stage",
            reason="git.stage requires approval",
        )
        record = store.get_run(original.task_id)
        assert record is not None
    finally:
        store.close()

    second_task_id = ""

    def fake_run_task(*args: object, **kwargs: object) -> IngestOutcome:
        nonlocal second_task_id
        run_store = kwargs["store"]
        assert isinstance(run_store, RunStore)
        resume_of = kwargs["resume_of"]
        if resume_of == original.task_id:
            approval_verdict = kwargs["approval_verdict"]
            assert isinstance(approval_verdict, ApprovalVerdict)
            second = mint_task(
                workspace=repo,
                instructions=original.instructions,
                permissions=original.permissions,
                resume_of=original.task_id,
                approval_verdict=approval_verdict,
            )
            second_task_id = second.task_id
            second_audit_dir = config.audit_dir / second.task_id
            second_audit_dir.mkdir(parents=True, exist_ok=True)
            (second_audit_dir / "task.json").write_text(
                second.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
            run_store.create_run(second, repo=repo, ref=None, execution_mode="workspace")
            run_store.transition(second.task_id, "pending_approval")
            run_store.enqueue_approval(
                second.task_id,
                action="git.commit",
                reason="git.commit requires approval",
            )
            pending = run_store.get_run(second.task_id)
            assert pending is not None
            return IngestOutcome(record=pending, review_id=None)

        assert resume_of == second_task_id
        return IngestOutcome(
            record=RunRecord(
                task_id="completed-after-second-approval",
                trace_id="trace-completed-after-second-approval",
                repo=str(repo),
                ref=None,
                workspace=str(repo),
                execution_mode="workspace",
                instructions=original.instructions,
                state="completed",
                summary="completed after second approval",
                verification_outcome="passed",
                verification_details="ok",
                worker_version="fake",
                manifest_fingerprint="f" * 64,
                resume_of=second_task_id,
                created_at="2026-06-26T00:00:02Z",
                updated_at="2026-06-26T00:00:02Z",
            ),
            review_id=None,
        )

    choices = iter(["a", "a"])
    monkeypatch.setattr(cli_cmds, "_stdin_is_interactive", lambda: True, raising=False)
    monkeypatch.setattr(builtins, "input", lambda _prompt="": next(choices))
    monkeypatch.setattr(cli_cmds, "run_task", fake_run_task)

    assert cli_cmds._prompt_inline_approval(config, record) == 0
    out = capsys.readouterr().out
    assert "approval needed: git.stage" in out
    assert "approval needed: git.commit" in out
    assert "resumed: " in out
    assert "completed-after-second-approval" in out

    store = RunStore(config.db_path)
    try:
        assert [approval.status for approval in store.approvals_for(original.task_id)] == [
            "approved"
        ]
        assert [approval.status for approval in store.approvals_for(second_task_id)] == ["approved"]
    finally:
        store.close()


def test_remembered_approval_survives_multi_gate_resume_chain(
    repo: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    config = build_config(home, None)
    approval_reason = "network.fetch requires approval with a task network allowlist"
    original = mint_task(
        workspace=repo,
        instructions="Add login with JWT package metadata.",
        permissions=Permissions(
            read=["workspace"],
            write=["workspace"],
            network=[],
            env_allowlist=[],
        ),
    )
    audit_dir = config.audit_dir / original.task_id
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "task.json").write_text(
        original.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )

    store = RunStore(config.db_path)
    try:
        store.create_run(original, repo=repo, ref=None, execution_mode="workspace")
        store.transition(original.task_id, "pending_approval")
        network_review_id = store.enqueue_approval(
            original.task_id,
            action="network.fetch",
            reason=approval_reason,
        )
        store.ingest_events(
            [
                Event.model_validate(
                    {
                        "contract_version": original.contract_version,
                        "event_id": "approval-requested-network",
                        "seq": 1,
                        "task_id": original.task_id,
                        "trace_id": original.trace_id,
                        "ts": "2026-06-26T00:00:00Z",
                        "type": "approval.requested",
                        "payload": {
                            "action": "network.fetch",
                            "reason": approval_reason,
                            "decision": {
                                "verdict": "require_approval",
                                "reason": "capability.require_approval.network_allowlist_missing",
                                "detail": "https://pypi.org/simple/pyjwt/",
                                "decided_by": None,  # v40-F8 additive field
                            },
                        },
                    }
                )
            ]
        )
        record = store.get_run(original.task_id)
        assert record is not None
    finally:
        store.close()

    second_task_id = ""

    def fake_run_task(*args: object, **kwargs: object) -> IngestOutcome:
        nonlocal second_task_id
        run_store = kwargs["store"]
        assert isinstance(run_store, RunStore)
        resume_of = kwargs["resume_of"]
        if resume_of == original.task_id:
            approval_verdict = kwargs["approval_verdict"]
            assert isinstance(approval_verdict, ApprovalVerdict)
            second = mint_task(
                workspace=repo,
                instructions=original.instructions,
                permissions=original.permissions,
                resume_of=original.task_id,
                approval_verdict=approval_verdict,
            )
            second_task_id = second.task_id
            second_audit_dir = config.audit_dir / second.task_id
            second_audit_dir.mkdir(parents=True, exist_ok=True)
            (second_audit_dir / "task.json").write_text(
                second.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
            run_store.create_run(second, repo=repo, ref=None, execution_mode="workspace")
            run_store.transition(second.task_id, "pending_approval")
            run_store.enqueue_approval(
                second.task_id,
                action="git.commit",
                reason="git.commit requires approval",
            )
            pending = run_store.get_run(second.task_id)
            assert pending is not None
            return IngestOutcome(record=pending, review_id=None)

        assert resume_of == second_task_id
        return IngestOutcome(
            record=RunRecord(
                task_id="completed-after-second-approval",
                trace_id="trace-completed-after-second-approval",
                repo=str(repo),
                ref=None,
                workspace=str(repo),
                execution_mode="workspace",
                instructions=original.instructions,
                state="completed",
                summary="completed after second approval",
                verification_outcome="passed",
                verification_details="ok",
                worker_version="fake",
                manifest_fingerprint="f" * 64,
                resume_of=second_task_id,
                created_at="2026-06-26T00:00:02Z",
                updated_at="2026-06-26T00:00:02Z",
            ),
            review_id=None,
        )

    choices = iter(["b", "a"])
    monkeypatch.setattr(cli_cmds, "_stdin_is_interactive", lambda: True, raising=False)
    monkeypatch.setattr(cli_cmds, "_read_single_key", lambda: next(choices), raising=False)
    monkeypatch.setattr(builtins, "input", lambda _prompt="": pytest.fail("unexpected input"))
    monkeypatch.setattr(cli_cmds, "run_task", fake_run_task)

    assert cli_cmds._prompt_inline_approval(config, record) == 0
    out = capsys.readouterr().out
    assert "approval needed: network.fetch" in out
    assert "approval needed: git.commit" in out
    assert "saved template" in out

    store = RunStore(config.db_path)
    try:
        ledger = store.ledger_for_repo(repo)
        network_entry = next(entry for entry in ledger if entry.action == "network.fetch")
        commit_entry = next(entry for entry in ledger if entry.action == "git.commit")
        assert network_entry.review_id == network_review_id
        assert network_entry.resource == "https://pypi.org/simple/pyjwt/"
        assert network_entry.remembered is True
        assert network_entry.task_outcome == "completed"
        assert commit_entry.remembered is False
        templates = store.list_templates()
        assert len(templates) == 1
        assert templates[0].provenance == "learned"
        assert templates[0].repo == str(repo)
        assert templates[0].network == ("pypi.org",)
    finally:
        store.close()


def test_remembered_drift_updates_matched_template(
    repo: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    config = build_config(home, None)
    command = ["python", "-m", "pytest"]
    approval_reason = f"shell.run requires approval for command: {shlex.join(command)}"
    task = mint_task(
        workspace=repo,
        instructions="Add an OAuth login page.",
        permissions=Permissions(
            read=["workspace"],
            write=["workspace"],
            network=["pypi.org"],
            env_allowlist=[],
        ),
    )
    audit_dir = config.audit_dir / task.task_id
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "task.json").write_text(task.model_dump_json(indent=2) + "\n", encoding="utf-8")

    store = RunStore(config.db_path)
    try:
        store.add_template(
            WorkflowTemplate(
                name="web-feature",
                instructions="Add a login page.",
                repo=str(repo),
                network=("pypi.org",),
                provenance="learned",
            )
        )
        store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
        store.transition(task.task_id, "pending_approval")
        review_id = store.enqueue_approval(task.task_id, action="shell.run", reason=approval_reason)
        store.ingest_events(
            [
                Event.model_validate(
                    {
                        "contract_version": task.contract_version,
                        "event_id": "approval-requested-drift",
                        "seq": 1,
                        "task_id": task.task_id,
                        "trace_id": task.trace_id,
                        "ts": "2026-06-26T00:00:00Z",
                        "type": "approval.requested",
                        "payload": {
                            "action": "shell.run",
                            "reason": approval_reason,
                            "decision": {
                                "verdict": "require_approval",
                                "reason": (
                                    "capability.require_approval.shell_nonverify_not_allowlisted"
                                ),
                                "detail": shlex.join(command),
                            },
                        },
                    }
                )
            ]
        )
        record = store.get_run(task.task_id)
        assert record is not None
    finally:
        store.close()

    def fake_run_task(*args: object, **kwargs: object) -> IngestOutcome:
        return IngestOutcome(
            record=RunRecord(
                task_id="resumed-drift",
                trace_id="trace-resumed-drift",
                repo=str(repo),
                ref=None,
                workspace=str(repo),
                execution_mode="workspace",
                instructions=str(kwargs["instructions"] if "instructions" in kwargs else args[1]),
                state="completed",
                summary="completed after drift approval",
                verification_outcome="passed",
                verification_details="ok",
                worker_version="fake",
                manifest_fingerprint="f" * 64,
                resume_of=task.task_id,
                created_at="2026-06-26T00:00:01Z",
                updated_at="2026-06-26T00:00:01Z",
            ),
            review_id=None,
        )

    monkeypatch.setattr(cli_cmds, "_stdin_is_interactive", lambda: True, raising=False)
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "b")
    monkeypatch.setattr(cli_cmds, "run_task", fake_run_task)

    assert cli_cmds._prompt_inline_approval(config, record) == 0
    out = capsys.readouterr().out
    assert "[b] approve + remember (update template)" in out
    assert "updated template: web-feature" in out

    store = RunStore(config.db_path)
    try:
        ledger = store.ledger_for_repo(repo)
        assert len(ledger) == 1
        assert ledger[0].review_id == review_id
        assert ledger[0].task_outcome == "completed"
        templates = store.list_templates()
        assert [template.name for template in templates] == ["web-feature"]
        assert templates[0].network == ("pypi.org",)
        assert templates[0].shell_allowlist == (("python", "-m", "pytest"),)
    finally:
        store.close()


def test_schedule_list_shows_last_outcome_for_policy_blocked_schedule(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    config = build_config(home, None)
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="project-no-auto-dispatch",
            name="trusted repo",
            strategy="trusted_local_dev",
            phase="maintain",
            policy={"default_execution_mode": "workspace"},
        )
        store.add_project_binding(
            project_id="project-no-auto-dispatch",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
        store.add_schedule(
            make_schedule(
                name="nightly-fix",
                repo=repo,
                instructions="Fix the bug. MODE:happy",
                interval_seconds=86400,
                start_at="2026-06-11T00:00:00Z",
            )
        )
        results = run_due(store=store, config=config, now="2026-06-11T09:00:00Z")
        assert len(results) == 1
        assert (
            results[0].state
            == "policy_blocked: dispatch.require_approval.project_policy_disables_auto_dispatch"
        )
    finally:
        store.close()

    assert _run_cli(home, "schedule", "list") == 0
    out = capsys.readouterr().out
    assert "last outcome" in out.splitlines()[0]
    assert "nightly-fix" in out
    assert "policy_blocked: dispatch.require_approval" in out


def test_review_unknown_task_gives_doctor_error(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    _run_cli(
        home,
        "run",
        str(repo),
        "Fix. MODE:happy",
        "--execution-mode",
        "workspace",
        "--worker-cmd",
        _worker_cmd(),
        "--quiet",
    )
    capsys.readouterr()
    code = _run_cli(home, "review", "no-such-task")
    captured = capsys.readouterr()
    assert code == 2
    assert "no run matches" in captured.err
    assert "next:" in captured.err


def test_run_rejects_non_git_target(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = _run_cli(tmp_path / "home", "run", str(tmp_path / "not-a-repo"), "x", "--quiet")
    captured = capsys.readouterr()
    assert code == 2
    assert "not a git repository" in captured.err


def test_run_without_quiet_starts_and_stops_the_tail(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without --quiet the tail thread must start and shut down cleanly.

    The fake worker finishes faster than the tail's poll interval, so phase
    lines are not guaranteed here — the deterministic rendering check is
    test_phase_tail_renders_coarse_phases below.
    """
    code = _run_cli(
        tmp_path / "home",
        "run",
        str(repo),
        "Fix the bug. MODE:happy",
        "--execution-mode",
        "workspace",
        "--worker-cmd",
        _worker_cmd(),
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "dispatching task against" in out
    assert "state:        completed" in out


def test_run_inherits_bound_project_policy_defaults(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    config = build_config(home, None)
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="project-1",
            name="trusted repo",
            strategy="trusted_local_dev",
            phase="build",
            policy={
                "default_execution_mode": "workspace",
                "default_network": ["*"],
                "allowed_shell_commands": [["pytest"]],
                "default_wall_clock_seconds": 321,
                "default_max_iterations": 7,
                "default_max_actions": 11,
                "default_max_provider_calls": 13,
            },
        )
        store.add_project_binding(
            project_id="project-1",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
    finally:
        store.close()

    code = _run_cli(
        home,
        "run",
        str(repo),
        "Fix the bug. MODE:happy",
        "--worker-cmd",
        _worker_cmd(),
        "--quiet",
    )
    out = capsys.readouterr().out
    assert code == 0, out

    task_id = _only_task_id(home)
    store = RunStore(config.db_path)
    try:
        run = store.get_run(task_id)
        assert run is not None
        assert run.execution_mode == "workspace"
    finally:
        store.close()

    task = json.loads((config.audit_dir / task_id / "task.json").read_text())
    assert task["permissions"]["network"] == ["*"]
    assert task["permissions"]["shell_allowlist"] == [["pytest"]]
    assert task["budget"] == {
        "wall_clock_seconds": 321,
        "max_iterations": 7,
        "max_actions": 11,
        "max_provider_calls": 13,
    }


def test_run_inherits_bound_project_auto_apply_policy(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    config = build_config(home, None)
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="project-auto-apply",
            name="trusted repo",
            strategy="trusted_local_dev",
            phase="maintain",
            policy={
                "default_execution_mode": "workspace",
                "auto_apply_verified_patch": True,
                # v90-F4: maintain auto-lands only on a pinned verify command.
                "verify_command": 'grep -q "value = 1" existing.py',
            },
        )
        store.add_project_binding(
            project_id="project-auto-apply",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
    finally:
        store.close()

    code = _run_cli(
        home,
        "run",
        str(repo),
        "Fix the bug. MODE:happy",
        "--worker-cmd",
        _worker_cmd(),
        "--quiet",
    )
    out = capsys.readouterr().out
    assert code == 0, out

    task_id = _only_task_id(home)
    assert git(repo, "rev-parse", "--verify", f"refs/heads/skep/{task_id}")
    task = json.loads((config.audit_dir / task_id / "task.json").read_text())
    assert task["auto_apply_verified_patch"] is True


def test_run_inherits_bound_project_phase_auto_apply_default(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    config = build_config(home, None)
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="project-phase-maintain",
            name="trusted repo",
            strategy="trusted_local_dev",
            phase="maintain",
            policy={
                "default_execution_mode": "workspace",
                # v90-F4: maintain auto-lands only on a pinned verify command.
                "verify_command": 'grep -q "value = 1" existing.py',
            },
        )
        store.add_project_binding(
            project_id="project-phase-maintain",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
    finally:
        store.close()

    code = _run_cli(
        home,
        "run",
        str(repo),
        "Fix the bug. MODE:happy",
        "--worker-cmd",
        _worker_cmd(),
        "--quiet",
    )
    out = capsys.readouterr().out
    assert code == 0, out

    task_id = _only_task_id(home)
    assert git(repo, "rev-parse", "--verify", f"refs/heads/skep/{task_id}")
    task = json.loads((config.audit_dir / task_id / "task.json").read_text())
    assert task["auto_apply_verified_patch"] is True
    assert task["project_context"] == {
        "project_id": "project-phase-maintain",
        "name": "trusted repo",
        "strategy": "trusted_local_dev",
        "phase": "maintain",
        "binding_kind": "repo_path",
        "binding_value": str(repo),
    }
    dispatch_decision = _project_dispatch_decision(
        reason="dispatch.allow.run_request_resolved",
        project_id="project-phase-maintain",
        phase="maintain",
    )
    assert task["dispatch_decision"] == dispatch_decision
    assert task["landing_decision"] == {
        "verdict": "allow",
        "reason": "landing.auto_apply.project_policy_enabled",
        "detail": None,
        "decided_by": None,  # v40-F8 additive field
    }
    store = RunStore(config.db_path)
    try:
        transitions = store.transitions_for(task_id)
    finally:
        store.close()
    assert json.loads(str(transitions[0][1])) == {
        "project_context": {
            "project_id": "project-phase-maintain",
            "name": "trusted repo",
            "strategy": "trusted_local_dev",
            "phase": "maintain",
            "binding_kind": "repo_path",
            "binding_value": str(repo),
        },
        "dispatch_decision": dispatch_decision,
        "landing_decision": {
            "verdict": "allow",
            "reason": "landing.auto_apply.project_policy_enabled",
            "detail": None,
            "decided_by": None,  # v40-F8 additive field
        },
    }

    assert _run_cli(home, "review", task_id) == 0
    review_out = capsys.readouterr().out
    assert "project:      project-phase-maintain (trusted_local_dev/maintain)" in review_out
    assert "dispatch:     allow dispatch.allow.run_request_resolved" in review_out
    assert "landing:      allow landing.auto_apply.project_policy_enabled" in review_out


def test_status_personal_shows_bound_project_and_autonomy_columns(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    store = RunStore(build_config(home, None).db_path)
    try:
        store.add_project_policy(
            project_id="project-phase-maintain",
            name="trusted repo",
            strategy="trusted_local_dev",
            phase="maintain",
            policy={"default_execution_mode": "workspace"},
        )
        store.add_project_binding(
            project_id="project-phase-maintain",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
    finally:
        store.close()

    code = _run_cli(
        home,
        "run",
        str(repo),
        "Fix the bug. MODE:happy",
        "--worker-cmd",
        _worker_cmd(),
        "--quiet",
    )
    run_out = capsys.readouterr().out
    assert code == 0, run_out

    assert _run_cli(home, "status", "--personal") == 0
    status_out = capsys.readouterr().out
    assert "project" in status_out.splitlines()[0]
    assert "autonomy" in status_out.splitlines()[0]
    assert "project-phase-maintain (maintain)" in status_out
    assert "d:allow allow.run_request_resolved" in status_out
    assert "l:allow auto_apply.project_policy_enabled" in status_out


def test_cli_run_records_project_policy_dispatch_reason_when_auto_dispatch_matches(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    config = build_config(home, None)
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="project-auto-dispatch",
            name="trusted repo",
            strategy="trusted_local_dev",
            phase="build",
            policy={
                "default_execution_mode": "workspace",
                "auto_dispatch_allowed": True,
            },
        )
        store.add_project_binding(
            project_id="project-auto-dispatch",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
    finally:
        store.close()

    code = _run_cli(
        home,
        "run",
        str(repo),
        "Fix the bug. MODE:happy",
        "--worker-cmd",
        _worker_cmd(),
        "--quiet",
    )
    out = capsys.readouterr().out
    assert code == 0, out

    task_id = _only_task_id(home)
    task = json.loads((config.audit_dir / task_id / "task.json").read_text())
    dispatch_decision = _project_dispatch_decision(
        reason="dispatch.auto_allowed.project_policy_match",
        project_id="project-auto-dispatch",
        phase="build",
    )
    assert task["dispatch_decision"] == dispatch_decision
    store = RunStore(config.db_path)
    try:
        transitions = store.transitions_for(task_id)
    finally:
        store.close()
    assert json.loads(str(transitions[0][1]))["dispatch_decision"] == dispatch_decision

    assert _run_cli(home, "status", "--personal") == 0
    status_out = capsys.readouterr().out
    assert "d:allow auto_allowed.project_policy_match" in status_out


def test_project_cli_preview_setup_show_list_and_set_phase(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"

    code = _run_cli(
        home,
        "project",
        "preview",
        str(repo),
        "--pack",
        "public_free",
        "--phase",
        "build",
        "--project-id",
        "free-project",
        "--name",
        "Free Project",
    )
    preview_out = capsys.readouterr().out
    assert code == 0, preview_out
    assert "preview: free-project (public_free/build)" in preview_out
    assert "pack: public_free@1" in preview_out
    assert "warnings: auto_dispatch_allowed" in preview_out
    assert "free-project-public-free-deps" in preview_out

    config = build_config(home, None)
    store = RunStore(config.db_path)
    try:
        assert store.get_project_policy("free-project") is None
    finally:
        store.close()

    code = _run_cli(
        home,
        "project",
        "setup",
        str(repo),
        "--pack",
        "public_free",
        "--phase",
        "build",
        "--project-id",
        "free-project",
        "--name",
        "Free Project",
    )
    setup_out = capsys.readouterr().out
    assert code == 0, setup_out
    assert "saved: free-project (public_free/build)" in setup_out
    assert "seeded templates: 4" in setup_out
    assert "seeded schedules: 3" in setup_out

    store = RunStore(config.db_path)
    try:
        policy = store.get_project_policy("free-project")
        assert policy is not None
        assert policy.pack_name == "public_free"
        assert policy.pack_version == "1"
        assert policy.policy["auto_apply_verified_patch"] is False
        template_names = {template.name for template in store.list_templates()}
        assert {
            "free-project-public-free-deps",
            "free-project-public-free-docs",
        }.issubset(template_names)
        schedule_names = {schedule.name for schedule in store.list_schedules()}
        assert {
            "free-project-public-free-deps-weekly",
            "free-project-public-free-docs-weekly",
        }.issubset(schedule_names)
    finally:
        store.close()

    assert _run_cli(home, "project", "list") == 0
    list_out = capsys.readouterr().out
    assert "free-project" in list_out
    assert "public_free" in list_out
    assert "public_free@1" in list_out

    assert _run_cli(home, "project", "show", "free-project") == 0
    show_out = capsys.readouterr().out
    assert "project free-project" in show_out
    assert "pack:      public_free@1" in show_out
    assert '"default_network": []' in show_out

    assert _run_cli(home, "project", "set-phase", "free-project", "maintain") == 0
    phase_out = capsys.readouterr().out
    assert "phase updated: free-project -> maintain" in phase_out

    store = RunStore(config.db_path)
    try:
        updated = store.get_project_policy("free-project")
        assert updated is not None
        assert updated.phase == "maintain"
        assert updated.policy["auto_apply_verified_patch"] is True
    finally:
        store.close()


def test_project_setup_engine_flag_saves_and_validates(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """v94-F5: coding_engine joined the policy keys in v90 but no operator
    surface ever wrote it — the only writers in the tree were tests, and the
    2026-07-26 field test had to edit the store by hand. --engine is that
    surface, validated at setup time so a typo fails naming the choices (I9)."""
    home = tmp_path / "home"

    code = _run_cli(
        home,
        "project",
        "setup",
        str(repo),
        "--strategy",
        "trusted_local_dev",
        "--project-id",
        "engine-project",
        "--engine",
        "claude_code",
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "coding engine: claude_code" in out

    config = build_config(home, None)
    store = RunStore(config.db_path)
    try:
        saved = store.get_project_policy("engine-project")
        assert saved is not None
        assert saved.policy["coding_engine"] == "claude_code"
    finally:
        store.close()

    code = _run_cli(
        home,
        "project",
        "setup",
        str(repo),
        "--strategy",
        "trusted_local_dev",
        "--project-id",
        "engine-project",
        "--engine",
        "warp-drive",
    )
    err = capsys.readouterr().err
    assert code == 2
    assert "warp-drive" in err
    assert "builtin" in err  # the refusal names the valid choices


def test_project_setup_verify_command_flag_pins_the_gate(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """v100-F9: v94-F5 gave the CLI --engine but nothing to pin verify_command
    with, and policy_resolver.py:543 refuses to dispatch a CLI engine without
    one. So `project setup --engine claude_code` on a repo whose entry point
    v91-F1 cannot infer built a project that could never run, and the refusal
    named a way forward no CLI or REST surface had (I9). Found by v100's own
    field-test acceptance, on the operator's real skep-benchmarks project."""
    home = tmp_path / "home"

    code = _run_cli(
        home,
        "project",
        "setup",
        str(repo),
        "--strategy",
        "trusted_local_dev",
        "--project-id",
        "pinned-project",
        "--engine",
        "claude_code",
        "--verify-command",
        "python3 verify_plan.py",
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "verify command: python3 verify_plan.py" in out
    assert "coding engine: claude_code" in out

    config = build_config(home, None)
    store = RunStore(config.db_path)
    try:
        saved = store.get_project_policy("pinned-project")
        assert saved is not None
        assert saved.policy["verify_command"] == "python3 verify_plan.py"
        assert saved.policy["coding_engine"] == "claude_code"
    finally:
        store.close()


def test_repeated_setup_updates_and_never_re_installs(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """v100-F10: v24-F4 established that a repeated setup is a policy update,
    not a re-install — for seeded templates only. The project's own policy and
    bindings were still re-installed: bindings wiped and re-added, policy
    rebuilt from phase defaults that cover four keys. So changing one flag
    silently destroyed every other key the operator had set. v100's acceptance
    lost a live `verify_command` pin and a `repo_slug` binding to this."""
    home = tmp_path / "home"
    config = build_config(home, None)

    code = _run_cli(
        home,
        "project",
        "setup",
        str(repo),
        "--strategy",
        "trusted_local_dev",
        "--project-id",
        "carry-project",
        "--phase",
        "maintain",
        "--verify-command",
        "python3 verify_plan.py",
    )
    assert code == 0, capsys.readouterr().out
    capsys.readouterr()

    store = RunStore(config.db_path)
    try:  # a binding of another kind, as the field project had
        store.add_project_binding(
            project_id="carry-project", binding_kind="repo_slug", binding_value="carry-repo"
        )
    finally:
        store.close()

    # Change ONE unrelated flag, exactly as the acceptance did.
    code = _run_cli(
        home,
        "project",
        "setup",
        str(repo),
        "--strategy",
        "trusted_local_dev",
        "--project-id",
        "carry-project",
        "--phase",
        "maintain",
        "--engine",
        "claude_code",
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "verify command: python3 verify_plan.py" in out  # not "none detected"

    store = RunStore(config.db_path)
    try:
        saved = store.get_project_policy("carry-project")
        assert saved is not None
        assert saved.policy["verify_command"] == "python3 verify_plan.py"  # survived
        assert saved.policy["coding_engine"] == "claude_code"  # and the new flag applied
        kinds = {b.binding_kind for b in store.project_bindings("carry-project")}
        assert kinds == {"repo_path", "repo_slug"}  # the other kind survived
    finally:
        store.close()

    # A phase change still moves the trust flags the phase defaults OWN — the
    # carry-forward preserves operator keys, it does not freeze policy.
    code = _run_cli(
        home,
        "project",
        "setup",
        str(repo),
        "--strategy",
        "trusted_local_dev",
        "--project-id",
        "carry-project",
        "--phase",
        "build",
    )
    assert code == 0, capsys.readouterr().out
    store = RunStore(config.db_path)
    try:
        saved = store.get_project_policy("carry-project")
        assert saved is not None
        assert saved.policy["auto_apply_verified_patch"] is False  # build's default wins
        assert saved.policy["verify_command"] == "python3 verify_plan.py"  # still kept
    finally:
        store.close()


def test_preview_summary_prints_the_seeded_verify_pin(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """v94-F6: the preview printer read result['policy'], a key the preview
    payload never had — so it said 'none detected' for the very repo whose pin
    it had just inferred (v91-F1's own I8 rule broken on its own surface)."""
    from skep.supervisor.cli_cmds import _print_project_setup_summary

    preview_result = {
        "project": {
            "project_id": "p",
            "strategy": "trusted_local_dev",
            "phase": "build",
            "policy": {"verify_command": "uv run pytest", "coding_engine": "claude_code"},
            "bindings": [],
        },
        "effective_policy": {"verify_command": "uv run pytest"},
    }
    _print_project_setup_summary(preview_result, preview=True)
    out = capsys.readouterr().out
    assert "verify command: uv run pytest" in out
    assert "none detected" not in out
    assert "coding engine: claude_code" in out


def test_phase_tail_renders_coarse_phases(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from skep.supervisor.cli_cmds import _PhaseTail, build_config

    config = build_config(tmp_path / "home", None)
    events_dir = config.worktrees_root / "task-1" / ".events"
    events_dir.mkdir(parents=True)
    lines = [
        {
            "type": "task.start",
            "payload": {
                "worker_version": "0.2.0",
                "project_context": {
                    "project_id": "project-1",
                    "name": "trusted repo",
                    "strategy": "trusted_local_dev",
                    "phase": "maintain",
                    "binding_kind": "repo_path",
                    "binding_value": "/tmp/repo",
                },
                "dispatch_decision": {
                    "verdict": "allow",
                    "reason": "dispatch.auto_allowed.project_policy_match",
                    "detail": None,
                    "decided_by": None,  # v40-F8 additive field
                },
                "landing_decision": {
                    "verdict": "allow",
                    "reason": "landing.auto_apply.project_policy_enabled",
                    "detail": None,
                    "decided_by": None,  # v40-F8 additive field
                },
            },
        },
        {"type": "plan.created", "payload": {"steps": ["iteration 1: 2 action(s) planned"]}},
        {
            "type": "command.start",
            "payload": {
                "command": "pytest -q",
                "purpose": "verify",
                "decision": {
                    "verdict": "allow",
                    "reason": "capability.allow.shell_verify",
                    "detail": "pytest -q",
                    "decided_by": None,  # v40-F8 additive field
                },
            },
        },
        {
            "type": "approval.requested",
            "payload": {
                "action": "shell.run",
                "reason": "shell.run requires approval",
                "decision": {
                    "verdict": "require_approval",
                    "reason": "capability.require_approval.shell_nonverify_not_allowlisted",
                    "detail": "python write.py",
                    "decided_by": None,  # v40-F8 additive field
                },
            },
        },
        {"type": "verify.result", "payload": {"outcome": "passed", "details": "ok"}},
        {"type": "task.terminal", "payload": {"status": "completed", "summary": "done"}},
    ]
    (events_dir / "task-1.ndjson").write_text("\n".join(json.dumps(line) for line in lines) + "\n")

    tail = _PhaseTail(config)
    tail._drain()  # deterministic single drain; no thread, no race
    out = capsys.readouterr().out
    assert "worker started (v0.2.0)  project: project-1 (trusted_local_dev/maintain)" in out
    assert "dispatch: allow dispatch.auto_allowed.project_policy_match" in out
    assert "landing: allow landing.auto_apply.project_policy_enabled" in out
    assert "plan: iteration 1: 2 action(s) planned" in out
    assert "run: pytest -q  policy: allow capability.allow.shell_verify (pytest -q)" in out
    assert "approval needed: shell.run" in out
    assert (
        "policy: require_approval capability.require_approval.shell_nonverify_not_allowlisted "
        "(python write.py)"
    ) in out
    assert "verification: passed" in out
    assert "terminal: completed" in out


def test_phase_tail_renders_supervisor_approval_and_reverify_events(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from skep.supervisor.cli_cmds import _PhaseTail, build_config

    config = build_config(tmp_path / "home", None)
    config.worktrees_root.mkdir(parents=True, exist_ok=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = config.worktrees_root / "task-1"
    workspace.mkdir(parents=True, exist_ok=True)

    task = mint_task(
        workspace=workspace,
        instructions="Fix the bug.",
        project_context=ProjectContextPayload(
            project_id="project-1",
            name="trusted repo",
            strategy="trusted_local_dev",
            phase="maintain",
            binding_kind="repo_path",
            binding_value=str(repo),
        ),
        landing_decision=AutonomyDecisionPayload(
            verdict="require_approval",
            reason="landing.require_approval.no_auto_apply_rule",
            detail=None,
        ),
    )

    store = RunStore(config.db_path)
    try:
        store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
        review_id = store.enqueue_approval(
            task.task_id,
            action="apply_patch",
            reason="patch application review",
        )
        store.resolve_approval(review_id, approved=True, actor="tester", note="manual ok")
        store.record_reverification(
            task.task_id,
            outcome="passed",
            worker_outcome="passed",
            confirmed=True,
            commands=["pytest -q"],
            exit_codes=[0],
            detail="re-ran clean: all exit 0",
        )
    finally:
        store.close()

    tail = _PhaseTail(config)
    tail._drain()
    out = capsys.readouterr().out
    assert (
        "approval needed: apply_patch  project: project-1 (trusted_local_dev/maintain)  "
        "patch application review  "
        "policy: require_approval "
        "landing.require_approval.no_auto_apply_rule"
    ) in out
    assert (
        f"approval resolved: apply_patch approved by tester skep/{task.task_id}  "
        "project: project-1 (trusted_local_dev/maintain)  "
        "policy: require_approval "
        "landing.require_approval.no_auto_apply_rule  manual ok"
    ) in out
    assert "re-verify: passed [confirmed]  worker passed  re-ran clean: all exit 0" in out
    assert "re-ran pytest -q  -> exit 0" in out


def test_phase_tail_renders_a_patchless_reverify_as_benign(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """v65-F2: a not_applicable reverify shows no NOT CONFIRMED alarm and
    never claims a re-run that did not happen."""
    from skep.supervisor.cli_cmds import _PhaseTail, build_config

    config = build_config(tmp_path / "home", None)
    config.worktrees_root.mkdir(parents=True, exist_ok=True)
    workspace = config.worktrees_root / "task-1"
    workspace.mkdir(parents=True, exist_ok=True)
    task = mint_task(workspace=workspace, instructions="Audit the deps.")

    store = RunStore(config.db_path)
    try:
        store.create_run(task, repo=tmp_path / "repo", ref=None, execution_mode="workspace")
        store.record_reverification(
            task.task_id,
            outcome="not_applicable",
            worker_outcome="passed",
            confirmed=False,
            commands=["python3 -m pytest -q"],
            exit_codes=[],
            detail="run changed no files — no patch to re-verify",
        )
    finally:
        store.close()

    tail = _PhaseTail(config)
    tail._drain()
    out = capsys.readouterr().out
    assert "re-verify: nothing to re-verify" in out
    assert "run changed no files" in out
    assert "recorded verify: python3 -m pytest -q" in out
    assert "NOT CONFIRMED" not in out
    assert "re-ran" not in out


# ---------- v14 Step 8: schedule + provider health CLI views ----------


def test_schedule_health_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from skep.supervisor.cli_cmds import build_config
    from skep.supervisor.scheduler import make_schedule
    from skep.supervisor.store import RunStore

    home = tmp_path / "home"
    store = RunStore(build_config(home, None).db_path)
    try:
        store.add_schedule(
            make_schedule(
                name="nightly",
                repo=tmp_path / "repo",
                instructions="x",
                interval_seconds=86400,
                worker_kind="audit",
                start_at="2026-06-11T00:00:00Z",
            )
        )
        store.record_schedule_outcome("nightly", task_id="t1", state="completed")
    finally:
        store.close()

    assert _run_cli(home, "schedule", "health") == 0
    out = capsys.readouterr().out
    assert "nightly" in out and "100%" in out


def test_provider_list_and_health_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from skep.supervisor.cli_cmds import build_config
    from skep.supervisor.providers import ProviderHealth, ProviderProfile
    from skep.supervisor.store import RunStore

    home = tmp_path / "home"
    store = RunStore(build_config(home, None).db_path)
    try:
        store.upsert_provider_profile(
            ProviderProfile(
                provider_id="local",
                protocol="ollama",
                base_url="http://localhost:11434",
                model="qwen3",
                active=True,
            )
        )
        store.record_provider_health(
            ProviderHealth(
                provider_id="local",
                reachable=True,
                model_found=True,
                latency_ms=5,
                error=None,
                checked_at="2026-07-08T00:00:00Z",
            )
        )
    finally:
        store.close()

    assert _run_cli(home, "provider", "list") == 0
    assert "local" in capsys.readouterr().out
    assert _run_cli(home, "provider", "health") == 0
    health_out = capsys.readouterr().out
    assert "local" in health_out and "5ms" in health_out


def _bindings(home: Path, project_id: str) -> set[tuple[str, str]]:
    store = RunStore(build_config(home, None).db_path)
    try:
        policy = store.get_project_policy(project_id)
        assert policy is not None
        return {(b.binding_kind, b.binding_value) for b in store.project_bindings(project_id)}
    finally:
        store.close()


def test_project_setup_writes_the_slug_binding_for_a_managed_clone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """v101-F13: PROJECT_BINDING_KINDS has three members; the chat tool wrote
    two, the REST route all three, the CLI exactly one — `repo_slug=None`, a
    literal. So the Queen, on a small model, had a strictly larger authority
    surface than the human typing commands (I5).

    A managed clone's path is DERIVED (<home>/repos/<slug>); the slug is the
    identity. The scheduler resolves ticks by `("repo_slug", repo.name)`, so a
    project with only a path binding loses its project on a tick and runs on
    global defaults — silently, which is worse than breaking (v23-F3)."""
    home = tmp_path / "home"
    managed = home / "repos" / "widget-svc"
    managed.mkdir(parents=True)
    git(managed, "init", "-b", "main")
    (managed / "README.md").write_text("hi\n")
    git(managed, "add", "-A")
    git(managed, "-c", "user.email=t@e", "-c", "user.name=T", "commit", "-m", "init")

    code = _run_cli(
        home, "project", "setup", str(managed), "--pack", "public_free", "--project-id", "widget"
    )
    out = capsys.readouterr().out
    assert code == 0, out

    assert _bindings(home, "widget") == {
        ("repo_slug", "widget-svc"),
        ("repo_path", str(managed)),
    }
    # v94-F6: the surface that performs the write reports the write.
    assert "repo_slug=widget-svc" in out
    assert f"repo_path={managed}" in out


def test_project_setup_invents_no_slug_for_a_directory_outside_the_root(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A /workon directory has no slug, and inferring one would be a name for a
    thing that does not have it — _validate_project_binding would reject it.
    Inference, not a --repo-slug flag: for a managed clone register_repo clones
    to root/slug, so the directory name IS the slug and the flag would have
    exactly one correct value."""
    home = tmp_path / "home"
    code = _run_cli(
        home, "project", "setup", str(repo), "--pack", "public_free", "--project-id", "workon-proj"
    )
    out = capsys.readouterr().out
    assert code == 0, out

    assert _bindings(home, "workon-proj") == {("repo_path", str(repo))}
    assert "repo_slug=" not in out


def test_project_preview_reports_the_bindings_setup_will_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Preview that under-reports what setup does is the reason v100's own
    acceptance lost a binding without noticing (I8)."""
    home = tmp_path / "home"
    managed = home / "repos" / "preview-svc"
    managed.mkdir(parents=True)
    git(managed, "init", "-b", "main")
    (managed / "README.md").write_text("hi\n")
    git(managed, "add", "-A")
    git(managed, "-c", "user.email=t@e", "-c", "user.name=T", "commit", "-m", "init")

    assert (
        _run_cli(
            home, "project", "preview", str(managed), "--pack", "public_free", "--project-id", "pv"
        )
        == 0
    )
    preview_out = capsys.readouterr().out
    assert (
        _run_cli(
            home, "project", "setup", str(managed), "--pack", "public_free", "--project-id", "pv"
        )
        == 0
    )
    setup_out = capsys.readouterr().out

    def bindings_line(text: str) -> str:
        return next(line for line in text.splitlines() if "bindings:" in line)

    assert bindings_line(preview_out) == bindings_line(setup_out)
    assert "repo_slug=preview-svc" in bindings_line(preview_out)


def test_the_v100_sequence_ends_with_both_bindings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The exact failure v100's acceptance hit on the live skep-benchmarks
    project: set up by slug, re-set up by path, and the slug binding was gone
    with no verb able to put it back. v100-F10 stopped setup DELETING it; F13
    lets setup CREATE it, which is what made the state unfixable from the
    surface that caused it (I9)."""
    home = tmp_path / "home"
    managed = home / "repos" / "bench"
    managed.mkdir(parents=True)
    git(managed, "init", "-b", "main")
    (managed / "README.md").write_text("hi\n")
    git(managed, "add", "-A")
    git(managed, "-c", "user.email=t@e", "-c", "user.name=T", "commit", "-m", "init")

    assert (
        _run_cli(
            home, "project", "setup", str(managed), "--pack", "public_free", "--project-id", "bench"
        )
        == 0
    )
    capsys.readouterr()
    # The re-run — a different flag, the same repo, through the resolved path.
    assert (
        _run_cli(
            home,
            "project",
            "setup",
            str(managed),
            "--pack",
            "public_free",
            "--project-id",
            "bench",
            "--phase",
            "maintain",
        )
        == 0
    )
    assert "saved: bench" in capsys.readouterr().out

    assert _bindings(home, "bench") == {
        ("repo_slug", "bench"),
        ("repo_path", str(managed)),
    }


def test_doctor_names_a_verify_pin_this_host_cannot_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v101-F14: `_verify_pin_advisories` warned about projects pinning
    NOTHING; the inverse — a pin whose binary is missing — had no warning,
    which is why run 019faa33 (`make test`, exit 127, on a host with no make)
    sat unnoticed through v100. G10 could never confirm on that project, and
    the doctor said nothing.

    The two advisories are independent: one project can be unpinned while
    another is pinned to something unrunnable, and both must be named."""
    from skep.status import build_status

    home = tmp_path / "personal"
    db_path = home / "supervisor" / "supervisor.sqlite3"
    db_path.parent.mkdir(parents=True)
    store = RunStore(db_path)
    try:
        store.add_project_policy(
            project_id="makefile-proj",
            name="Makefile",
            strategy="trusted_local_dev",
            phase="build",
            policy={"verify_command": "make test"},
        )
        store.add_project_policy(
            project_id="unpinned-proj",
            name="Unpinned",
            strategy="trusted_local_dev",
            phase="build",
            policy={},
        )
    finally:
        store.close()

    monkeypatch.setattr("skep.status.shutil.which", lambda _: None)
    advisories = build_status(home)["advisories"]

    unrunnable = [a for a in advisories if "can never confirm" in a]
    assert len(unrunnable) == 1
    # Names the project AND the command, and the way forward (I9).
    assert "makefile-proj (make test)" in unrunnable[0]
    assert "--verify-command" in unrunnable[0]
    # `unavailable`, not `failed` — the outcome the operator will actually see.
    assert "unavailable" in unrunnable[0]

    # The v91-F1 advisory still fires, unchanged and separately.
    unpinned = [a for a in advisories if "pin no verify_command" in a]
    assert len(unpinned) == 1
    assert "unpinned-proj" in unpinned[0] and "makefile-proj" not in unpinned[0]

    # Binary present: silence. A runnable pin is not a finding.
    monkeypatch.setattr("skep.status.shutil.which", lambda name: f"/usr/bin/{name}")
    assert not [a for a in build_status(home)["advisories"] if "can never confirm" in a]


# ---------------------------------------------------------------------------
# v104-F2/F3/F4 — the operator's half of the git surface.
#
# `skep --help` listed 19 command groups and none of them touched a branch or
# a pull request, so the human typing commands had a strictly narrower
# authority surface than the small model in the chat box (I5). The v103 field
# test is the evidence: consolidating three branches needed a `uv run python
# -c` one-liner, and the PR was opened with a raw `gh pr create`.
# ---------------------------------------------------------------------------


def _registered_clone(home: Path, repo: Path, name: str = "fixture") -> Path:
    """Register `repo` and return the managed clone the verbs operate on."""
    from skep.supervisor.cli_cmds import _project_root
    from skep.supervisor.serve.registry import register_repo

    config = build_config(home, None)
    root = _project_root(config)
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    register_repo(root, url=str(repo), name=name)
    return root / name


def _commit_on(clone: Path, branch: str, filename: str, body: str) -> None:
    git(clone, "branch", branch)
    work = clone.parent / f"wt-{branch.replace('/', '-')}"
    git(clone, "worktree", "add", str(work), branch)
    (work / filename).write_text(body, encoding="utf-8")
    git(work, "add", "-A")
    git(work, "-c", "user.email=t@e", "-c", "user.name=T", "commit", "-m", f"on {branch}")
    git(clone, "worktree", "remove", "--force", str(work))


def test_skep_branch_consolidates_task_branches(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The v103 consolidation, redone entirely through the CLI — the
    acceptance the plan named."""
    home = tmp_path / "home"
    clone = _registered_clone(home, repo)
    capsys.readouterr()

    _commit_on(clone, "skep/task-a", "a.txt", "from a\n")
    _commit_on(clone, "skep/task-b", "b.txt", "from b\n")

    assert _run_cli(home, "branch", "create", "fixture", "skep/integration") == 0
    assert (
        _run_cli(
            home,
            "branch",
            "merge",
            "fixture",
            "--source",
            "skep/task-a",
            "--into",
            "skep/integration",
        )
        == 0
    )
    assert (
        _run_cli(
            home,
            "branch",
            "merge",
            "fixture",
            "--source",
            "skep/task-b",
            "--into",
            "skep/integration",
        )
        == 0
    )
    capsys.readouterr()

    tree = git(clone, "ls-tree", "--name-only", "skep/integration").stdout.split()
    assert "a.txt" in tree and "b.txt" in tree

    assert _run_cli(home, "branch", "list", "fixture") == 0
    listing = capsys.readouterr().out
    assert "skep/integration" in listing
    assert "*" in listing  # the default branch is marked


def test_skep_branch_reports_the_actions_own_refusal(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """I9 + I5: the refusal text is the action's, not a CLI paraphrase. A
    second copy of "never the default branch" in the CLI would be a shadow
    permission system that eventually disagrees with the chat verb."""
    home = tmp_path / "home"
    clone = _registered_clone(home, repo)
    default = git(clone, "symbolic-ref", "--short", "HEAD").stdout.strip()
    _commit_on(clone, "skep/task-a", "a.txt", "from a\n")
    capsys.readouterr()

    assert (
        _run_cli(home, "branch", "merge", "fixture", "--source", "skep/task-a", "--into", default)
        != 0
    )
    assert "merge_pr" in capsys.readouterr().err

    assert _run_cli(home, "branch", "push", "fixture", default) != 0
    assert "default branch" in capsys.readouterr().err

    # A conflict exits non-zero, names the file, and moves nothing.
    _commit_on(clone, "skep/left", "same.txt", "left\n")
    _commit_on(clone, "skep/right", "same.txt", "right\n")
    before = git(clone, "rev-parse", "skep/right").stdout.strip()
    assert (
        _run_cli(
            home, "branch", "merge", "fixture", "--source", "skep/left", "--into", "skep/right"
        )
        != 0
    )
    assert "same.txt" in capsys.readouterr().err
    assert git(clone, "rev-parse", "skep/right").stdout.strip() == before


def test_skep_pr_open_refuses_the_default_branch(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The git-side guard fires before gh is ever invoked, so this needs no
    network and no credentials."""
    home = tmp_path / "home"
    clone = _registered_clone(home, repo)
    default = git(clone, "symbolic-ref", "--short", "HEAD").stdout.strip()
    capsys.readouterr()

    assert _run_cli(home, "pr", "open", "fixture", "--branch", default, "--base", default) != 0
    assert "default" in capsys.readouterr().err.lower()


def test_skep_repo_refresh_fast_forwards_the_clone(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The prerequisite for every recipe in the git-and-github skill, and the
    one verb that had a REST route and a chat tool and no CLI."""
    home = tmp_path / "home"
    clone = _registered_clone(home, repo)
    default = git(clone, "symbolic-ref", "--short", "HEAD").stdout.strip()
    capsys.readouterr()

    # origin moves on.
    (repo / "later.txt").write_text("later\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=t@e", "-c", "user.name=T", "commit", "-m", "origin moves")

    assert _run_cli(home, "repo", "refresh", "fixture") == 0
    assert "fetched: True" in capsys.readouterr().out
    assert "later.txt" in git(clone, "ls-tree", "--name-only", f"origin/{default}").stdout


def test_the_new_groups_are_registered() -> None:
    """The parser tree is hand-built, so a group that exists as handlers but
    was never hung on `subcommands` is invisible — exactly how these verbs
    stayed chat-only for so long."""
    import argparse
    import contextlib
    import io

    from skep.cli import main

    for group in ("branch", "pr", "repo"):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), pytest.raises(SystemExit) as exit_code:
            main([group, "--help"])
        assert exit_code.value.code == 0, group
        assert "usage: skep " + group in out.getvalue()
    assert argparse  # imported for the reader: this is the argparse tree
