from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_local_release_gates_script_is_documented_and_makefile_backed() -> None:
    script = ROOT / "scripts" / "local-release-gates.sh"
    checklist = (ROOT / "docs" / "release-checklist.md").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert script.is_file()
    assert "scripts/local-release-gates.sh" in checklist
    assert "local-release-gates:" in makefile


def test_local_release_gates_cover_non_account_bound_release_checks() -> None:
    script = (ROOT / "scripts" / "local-release-gates.sh").read_text(encoding="utf-8")

    required = (
        "make all",
        "make smoke",
        "./scripts/reliability.sh",
        "./scripts/linux-sandbox-smoke.sh",
        "make docs-link-smoke",
        "uv build",
        "uvx twine check dist/*",
        "./scripts/package-install-smoke.sh",
        "./scripts/docker-image-smoke.sh",
        "./scripts/linux-sandbox-docker-smoke.sh",
    )
    for command in required:
        assert command in script

    assert "scripts/claude-adapter-smoke.sh" not in script
