"""LAUNCH-1-L2: the release hygiene sweep is a gate, not a grep session.

The script is exercised for real against a disposable copy of its expected
tree layout — a planted personal path must fail it, and the project's own
namespace identifiers must not.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "release-hygiene-scan.sh"

# Concatenated so the scanner does not flag its own test when it sweeps
# tests/ — these must never appear literally in this file.
PLANTED_PATH = "/home/" + "anmolnoor" + "/x"
PLANTED_EMAIL = "someone@" + "gmail" + ".com"
CONTACT_EMAIL = "anmolnoor59@" + "gmail" + ".com"

SCAN_DIRS = ["docs", "examples", "src/skep", "tests", ".github"]
SCAN_FILES = [
    "README.md",
    "CONTRIBUTING.md",
    "Makefile",
    "pyproject.toml",
    ".gitignore",
    "Dockerfile",
    "Dockerfile.dockerignore",
    "docker-compose.yml",
    "SECURITY.md",
    "agent-task-contract-spec-v0.1.md",
]


def _make_tree(tmp_path: Path) -> Path:
    for name in SCAN_DIRS:
        (tmp_path / name).mkdir(parents=True)
        (tmp_path / name / "placeholder.txt").write_text("ok\n", encoding="utf-8")
    for name in SCAN_FILES:
        (tmp_path / name).write_text("ok\n", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / SCRIPT.name)
    git = ["git", "-c", "user.email=test@example.invalid", "-c", "user.name=test"]
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run([*git, "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "seed"], cwd=tmp_path, check=True)
    return tmp_path


def _run(tree: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(tree / "scripts" / SCRIPT.name)],
        capture_output=True,
        text=True,
        check=False,
    )


needs_rg = pytest.mark.skipif(shutil.which("rg") is None, reason="requires ripgrep")


def test_scan_covers_personal_email_addresses() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert "@(gmail|proton|outlook)" in script
    assert "personal email addresses" in script
    # The published contact addresses are deliberate; the scanner must not
    # teach the operator to ignore it.
    assert '"$scan_path" == "SECURITY.md"' in script
    # LAUNCH-2: the site's private-channel pages (security, conduct) carry
    # the published contact; the rest of the site must stay address-free.
    assert "-g '!docs/security.html'" in script
    assert "-g '!docs/code-of-conduct.html'" in script
    assert "-g '!docs/launch.md'" in script
    assert "gitleaks" in script
    assert "agent-task-contract-spec-v0.1.md" in script


@needs_rg
def test_clean_tree_passes(tmp_path: Path) -> None:
    result = _run(_make_tree(tmp_path))

    assert result.returncode == 0, result.stderr
    assert "RELEASE HYGIENE PASS" in result.stdout


@needs_rg
def test_planted_personal_path_fails(tmp_path: Path) -> None:
    tree = _make_tree(tmp_path)
    (tree / "docs" / "note.md").write_text(PLANTED_PATH + "\n", encoding="utf-8")

    result = _run(tree)

    assert result.returncode != 0
    assert "personal machine paths" in result.stderr


@needs_rg
def test_planted_personal_email_fails(tmp_path: Path) -> None:
    tree = _make_tree(tmp_path)
    (tree / "README.md").write_text(f"contact me at {PLANTED_EMAIL}\n", encoding="utf-8")

    result = _run(tree)

    assert result.returncode != 0
    assert "personal email addresses" in result.stderr


@needs_rg
def test_namespace_identifiers_pass(tmp_path: Path) -> None:
    tree = _make_tree(tmp_path)
    (tree / "docs" / "note.md").write_text(
        "https://github.com/Anmolnoor/skep and ghcr.io/anmolnoor/skep\n",
        encoding="utf-8",
    )

    result = _run(tree)

    assert result.returncode == 0, result.stderr
    assert "RELEASE HYGIENE PASS" in result.stdout


@needs_rg
def test_deliberate_contact_addresses_pass(tmp_path: Path) -> None:
    tree = _make_tree(tmp_path)
    (tree / "SECURITY.md").write_text(f"Email {CONTACT_EMAIL}\n", encoding="utf-8")

    result = _run(tree)

    assert result.returncode == 0, result.stderr
