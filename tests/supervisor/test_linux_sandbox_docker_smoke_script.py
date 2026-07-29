from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_release_checklist_points_to_linux_sandbox_docker_smoke_script() -> None:
    script = ROOT / "scripts" / "linux-sandbox-docker-smoke.sh"
    checklist = (ROOT / "docs" / "release-checklist.md").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert script.is_file()
    assert "scripts/linux-sandbox-docker-smoke.sh" in checklist
    assert "linux-sandbox-docker-smoke:" in makefile


def test_linux_sandbox_docker_smoke_builds_image_and_runs_linux_smoke() -> None:
    script = (ROOT / "scripts" / "linux-sandbox-docker-smoke.sh").read_text(encoding="utf-8")

    assert "SKEP_DOCKER_IMAGE:-skep:linux-sandbox-smoke" in script
    assert "SKEP_DOCKER_BUILD:-1" in script
    assert 'docker build -f "$ROOT/Dockerfile" -t "$IMAGE" "$ROOT"' in script
    assert 'docker run --rm --entrypoint bash "$IMAGE"' in script
    assert "scripts/linux-sandbox-smoke.sh" in script
