from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from skep.supervisor import sandbox


def test_sandbox_available_requires_successful_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """A present sandbox-exec binary is not enough; profile application must work."""

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["sandbox-exec"],
            returncode=71,
            stdout="",
            stderr="sandbox-exec: sandbox_apply: Operation not permitted\n",
        )

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "exists", lambda _path: True)
    monkeypatch.setattr(subprocess, "run", fake_run)
    sandbox.availability.cache_clear()
    try:
        probe = sandbox.availability()

        assert probe.usable is False
        assert probe.reason == "probe_rejected"
        assert "sandbox_apply" in str(probe.detail)
        assert sandbox.available() is False
    finally:
        sandbox.availability.cache_clear()
