from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_launch_plan_covers_public_launch_channels() -> None:
    launch = (ROOT / "docs" / "launch.md").read_text(encoding="utf-8")

    for required in (
        "## Hacker News",
        "## Reddit",
        "## Twitter / X",
        "## LinkedIn",
        "## Product Hunt",
        "## First 48 Hours",
    ):
        assert required in launch


def test_launch_plan_covers_first_48_and_minimal_analytics() -> None:
    launch = (ROOT / "docs" / "launch.md").read_text(encoding="utf-8")

    for required in (
        "GitHub stars/forks/clones",
        "PyPI download stats",
        "Cloudflare Analytics",
        "Check HN, Reddit, and Twitter every 30 minutes for the first 12 hours",
        "Ask 2-3 friends or collaborators to try it and star it on day 1",
    ):
        assert required in launch


def test_launch_plan_covers_launch_guardrails() -> None:
    launch = (ROOT / "docs" / "launch.md").read_text(encoding="utf-8")

    for required in (
        "Do not launch on a Friday or weekend",
        "Do not post and disappear",
        "Do not spam multiple subreddits",
        "Do not buy ads",
        "Do not publish the launch post on Medium",
    ):
        assert required in launch
