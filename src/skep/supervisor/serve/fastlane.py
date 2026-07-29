"""v83-F2: the run_code fast lane — supervisor-side, sandboxed, 10 seconds.

run_code (ADR 0024) dispatches a script-caste worker: worktree, contract,
spawn, 120s wall clock. That is the right shape for anything touching a
repo — and pure overkill for "sum these numbers". The fast lane runs the
same code supervisor-side INSIDE the same walls (I12): the native sandbox
backend (bwrap/Seatbelt), a throwaway tmp workspace, deny-all network,
10s timeout.

Fail-closed by construction: no usable native sandbox → the fast lane
does not exist on this host — ``run_code_fast`` returns None and the
caller falls through to the ordinary worker dispatch, naming the reason
(I9). The fast lane never executes unsandboxed under any input. The
result dict carries the evidence (code hash, backend, exit, output) and
lands in the chat action record like every mutation result (I8) — one
record system, no shadow event stream (I5).
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from ..sandbox import DENY_ALL_NETWORK, availability, wrap_command, write_profile

FAST_LANE_TIMEOUT_SECS = 10
FAST_LANE_OUTPUT_CAP = 10_000

FAST_LANE_FALLBACK_NOTE = (
    "fast lane needs a native sandbox (bwrap/Seatbelt) and none is usable on "
    "this host; dispatched as a sandboxed worker run instead"
)


def _capped(text: str) -> str:
    if len(text) <= FAST_LANE_OUTPUT_CAP:
        return text
    return text[:FAST_LANE_OUTPUT_CAP] + "\n… (truncated)"


def run_code_fast(language: str, code: str) -> dict[str, Any] | None:
    """Run a short python/shell script in the native sandbox, supervisor-side.

    Returns None when no native backend is usable — the caller's worker
    dispatch is the honest slow path (never fail-open).
    """
    probe = availability()
    if not probe.usable:
        return None
    with tempfile.TemporaryDirectory(prefix="skep-fastlane-") as tmp:
        workspace = Path(tmp)
        script_path = workspace / ("script.py" if language == "python" else "script.sh")
        script_path.write_text(code)
        profile = write_profile(
            workspace / "sandbox.profile",
            workspace=workspace,
            network=DENY_ALL_NETWORK,
        )
        argv = (
            [sys.executable, str(script_path)]
            if language == "python"
            else ["/bin/sh", str(script_path)]
        )
        base: dict[str, Any] = {
            "fast_lane": True,
            "sandbox_backend": probe.backend,
            "code_sha256": hashlib.sha256(code.encode()).hexdigest(),
        }
        try:
            proc = subprocess.run(
                wrap_command(argv, profile),
                capture_output=True,
                text=True,
                timeout=FAST_LANE_TIMEOUT_SECS,
                cwd=str(workspace),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                **base,
                "timed_out": True,
                "error": (
                    f"fast lane timed out after {FAST_LANE_TIMEOUT_SECS}s — "
                    "retry without fast=true for the 120s worker lane"
                ),
            }
        return {
            **base,
            "exit_code": proc.returncode,
            "output": _capped(proc.stdout),
            **({"stderr": _capped(proc.stderr)} if proc.stderr else {}),
        }
