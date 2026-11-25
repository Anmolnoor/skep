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
    assert project["version"] == "2.0.1"
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


def test_sdist_leaves_the_docs_site_out() -> None:
    """docs-site: site/ holds React Bits copies (MIT + Commons Clause) that may
    be used in the website but not redistributed, so the sdist excludes it.
    The wheel is `packages = ["src/skep"]` and never saw it. pytest cannot
    build an sdist offline; the release gate lists the real one."""
    root = Path(__file__).parents[2]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    targets = pyproject["tool"]["hatch"]["build"]["targets"]

    assert targets["sdist"]["exclude"] == ["site"]
    assert targets["wheel"]["packages"] == ["src/skep"]

    smoke = (root / "scripts" / "package-install-smoke.sh").read_text(encoding="utf-8")
    assert 'sdists=("$ROOT"/dist/skep-*.tar.gz)' in smoke
    assert "No built sdist found" in smoke  # a wheel-only dist/ must not pass unchecked
    assert "sdist carries site/" in smoke
