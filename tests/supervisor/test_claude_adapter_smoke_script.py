from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_release_checklist_points_to_claude_adapter_smoke_script() -> None:
    script = ROOT / "scripts" / "claude-adapter-smoke.sh"
    checklist = (ROOT / "docs" / "release-checklist.md").read_text(encoding="utf-8")

    assert script.is_file()
    assert "scripts/claude-adapter-smoke.sh" in checklist


def test_claude_adapter_smoke_script_runs_disposable_workspace() -> None:
    script = (ROOT / "scripts" / "claude-adapter-smoke.sh").read_text(encoding="utf-8")

    assert "mktemp -d" in script
    assert "SKEP_CLAUDE_CODE_CMD" in script
    assert "CLAUDE_WORKER_CMD=" in script
    assert ('uv --project $(shell_quote "$ROOT") run python -m skep.workers.claude_code') in script
    assert '--worker-cmd "$CLAUDE_WORKER_CMD"' in script
    assert '--worker-cmd "python -m skep.workers.claude_code"' not in script
    assert "--env-allow SKEP_CLAUDE_CODE_CMD" in script
    assert "--execution-mode workspace" in script
    assert "claude_smoke.txt" in script
    assert "claude-code-adapter-0.1.0" in script


def test_claude_adapter_smoke_script_preflights_login_before_skep_run() -> None:
    script = (ROOT / "scripts" / "claude-adapter-smoke.sh").read_text(encoding="utf-8")

    preflight = "CLAUDE_LOGIN_OUTPUT="
    status = "CLAUDE_LOGIN_STATUS="
    print_probe = "CLAUDE_PRINT_PROBE_OUTPUT="
    skep_run = 'env SKEP_CLAUDE_CODE_CMD="$CLAUDE_CMD"'
    assert preflight in script
    assert status in script
    assert print_probe in script
    assert script.index(preflight) < script.index(skep_run)
    assert script.index(print_probe) < script.index(skep_run)
    assert "Not logged in" in script
    assert "claude auth login" in script


def test_claude_adapter_smoke_script_exits_before_dispatch_when_not_logged_in(
    tmp_path: Path,
) -> None:
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "auth" && "$2" == "status" ]]; then\n'
        '  printf \'{"loggedIn": false, "authMethod": "none"}\\n\'\n'
        "  exit 1\n"
        "fi\n"
        'echo unexpected claude invocation: "$@" >&2\n'
        "exit 99\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)

    env = {
        **os.environ,
        "SKEP_CLAUDE_CODE_CMD": str(fake_claude),
        "SKEP_CLAUDE_SMOKE_KEEP": "0",
    }
    result = subprocess.run(
        [str(ROOT / "scripts" / "claude-adapter-smoke.sh")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 2
    assert "Not logged in" in result.stderr
    assert "claude auth login" in result.stderr
    assert "task " not in result.stdout


def test_claude_adapter_smoke_script_exits_when_print_mode_cannot_use_auth(
    tmp_path: Path,
) -> None:
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "auth" && "$2" == "status" ]]; then\n'
        '  printf \'{"loggedIn": true, "authMethod": "claude.ai"}\\n\'\n'
        "  exit 0\n"
        "fi\n"
        'if [[ "$1" == "--print" ]]; then\n'
        "  echo 'Not logged in - Please run /login'\n"
        "  exit 1\n"
        "fi\n"
        'echo unexpected claude invocation: "$@" >&2\n'
        "exit 99\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)

    env = {
        **os.environ,
        "SKEP_CLAUDE_CODE_CMD": str(fake_claude),
        "SKEP_CLAUDE_SMOKE_KEEP": "0",
    }
    result = subprocess.run(
        [str(ROOT / "scripts" / "claude-adapter-smoke.sh")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 2
    assert "Claude Code print-mode preflight failed." in result.stderr
    assert "Not logged in" in result.stderr
    assert "task " not in result.stdout


def test_claude_adapter_smoke_auth_uses_executable_when_command_has_flags(
    tmp_path: Path,
) -> None:
    calls = tmp_path / "calls.log"
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{calls}"\n'
        'if [[ "$1" == "auth" && "$2" == "status" ]]; then\n'
        '  printf \'{"loggedIn": true, "authMethod": "claude.ai"}\\n\'\n'
        "  exit 0\n"
        "fi\n"
        'if [[ "$1" == "--fallback-model" && "$2" == "sonnet" && "$3" == "--print" ]]; then\n'
        "  echo 'fallback model still unavailable'\n"
        "  exit 1\n"
        "fi\n"
        'echo unexpected claude invocation: "$@" >&2\n'
        "exit 99\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)

    env = {
        **os.environ,
        "SKEP_CLAUDE_CODE_CMD": f"{fake_claude} --fallback-model sonnet",
        "SKEP_CLAUDE_SMOKE_KEEP": "0",
    }
    result = subprocess.run(
        [str(ROOT / "scripts" / "claude-adapter-smoke.sh")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 2
    assert "Claude Code print-mode preflight failed." in result.stderr
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "auth status",
        " ".join(
            [
                "--fallback-model sonnet --print --max-budget-usd 0.01",
                "--no-session-persistence",
                "Reply with exactly: skep claude adapter smoke ready",
            ]
        ),
    ]
