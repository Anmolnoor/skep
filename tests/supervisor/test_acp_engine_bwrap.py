"""Phase 04 4.1 on Linux: the ACP adapter inside the real strict bubblewrap
wall (ADR 0068) cannot read a home canary — the twin of the macOS Seatbelt
proof in test_acp_engine.py, whose helpers it reuses."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from skep.supervisor import SupervisorConfig, sandbox

from .test_acp_engine import EDIT, _dispatch, _patch


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or sandbox.availability().backend != "bubblewrap",
    reason="the real strict bubblewrap wall (Linux)",
)
def test_the_adapter_inside_the_strict_bwrap_wall_cannot_read_a_home_canary(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "fakehome"
    canary = home / "Downloads" / "canary-downloads.txt"
    canary.parent.mkdir(parents=True)
    canary.write_text("CANARY\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    probe = {"read": str(canary), "into": "canary.txt"}
    outcome = _dispatch(
        config, repo, monkeypatch, tmp_path, [probe, EDIT], unlocked=True,
        strict_read_confinement=True,
    )  # fmt: skip
    assert outcome.record.state == "completed", outcome.record.summary
    patch = _patch(config, outcome.record.task_id)
    (seen,) = [line for line in patch.splitlines() if line.endswith("Error")]
    assert seen in ("+PermissionError", "+FileNotFoundError") and "CANARY" not in patch, patch
