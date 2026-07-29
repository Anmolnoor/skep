from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_scorecard_script_exists_and_is_makefile_backed() -> None:
    script = ROOT / "scripts" / "scorecard.py"
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert script.is_file()
    assert "scorecard:" in makefile
    assert "scripts/scorecard.py" in makefile
    assert "scorecard" in makefile.splitlines()[0]  # listed in .PHONY


def test_scorecard_script_is_a_thin_wrapper_over_the_runner() -> None:
    script = (ROOT / "scripts" / "scorecard.py").read_text(encoding="utf-8")
    assert "from skep.supervisor.scorecard_runner import main" in script
    assert "SystemExit(main())" in script
