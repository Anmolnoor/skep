#!/usr/bin/env python3
"""Deterministic worker used only for launch demo recording.

It speaks Skep's worker contract, requests one shell approval for
``python3 scripts/check.py``, then completes after that command is approved or
remembered. This keeps the public demo local, repeatable, and free of provider
secrets while still exercising the real supervisor run/review/approval loop.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from skep.worker_contract import CONTRACT_VERSION

VERIFY_COMMAND = "python3 scripts/check.py"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: UP017


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class EventStream:
    def __init__(self, path: Path, task_id: str, trace_id: str) -> None:
        self.path = path
        self.task_id = task_id
        self.trace_id = trace_id
        self.seq = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self.seq += 1
        event = {
            "contract_version": CONTRACT_VERSION,
            "event_id": str(uuid.uuid4()),
            "seq": self.seq,
            "task_id": self.task_id,
            "trace_id": self.trace_id,
            "ts": _now(),
            "type": event_type,
            "payload": payload,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def _result(
    task: dict[str, Any],
    *,
    status: str,
    summary: str,
    outcome: str,
    details: str,
    changed_files: list[str],
    artifacts: list[dict[str, str]],
    commands: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "task_id": task["task_id"],
        "trace_id": task["trace_id"],
        "status": status,
        "summary": summary,
        "changed_files": changed_files,
        "commands": commands or [],
        "verification": {"outcome": outcome, "details": details},
        "artifacts": artifacts,
        "usage": {"provider_calls": 0, "input_tokens": 0, "output_tokens": 0},
    }


def _argv_matches_prefix(argv: list[str], prefixes: list[list[str]]) -> bool:
    return any(argv[: len(prefix)] == prefix for prefix in prefixes if prefix)


def _has_shell_grant(task: dict[str, Any]) -> bool:
    verdict = task.get("approval_verdict")
    if (
        isinstance(verdict, dict)
        and verdict.get("approved")
        and verdict.get("action") == "shell.run"
    ):
        return True
    permissions = task.get("permissions")
    if not isinstance(permissions, dict):
        return False
    shell_allowlist = permissions.get("shell_allowlist")
    if not isinstance(shell_allowlist, list):
        return False
    prefixes = [prefix for prefix in shell_allowlist if isinstance(prefix, list)]
    return _argv_matches_prefix(VERIFY_COMMAND.split(), prefixes)


def _write_pending(task: dict[str, Any], stream: EventStream, out_path: Path) -> int:
    reason = f"shell.run requires approval for command: {VERIFY_COMMAND}"
    stream.emit(
        "approval.requested",
        {
            "action": "shell.run",
            "reason": reason,
            "decision": {
                "verdict": "require_approval",
                "reason": "capability.require_approval.shell_nonverify_not_allowlisted",
                "detail": VERIFY_COMMAND,
            },
        },
    )
    stream.emit(
        "task.terminal", {"status": "pending_approval", "summary": "waiting on shell approval"}
    )
    workspace = Path(str(task["workspace"]))
    event_log = workspace / ".events" / f"{task['task_id']}.ndjson"
    out_path.write_text(
        json.dumps(
            _result(
                task,
                status="pending_approval",
                summary="waiting on shell approval",
                outcome="not_attempted",
                details="verification command requires approval",
                changed_files=[],
                artifacts=[
                    {
                        "kind": "event_log",
                        "path": str(event_log.relative_to(workspace)),
                        "sha256": _sha256(event_log),
                    }
                ],
            )
        ),
        encoding="utf-8",
    )
    return 4


def _desired_function(instructions: str) -> tuple[str, str]:
    if "goodbye" in instructions.lower():
        return "goodbye", 'def goodbye():\n    return "goodbye"\n'
    return "health", 'def health():\n    return {"status": "ok"}\n'


def _ensure_app_change(workspace: Path, instructions: str) -> str:
    name, function_text = _desired_function(instructions)
    app_path = workspace / "app.py"
    existing = app_path.read_text(encoding="utf-8") if app_path.exists() else ""
    if f"def {name}(" not in existing:
        separator = "\n\n" if existing.strip() else ""
        app_path.write_text(existing.rstrip() + separator + function_text, encoding="utf-8")
    return name


def _git_diff(workspace: Path) -> str:
    result = subprocess.run(
        ["git", "diff", "--binary"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _write_completed(task: dict[str, Any], stream: EventStream, out_path: Path) -> int:
    workspace = Path(str(task["workspace"]))
    function_name = _ensure_app_change(workspace, str(task["instructions"]))
    stream.emit("file.changed", {"path": "app.py", "change": "modified"})
    stream.emit("command.start", {"command": VERIFY_COMMAND, "purpose": "verify"})
    verify = subprocess.run(
        VERIFY_COMMAND,
        cwd=workspace,
        shell=True,
        capture_output=True,
        text=True,
    )
    stream.emit(
        "command.result",
        {
            "command": VERIFY_COMMAND,
            "exit_code": verify.returncode,
            "duration_ms": 18,
            "stdout_tail": verify.stdout[-2000:],
            "stderr_tail": verify.stderr[-2000:],
        },
    )
    artifacts_dir = workspace / ".artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    patch_path = artifacts_dir / f"{task['task_id']}.patch"
    patch_path.write_text(_git_diff(workspace), encoding="utf-8")
    status = "completed" if verify.returncode == 0 else "failed"
    outcome = "passed" if verify.returncode == 0 else "failed"
    summary = f"added {function_name} endpoint"
    stream.emit(
        "verify.result", {"outcome": outcome, "details": summary, "commands": [VERIFY_COMMAND]}
    )
    stream.emit("task.terminal", {"status": status, "summary": summary})
    event_log = workspace / ".events" / f"{task['task_id']}.ndjson"
    out_path.write_text(
        json.dumps(
            _result(
                task,
                status=status,
                summary=summary,
                outcome=outcome,
                details=f"{VERIFY_COMMAND} exited {verify.returncode}",
                changed_files=["app.py"],
                commands=[
                    {
                        "command": VERIFY_COMMAND,
                        "exit_code": verify.returncode,
                        "purpose": "verify",
                    }
                ],
                artifacts=[
                    {
                        "kind": "event_log",
                        "path": str(event_log.relative_to(workspace)),
                        "sha256": _sha256(event_log),
                    },
                    {
                        "kind": "patch",
                        "path": str(patch_path.relative_to(workspace)),
                        "sha256": _sha256(patch_path),
                    },
                ],
            )
        ),
        encoding="utf-8",
    )
    return 0 if verify.returncode == 0 else 3


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--task-file", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    task = json.loads(args.task_file.read_text(encoding="utf-8"))
    task_id = str(task["task_id"])
    trace_id = str(task["trace_id"])
    workspace = Path(str(task["workspace"]))
    stream = EventStream(workspace / ".events" / f"{task_id}.ndjson", task_id, trace_id)
    stream.emit(
        "task.start",
        {"worker_version": "skep-demo-worker-0.1.0", "manifest_fingerprint": "d" * 64},
    )
    if not _has_shell_grant(task):
        return _write_pending(task, stream, args.out)
    return _write_completed(task, stream, args.out)


if __name__ == "__main__":
    sys.exit(main())
