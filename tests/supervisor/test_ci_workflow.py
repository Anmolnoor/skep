from __future__ import annotations

from pathlib import Path


def test_ci_uses_first_party_cross_platform_matrix() -> None:
    workflow = (Path(__file__).parents[2] / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "runs-on: ${{ matrix.os }}" in workflow
    assert "ubuntu-latest" in workflow
    assert "macos-latest" in workflow
    assert "sudo apt-get install -y bubblewrap" in workflow
    assert "scripts/linux-sandbox-smoke.sh" in workflow
    assert "make smoke" in workflow


def test_ci_and_make_unit_keep_container_tests_opt_in() -> None:
    root = Path(__file__).parents[2]
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    makefile = (root / "Makefile").read_text(encoding="utf-8")

    marker = "not smoke and not container"
    assert marker in workflow
    assert marker in makefile


def test_release_workflow_builds_and_publishes_package() -> None:
    workflow = (Path(__file__).parents[2] / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "tags:" in workflow
    assert "v*" in workflow
    assert "name: Release gate" in workflow
    assert "UV_CACHE_DIR=.uv-cache scripts/local-release-gates.sh" in workflow
    assert "needs: release-gate" in workflow
    assert "id-token: write" in workflow
    assert "uv build" in workflow
    assert "uvx twine check dist/*" in workflow
    assert "scripts/package-install-smoke.sh" in workflow
    assert "pypa/gh-action-pypi-publish" in workflow
    assert "softprops/action-gh-release" in workflow
    assert "dist/skep-*.tar.gz" in workflow
    assert "dist/skep-*-py3-none-any.whl" in workflow


def test_local_lint_targets_cover_scripts_like_ci() -> None:
    makefile = (Path(__file__).parents[2] / "Makefile").read_text(encoding="utf-8")

    assert "uv run ruff format --check src/skep tests scripts" in makefile
    assert "uv run ruff format src/skep tests scripts" in makefile
