from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_version_history_does_not_list_linux_ci_as_remaining_launch_work() -> None:
    history = (ROOT / "docs" / "version-history.md").read_text(encoding="utf-8")

    stale_text = "egress pin (iptables in the container netns), a spawner backend, and Linux CI"

    assert stale_text not in history
    assert "Linux/macOS CI" in history


def test_version_history_names_every_round_since_v19() -> None:
    """v39-F5: history can never silently skip a round again (v20-v36 did —
    a full plan audit had to rediscover them from git)."""
    history = (ROOT / "docs" / "version-history.md").read_text(encoding="utf-8")
    for version in range(19, 45):
        assert f"## v{version} (" in history, f"v{version} missing from version-history"
