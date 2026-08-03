from __future__ import annotations

import tomllib
from pathlib import Path


def test_pyproject_has_public_release_metadata() -> None:
    pyproject = tomllib.loads(
        (Path(__file__).parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    )
    project = pyproject["project"]

    assert project["name"] == "skep"
    # v27-F2: a public v1.0.0 tag already exists; the next release must sort
    # after it and PyPI rejects re-uploads.
    assert project["version"] == "1.0.2"
    assert project["requires-python"] == ">=3.12"
    assert project["authors"] == [{"name": "Anmol Noor"}]
    assert {"ai-agents", "sandbox", "verification", "approval-workflow"} <= set(project["keywords"])
    assert {
        "Development Status :: 3 - Alpha",
        "License :: OSI Approved :: MIT License",
        "Operating System :: MacOS",
        "Operating System :: POSIX :: Linux",
        "Programming Language :: Python :: 3.12",
        "Topic :: Software Development :: Quality Assurance",
    } <= set(project["classifiers"])
    assert project["urls"] == {
        "Homepage": "https://skep.anmolnoor.com",
        "Documentation": "https://github.com/Anmolnoor/skep/tree/main/docs",
        "Issues": "https://github.com/Anmolnoor/skep/issues",
        "Source": "https://github.com/Anmolnoor/skep",
    }


def test_license_file_is_mit() -> None:
    # LAUNCH-1-L1: the classifier above is pinned; the 661-line file it
    # described never was — which is how a license sits unread.
    license_text = (Path(__file__).parents[2] / "LICENSE").read_text(encoding="utf-8")
    assert license_text.startswith("MIT License")


def test_ruff_includes_launch_scripts() -> None:
    pyproject = tomllib.loads(
        (Path(__file__).parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert "scripts/**/*.py" in pyproject["tool"]["ruff"]["include"]
