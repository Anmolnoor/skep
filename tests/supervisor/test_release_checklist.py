from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

DOC_LINK_PATTERNS = (
    re.compile(r"\[[^\]]+\]\(([^)#][^)]+)\)"),
    re.compile("h" r'ref="(\./[^"]+|[^:>#][^"]*)"'),
    re.compile("s" r'rc="(\./[^"]+|[^:>#][^"]*)"'),
)


def test_release_checklist_uses_repo_python_for_link_checker() -> None:
    checklist = (ROOT / "docs" / "release-checklist.md").read_text(encoding="utf-8")
    link_check = checklist.split("Run a relative-link check before release:", 1)[1]
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "\npython - <<'PY'" not in link_check
    assert ".venv/bin/python scripts/docs-link-smoke.py" in link_check
    assert (ROOT / "scripts" / "docs-link-smoke.py").is_file()
    assert "docs-link-smoke:" in makefile


def test_release_hygiene_scan_covers_gitignore_old_names() -> None:
    checklist = (ROOT / "docs" / "release-checklist.md").read_text(encoding="utf-8")
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    old_name = "bee" + "keeper"

    assert ".gitignore" in checklist
    assert old_name not in gitignore.lower()


def test_release_checklist_covers_landing_demo_assets() -> None:
    checklist = (ROOT / "docs" / "release-checklist.md").read_text(encoding="utf-8")

    for relpath in (
        "docs/demo-gif.md",
        "docs/site.css",
        "docs/site.js",
        "docs/assets/skep-demo.gif",
    ):
        assert relpath in checklist


def test_release_checklist_covers_package_install_smoke() -> None:
    checklist = (ROOT / "docs" / "release-checklist.md").read_text(encoding="utf-8")

    assert "scripts/package-install-smoke.sh" in checklist
    assert "installed wheel exposes a working `skep --version`" in checklist


def test_package_install_smoke_installs_built_wheel() -> None:
    script = (ROOT / "scripts" / "package-install-smoke.sh").read_text(encoding="utf-8")

    assert "dist/skep-*-py3-none-any.whl" in script
    assert "uv venv" in script
    assert "uv pip install" in script
    assert "skep --version" in script


def test_release_checklist_covers_social_account_day_of_checks() -> None:
    checklist = (ROOT / "docs" / "release-checklist.md").read_text(encoding="utf-8")

    for required in (
        "GitHub profile",
        "anmolnoor.com links to skep.anmolnoor.com",
        "consulting inquiries",
        "HN app / Reddit app / Twitter installed",
    ):
        assert required in checklist


