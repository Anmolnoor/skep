from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_legacy_sandbox_adrs_name_current_linux_bubblewrap_posture() -> None:
    for relpath in (
        "docs/adr/0005-seatbelt-sandbox.md",
        "docs/adr/0014-container-portability.md",
        "docs/adr/0018-container-packaging.md",
    ):
        text = (ROOT / relpath).read_text(encoding="utf-8")

        assert "Launch update" in text
        assert "bubblewrap" in text


def test_container_portability_adr_does_not_claim_old_launch_gaps() -> None:
    text = (ROOT / "docs/adr/0014-container-portability.md").read_text(encoding="utf-8")

    assert "No worker image is published" not in text
    assert "CI stays" not in text
    assert "not yet built (egress pin, spawner backend, Linux CI)" not in text
    assert "linux → container" not in text
    assert "The container is the Linux/CI boundary" not in text
    assert "Linux/container:" not in text
    assert "GHCR" in text
    assert "Linux/macOS CI" in text
