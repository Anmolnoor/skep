"""A tiny scripted worker for hermetic supervisor tests — no external worker, no LLM.

Speaks the contract wire format (raw JSON, stdlib only — childenv's one skep
import excepted) and takes its behavior from a ``MODE:<name>`` token in the
task instructions:

- happy:    full event stream, real patch artifact, completed result, exit 0
- pending:  approval.requested → terminal pending_approval, result, exit 4
- crash:    task.start then dies without terminal event or result, exit 9
- hang:     task.start + one heartbeat, then sleeps forever (monitor must kill)
- envdump:  writes its environment to <workspace>/envdump.json, then happy-min
- childenv: spawns a grandchild through the worker's REAL child-env boundary
            (the one skep import this file makes — a copy of the passthrough
            would prove nothing) and dumps the GRANDCHILD's env next to --out
- netprobe: attempts an outbound socket, records errno next to --out, then happy-min
- noresult: emits a completed terminal event but no result envelope
- badresult: emits a completed terminal event but writes malformed result JSON
- liar:     claims completed+passed, but the patch contradicts the recorded
            verification command (G10 re-verification must catch the lie)
- vacuous:  patches like liar, but nominates ``true`` as its own verification
            command. Nothing is false in the record — the worker simply chose a
            command that cannot fail, so re-running "the worker's verification"
            confirms a broken patch. Only a project-pinned verify_command
            (v88-F4) catches this one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

CONTRACT_VERSION = "0.1.0"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Stream:
    def __init__(self, path: Path, task_id: str, trace_id: str) -> None:
        self.path = path
        self.task_id = task_id
        self.trace_id = trace_id
        self.seq = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")

    def emit(self, event_type: str, payload: dict[str, object]) -> None:
        self.seq += 1
        line = {
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
            handle.write(json.dumps(line) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _result(
    task: dict[str, object],
    *,
    status: str,
    summary: str,
    outcome: str,
    details: str,
    changed_files: list[str],
    artifacts: list[dict[str, str]],
    commands: list[dict[str, object]] | None = None,
    usage: dict[str, object] | None = None,
) -> dict[str, object]:
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
        "usage": usage,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--task-file", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    task = json.loads(args.task_file.read_text())
    task_id = str(task["task_id"])
    trace_id = str(task["trace_id"])
    workspace = Path(str(task["workspace"]))
    instructions = str(task["instructions"])
    mode = "happy"
    # v54-F4: FILE:<name> picks which repo file the happy patch mutates, so a
    # test can land several runs on ONE branch without their patches colliding.
    file_name = "existing.py"
    for token in instructions.split():
        if token.startswith("MODE:"):
            mode = token.removeprefix("MODE:")
        if token.startswith("FILE:"):
            file_name = token.removeprefix("FILE:")

    stream = Stream(workspace / ".events" / f"{task_id}.ndjson", task_id, trace_id)
    start_payload: dict[str, object] = {
        "worker_version": "fake-1.0",
        "manifest_fingerprint": "f" * 64,
    }
    if task.get("project_context") is not None:
        start_payload["project_context"] = task["project_context"]
    if task.get("dispatch_decision") is not None:
        start_payload["dispatch_decision"] = task["dispatch_decision"]
    if task.get("landing_decision") is not None:
        start_payload["landing_decision"] = task["landing_decision"]
    stream.emit("task.start", start_payload)

    if mode == "crash":
        stream.emit("plan.created", {"steps": ["about to die"]})
        return 9

    if mode == "hang":
        stream.emit("heartbeat", {"phase": "executing"})
        time.sleep(300)
        return 0  # unreachable; the monitor kills us

    if mode in {"noresult", "badresult"}:
        stream.emit(
            "task.terminal",
            {"status": "completed", "summary": "claimed done without evidence"},
        )
        if mode == "badresult":
            args.out.write_text("{not valid json")
        return 0

    if mode == "envdump":
        # Written next to --out so the evidence survives worktree teardown.
        dump = args.out.parent / f"envdump-{task_id}.json"
        dump.write_text(json.dumps(dict(os.environ)))

    if mode == "childenv":
        # v109-F5: what a builtin worker's child shell command actually
        # spawns — env rebuilt by the real capability boundary, so the test
        # proves the passthrough tuple, not this script. The skep import is
        # slow; the heartbeat keeps the monitor's silence window open.
        stream.emit("heartbeat", {"phase": "executing"})
        import subprocess

        from skep.workers.capabilities import _child_process_env

        permissions = task.get("permissions") or {}
        assert isinstance(permissions, dict)
        grandchild = subprocess.run(
            [sys.executable, "-c", "import json, os; print(json.dumps(dict(os.environ)))"],
            capture_output=True,
            text=True,
            check=True,
            env=_child_process_env(
                env_allowlist=[str(n) for n in permissions.get("env_allowlist") or []],
                env_baseline=("PATH", "HOME"),
                network_allowlist=(),
            ),
        )
        (args.out.parent / f"childenv-{task_id}.json").write_text(grandchild.stdout)

    if mode == "netprobe":
        # Attempt an outbound connection; under the Seatbelt deny-all profile this
        # is physically refused with EPERM (errno 1). Recorded next to --out.
        import socket

        probe: dict[str, object] = {"connected": False, "errno": None}
        try:
            socket.setdefaulttimeout(5)
            socket.create_connection(("1.1.1.1", 443))
            probe["connected"] = True
        except OSError as exc:
            probe["errno"] = exc.errno
        (args.out.parent / f"netprobe-{task_id}.json").write_text(json.dumps(probe))

    approval_verdict = task.get("approval_verdict")
    resumed_with_approval = isinstance(approval_verdict, dict) and approval_verdict.get("approved")

    if mode == "pending" and not resumed_with_approval:
        # Leave evidence of in-flight work plus a v2 resume checkpoint so the
        # supervisor can resume this exact worktree in-place.
        (workspace / "pending-marker.txt").write_text("suspended here\n")
        artifacts_dir = workspace / ".artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        checkpoint = artifacts_dir / "resume-checkpoint.json"
        checkpoint.write_text(
            json.dumps(
                {
                    "resume_checkpoint": {
                        "version": 2,
                        "plan": {
                            "summary": "fake suspended plan",
                            "files": [],
                            "verify": {"argv": ["true"]},
                        },
                        "workspace": str(workspace),
                        "cursor": {
                            "completed_steps": 0,
                            "changed_files": [],
                            "commands": [],
                            "verification": None,
                        },
                    }
                }
            )
        )
        stream.emit(
            "approval.requested",
            {"action": "git_commit", "reason": "instructions asked for a commit"},
        )
        stream.emit(
            "task.terminal",
            {"status": "pending_approval", "summary": "stopped for approval"},
        )
        event_log = workspace / ".events" / f"{task_id}.ndjson"
        result = _result(
            task,
            status="pending_approval",
            summary="stopped for approval",
            outcome="not_attempted",
            details="stopped before verification",
            changed_files=[],
            artifacts=[
                {
                    "kind": "event_log",
                    "path": str(event_log.relative_to(workspace)),
                    "sha256": _sha256(event_log),
                },
                {
                    "kind": "file",
                    "path": str(checkpoint.relative_to(workspace)),
                    "sha256": _sha256(checkpoint),
                },
            ],
        )
        args.out.write_text(json.dumps(result))
        return 4

    if mode == "pending" and resumed_with_approval:
        # Record whether the suspended attempt's evidence survived into this
        # run's workspace (true only for an in-place resume).
        (args.out.parent / f"reuse-{task_id}.json").write_text(
            json.dumps({"marker": (workspace / "pending-marker.txt").exists()})
        )

    # happy / envdump / netprobe / liar: mutate a file, write a real patch
    # artifact, claim verify "passed". The recorded verification command checks
    # for `value = 1`; MODE:liar patches `value = 999` instead, so supervisor
    # re-verification (G10) re-runs the command against the patch and disagrees.
    new_value = "999" if mode in ("liar", "vacuous") else "1"
    target = workspace / file_name
    target.write_text(f"value = {new_value}\n")
    # v88-F4: MODE:vacuous nominates a command that cannot fail. The patch is
    # as broken as MODE:liar's, but re-running the WORKER'S choice confirms it.
    verify_command = "true" if mode == "vacuous" else f'grep -q "value = 1" {file_name}'
    stream.emit("file.changed", {"path": file_name, "change": "modified"})
    stream.emit("command.start", {"command": verify_command, "purpose": "verify"})
    stream.emit(
        "command.result",
        {
            "command": verify_command,
            "exit_code": 0,
            "duration_ms": 12,
            "stdout_tail": "",
            "stderr_tail": "",
        },
    )
    artifacts_dir = workspace / ".artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    patch_path = artifacts_dir / f"{task_id}.patch"
    patch_path.write_text(
        f"--- a/{file_name}\n+++ b/{file_name}\n@@ -1 +1 @@\n-value = 0\n+value = {new_value}\n"
    )
    stream.emit(
        "verify.result",
        {"outcome": "passed", "details": "claimed passed", "commands": [verify_command]},
    )
    stream.emit(
        "task.terminal",
        {"status": "completed", "summary": "fixed and verified"},
    )

    event_log = workspace / ".events" / f"{task_id}.ndjson"
    result = _result(
        task,
        status="completed",
        summary="fixed and verified",
        outcome="passed",
        details="claimed passed (exit 0)",
        changed_files=[file_name],
        commands=[{"command": verify_command, "exit_code": 0, "purpose": "verify"}],
        usage={"provider_calls": 2, "input_tokens": 120, "output_tokens": 40},
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
    args.out.write_text(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