def test_public_doc_relative_links_exist() -> None:
    missing: list[str] = []
    docs = sorted(ROOT.rglob("*.md")) + sorted((ROOT / "docs").rglob("*.html"))
    for path in docs:
        if any(part in {".git", ".venv", ".uv-cache"} for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in DOC_LINK_PATTERNS:
            for match in pattern.findall(text):
                target = match.strip()
                if not target or target.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                # v27-F1: an absolute path is never a repo-relative doc link
                # (uploaded snapshots carry author-machine paths).
                if target.startswith("/"):
                    continue
                # LAUNCH-2: JS placeholders in inline scripts are not links,
                # and a query string addresses the same file on disk.
                if "$" in target:
                    continue
                target = target.split("#", 1)[0].split("?", 1)[0]
                if not target or target.endswith("/"):
                    continue
                if not (path.parent / target).resolve().exists():
                    missing.append(f"{path.relative_to(ROOT)} -> {target}")

    assert not missing, "missing relative doc links/assets:\n" + "\n".join(missing)


def test_release_docs_exist_and_name_the_parked_steps() -> None:
    """v27-F4: the ADR index and the honest release-process doc."""
    adr_index = (ROOT / "docs" / "adr" / "README.md").read_text(encoding="utf-8")
    for adr in sorted((ROOT / "docs" / "adr").glob("0*.md")):
        assert adr.name in adr_index, f"ADR index is missing {adr.name}"

    releases = (ROOT / "docs" / "releases" / "README.md").read_text(encoding="utf-8")
    assert "local-release-gates.sh" in releases
    assert "trusted publisher" in releases
    assert "PARKED" in releases
    assert "agent-task-contract" in releases

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "scripts/install.sh" in readme
    assert "uvx skep" in readme


def test_readme_installs_above_the_fold() -> None:
    # LAUNCH-1-L5: "install one-liner and the security model above the fold"
    # is a checkable claim, not an aspiration.
    head = "\n".join((ROOT / "README.md").read_text(encoding="utf-8").splitlines()[:40])
    assert "pipx install skep" in head
    # pip stays documented, but only as the inside-a-venv path (PEP 668).
    assert "pip install skep" in head
    assert any(word in head.lower() for word in ("sandbox", "verif", "approv"))


def test_release_runbook_has_go_no_go_and_rehearsal_lane() -> None:
    """v37-F1: everything below the operator's go/no-go is scripted."""
    releases = (ROOT / "docs" / "releases" / "README.md").read_text(encoding="utf-8")
    assert "Go/no-go" in releases
    assert "testpypi-rehearsal" in releases
    assert "mirror-demo-repo.sh" in releases

    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch" in workflow
    assert "https://test.pypi.org/legacy/" in workflow
    # A manual dispatch must never reach the GitHub-release/PyPI job.
    assert "if: startsWith(github.ref, 'refs/tags/')" in workflow
    assert "if: github.event_name == 'workflow_dispatch'" in workflow


def test_mirror_demo_repo_dry_run_lists_and_touches_nothing(tmp_path: Path) -> None:
    import subprocess

    before = sorted(p for p in (ROOT / "examples" / "skep-demo").rglob("*"))
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "mirror-demo-repo.sh"), "--dry-run"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "examples/skep-demo/app.py" in result.stdout
    assert sorted(p for p in (ROOT / "examples" / "skep-demo").rglob("*")) == before

    script = (ROOT / "scripts" / "mirror-demo-repo.sh").read_text(encoding="utf-8")
    assert "--push" in script  # push is opt-in, never the default


def test_docs_toc_and_community_files_exist() -> None:
    """v37-F2: the curated docs index lists every doc; community surface exists."""
    toc = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    for doc in sorted((ROOT / "docs").glob("*.md")):
        if doc.name == "README.md":
            continue
        assert doc.name in toc, f"docs/README.md is missing {doc.name}"

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/README.md" in readme

    # LAUNCH-2: the landing links the docs hub; the hub links the curated
    # markdown index, so every doc stays one hop from the front page.
    landing = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    assert 'href="./docs.html"' in landing
    hub = (ROOT / "docs" / "docs.html").read_text(encoding="utf-8")
    assert 'href="./README.md"' in hub

    assert (ROOT / "CONTRIBUTING.md").is_file()
    assert (ROOT / "SECURITY.md").is_file()
    assert (ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.md").is_file()
    assert (ROOT / ".github" / "ISSUE_TEMPLATE" / "question.md").is_file()


def test_link_checker_still_catches_broken_relative_links(tmp_path: Path) -> None:
    """v27-F1 guard-the-guard: skipping absolute paths must not blind the
    checker to genuinely broken relative links."""
    import subprocess
    import sys

    doc_root = tmp_path / "repo"
    (doc_root / "docs").mkdir(parents=True)
    (doc_root / "scripts").mkdir()
    (doc_root / "README.md").write_text(
        "[gone](docs/missing.md)\n[absolute is skipped](/Users/nobody/x.md)\n"
    )
    script = (ROOT / "scripts" / "docs-link-smoke.py").read_text(encoding="utf-8")
    (doc_root / "scripts" / "docs-link-smoke.py").write_text(script)
    result = subprocess.run(
        [sys.executable, str(doc_root / "scripts" / "docs-link-smoke.py")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "docs/missing.md" in result.stdout
    assert "/Users/nobody/x.md" not in result.stdout
