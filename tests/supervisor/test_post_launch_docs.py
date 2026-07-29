from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_readme_links_post_launch_roadmap() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "docs/post-launch.md" in readme


def test_release_checklist_tracks_post_launch_doc() -> None:
    checklist = (ROOT / "docs" / "release-checklist.md").read_text(encoding="utf-8")

    assert "docs/post-launch.md" in checklist


def test_post_launch_doc_covers_phase_6_paths() -> None:
    doc = (ROOT / "docs" / "post-launch.md").read_text(encoding="utf-8").lower()

    for required in (
        "consulting",
        "hosted",
        "funding",
        "career impact",
        "first 6 months",
    ):
        assert required in doc


def test_post_launch_doc_names_six_month_success_metrics() -> None:
    doc = (ROOT / "docs" / "post-launch.md").read_text(encoding="utf-8")

    for required in (
        "GitHub stars",
        "PyPI downloads/month",
        "Companies using Skep",
        "Consulting revenue",
        "Blog posts published",
        "Contributors",
    ):
        assert required in doc
