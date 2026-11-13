from __future__ import annotations

import subprocess
import sys
import tempfile
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


def test_seatbelt_probe_applies_the_real_profile_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """ES-F6 (B48, I8): under a parent sandbox, ``(allow default)`` applies
    but a profile with deny rules dies (rc=71), so the old probe said usable
    and every worker then crashed with a misleading diagnosis. The probe now
    runs what workers get: build_profile's deny-network / deny-write text."""
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    def no_temp_dir() -> str:
        raise FileNotFoundError("No usable temporary directory found")

    monkeypatch.setattr(Path, "exists", lambda _path: True)
    monkeypatch.setattr(subprocess, "run", fake_run)
    # Mac finding D: the probe runs /usr/bin/true and needs no writable root;
    # with no usable temp dir it raised instead of reporting probe_rejected.
    monkeypatch.setattr(tempfile, "gettempdir", no_temp_dir)
    assert sandbox._seatbelt_availability().usable is True
    profile = seen[0][seen[0].index("-p") + 1]
    assert "(deny network*)" in profile
    assert "(deny file-write*)" in profile
    assert "(allow file-write* (subpath" not in profile
