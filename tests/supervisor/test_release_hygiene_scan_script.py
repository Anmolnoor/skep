from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_release_hygiene_scan_script_is_documented_and_makefile_backed() -> None:
    script = ROOT / "scripts" / "release-hygiene-scan.sh"
    checklist = (ROOT / "docs" / "release-checklist.md").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    local_gates = (ROOT / "scripts" / "local-release-gates.sh").read_text(encoding="utf-8")

    assert script.is_file()
    assert "scripts/release-hygiene-scan.sh" in checklist
    assert "release-hygiene-scan:" in makefile
    assert "./scripts/release-hygiene-scan.sh" in local_gates


def test_release_hygiene_scan_script_covers_automatable_checks() -> None:
    script = (ROOT / "scripts" / "release-hygiene-scan.sh").read_text(encoding="utf-8")

    assert "old project names" in script
    assert "personal machine paths" in script
    assert "narrow secret patterns" in script
    for path in ("README.md", "docs", "examples", "CONTRIBUTING.md", "Makefile", "pyproject.toml"):
        assert path in script
    assert "git log --all -p" in script
    assert "sk-[A-Za-z0-9_-]{20,}" in script
    assert "RELEASE HYGIENE PASS" in script
