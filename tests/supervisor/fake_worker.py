"""A tiny scripted worker for hermetic supervisor tests — no external worker, no LLM.

Speaks the contract wire format (raw JSON, stdlib only — childenv's one skep
import excepted) and takes its behavior from a ``MODE:<name>`` token in the
task instructions:

- happy:    full event stream, real patch artifact, completed result, exit 0
- pending:  approval.requested → terminal pending_approval, result, exit 4
- crash:    task.start then dies without terminal event or result, exit 9
- crashgate: dies like crash but leaves a v2 resume checkpoint; its resume
            gates on the shell command ``make check`` (pending, exit 4)
- shellgate: pending, gating on the shell command ``GATE:<argv>`` (``_`` for
            spaces); resumed with an approval it runs happy
- hostgate: pending, gating on network.fetch of host ``GATE:<host>``
- hang:     task.start + one heartbeat, then sleeps forever (monitor must kill)
- envdump:  writes its environment to <workspace>/envdump.json, then happy-min
- childenv: spawns a grandchild through the worker's REAL child-env boundary
            (the one skep import this file makes — a copy of the passthrough
            would prove nothing) and dumps the GRANDCHILD's env next to --out
- netprobe: attempts an outbound socket, records errno next to --out, then happy-min
- noresult: emits a completed terminal event but no result envelope
- badresult: emits a completed terminal event but writes malformed result JSON
- unclaimed: happy, but the result's changed_files says [] — the shape of a
            file changed by a shell command the worker never counted (v115-F15)
- forge:    tries to overwrite another run's result (``victim``) beside its own
            result directory, records what the sandbox let it do next to
            --out, then happy-min (W26)
- gitpush:  the worker tries to edit, delete or replace the repo's
            skep-protocol-pin and to turn on worktree config, then a
            grandchild with an EMPTIED env runs ``git push`` over https;
            all outcomes recorded next to --out (W31), then happy-min
- wrongid:  happy, but its result envelope names another task id (W26)
- liar:     claims completed+passed, but the patch contradicts the recorded
            verification command (G10 re-verification must catch the lie)
- vacuous:  patches like liar, but nominates ``true`` as its own verification
            command. Nothing is false in the record — the worker simply chose a
            command that cannot fail, so re-running "the worker's verification"
            confirms a broken patch. Only a project-pinned verify_command
            (v88-F4) catches this one.

A ``GATE`` token (with any mode) holds the worker after ``task.start``,
heartbeating, until a ``gate`` file appears beside --out; it then records
whether its proxy answers on its port (``proxy-probe.txt``) — and, when
relayed (ADR 0062), the status of a model call through its relay
(``relay-probe.txt``) — and goes on
— the shape of a worker that outlives a serve restart (ADR 0060).
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
    gated = "make_check"
    for token in instructions.split():
        if token.startswith("MODE:"):
            mode = token.removeprefix("MODE:")
        if token.startswith("FILE:"):
            file_name = token.removeprefix("FILE:")
        if token.startswith("GATE:"):
            gated = token.removeprefix("GATE:")

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

    if "GATE" in instructions.split():
        gate_deadline = time.monotonic() + 120  # never outlive a failed test long
        while not (args.out.parent / "gate").exists() and time.monotonic() < gate_deadline:
            stream.emit("heartbeat", {"phase": "gated"})
            time.sleep(0.3)
        proxy = os.environ.get("HTTP_PROXY", "")
        proxy_probe = "no proxy"
        if proxy:
            import socket

            # A reply, not just an accept: on Linux the netshim inside the
            # sandbox accepts even when no proxy answers behind it.
            try:
                with socket.create_connection(("127.0.0.1", int(proxy.rsplit(":", 1)[1])), 5) as s:
                    s.sendall(b"CONNECT probe.invalid:443 HTTP/1.1\r\n\r\n")
                    reply = s.recv(16)
                proxy_probe = "connected" if reply.startswith(b"HTTP/1.") else f"no reply {reply!r}"
            except OSError as exc:
                proxy_probe = f"refused: {exc}"
        (args.out.parent / "proxy-probe.txt").write_text(proxy_probe)
        relay = os.environ.get("SKEP_PROVIDER_RELAY", "")
        if relay:  # ADR 0062: a model call through the run's relay, after the gate
            import urllib.error
            import urllib.request

            spec = json.loads(relay)
            request = urllib.request.Request(
                spec["url"] + "/v1/models", headers={"Authorization": f"Bearer {spec['token']}"}
            )
            try:
                with urllib.request.urlopen(request, timeout=10) as reply:
                    relay_probe = str(reply.status)
            except urllib.error.HTTPError as exc:
                relay_probe = str(exc.code)
            except OSError as exc:
                relay_probe = f"refused: {exc}"
            (args.out.parent / "relay-probe.txt").write_text(relay_probe)

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

    if mode == "forge":
        attempts: dict[str, object] = {}
        for target in (
            args.out.parent.parent / "victim" / "result.json",
            args.out.parent.parent / "victim.json",
        ):
            try:
                target.write_text('{"forged": true}')
                attempts[target.name] = "written"
            except OSError as exc:
                attempts[target.name] = exc.errno
        (args.out.parent / f"forge-{task_id}.json").write_text(json.dumps(attempts))

    if mode == "gitpush":
        import shutil
        import subprocess

        git_bin = shutil.which("git") or "git"

        def rev_parse(flag: str) -> Path:
            out = subprocess.run(
                [git_bin, "rev-parse", flag], cwd=workspace, capture_output=True, text=True
            ).stdout.strip()
            return Path(out) if Path(out).is_absolute() else Path(workspace, out)

        git_dir = rev_parse("--absolute-git-dir")
        pin = rev_parse("--git-common-dir") / "skep-protocol-pin"
        decoy = git_dir / "decoy"
        unpin = "[protocol]\n\tallow = always\n"
        tries: dict[str, object] = {}

        def replace() -> None:
            decoy.write_text(unpin)
            os.replace(decoy, pin)

        def shared_config() -> None:
            with Path(pin.parent, "config").open("a") as config:
                config.write("[extensions]\n\tworktreeConfig = true\n")

        for name, attempt in (
            ("edit", lambda: pin.write_text(unpin)),
            ("unlink", pin.unlink),
            ("replace", replace),
            ("shared_config", shared_config),
        ):
            try:
                attempt()
                tries[name] = "done"
            except OSError as exc:
                tries[name] = exc.errno
        # Its own git dir is writable: a config.worktree there is ignored while
        # the shared config leaves extensions.worktreeConfig off.
        Path(git_dir, "config.worktree").write_text(unpin)
        pushed = subprocess.run(
            [git_bin, "push", "https://192.0.2.1/x.git", "HEAD:refs/heads/x"],
            cwd=workspace,
            env={},  # what `env -i` or subprocess.run(env={}) hands git
            capture_output=True,
            text=True,
            timeout=60,
        )
        (args.out.parent / f"gitpush-{task_id}.json").write_text(
            json.dumps({"rc": pushed.returncode, "stderr": pushed.stderr, **tries})
        )

    approval_verdict = task.get("approval_verdict")
    resumed_with_approval = isinstance(approval_verdict, dict) and approval_verdict.get("approved")

    crash_first = mode == "crashgate" and task.get("resume_of") is None
    if mode in {"pending", "crashgate", "shellgate", "hostgate"} and not resumed_with_approval:
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
        if crash_first:
            return 9
        gate: dict[str, object] = {
            "action": "git_commit",
            "reason": "instructions asked for a commit",
        }
        if mode == "crashgate":
            gate = {
                "action": "shell.run",
                "reason": "shell.run requires approval for command: make check",
                "commands": [["make", "check"]],
            }
        if mode == "shellgate":
            argv = gated.split("_")
            gate = {
                "action": "shell.run",
                "reason": f"shell.run requires approval for command: {' '.join(argv)}",
                "commands": [argv],
            }
        if mode == "hostgate":
            gate = {
                "action": "network.fetch",
                "reason": f"network host {gated} is not allowlisted",
                "decision": {
                    "verdict": "require_approval",
                    "reason": "network.host_not_allowlisted",
                    "detail": gated,
                },
            }
        stream.emit("approval.requested", gate)
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

    if mode in {"pending", "shellgate", "hostgate"} and resumed_with_approval:
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
        changed_files=[] if mode == "unclaimed" else [file_name],
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
    if mode == "wrongid":
        result["task_id"] = "someone-elses-task"
    args.out.write_text(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
