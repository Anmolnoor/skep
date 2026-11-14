"""Suite-wide fixtures."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from shutil import rmtree

import pytest

from tests.supervisor import strict_wire


@pytest.fixture(autouse=True)
def _strict_fake_refusals() -> Iterator[None]:
    """ES-F1 (B43): a strict provider fake's 400 fails the test even when the
    product swallowed it (halve-on-4xx, retry). Tests that provoke one on
    purpose assert on ``strict_wire.REJECTIONS`` and clear it."""
    strict_wire.REJECTIONS.clear()
    yield
    refused = list(strict_wire.REJECTIONS)
    strict_wire.REJECTIONS.clear()
    assert not refused, f"a strict provider fake refused a request body: {refused}"


@pytest.fixture(autouse=True)
def _no_real_codex_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """ES-F120: skep reads the operator's Codex login from $CODEX_HOME (else
    ~/.codex); every test gets an empty one so none ever reads the real file."""
    monkeypatch.setenv("CODEX_HOME", str(Path(tmp_path_factory.mktemp("codex-home"))))


@pytest.fixture(autouse=True)
def _private_proxy_socket_root(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """ES-F257 follow-up: proxy socket dirs (skep-nx-*) land under
    XDG_RUNTIME_DIR, else /tmp (dispatch._proxy_socket_root). A test whose
    daemon thread or simulated crash skips the proxy's finally left them in
    the operator's real runtime dir. Each test gets its own dir beside the
    real one — short (AF_UNIX's 108-char cap) and never under the
    redirected TMPDIR, which bwrap tmpfs-masks — removed afterwards."""
    real = os.environ.get("XDG_RUNTIME_DIR")
    root = tempfile.mkdtemp(prefix="skt-", dir=real if real and os.path.isdir(real) else "/tmp")
    monkeypatch.setenv("XDG_RUNTIME_DIR", root)
    yield
    rmtree(root, ignore_errors=True)  # bound at import: tests patch shutil.rmtree
