"""Regression checks for the launch reliability script."""

from __future__ import annotations

import re
from pathlib import Path


def test_reliability_script_uses_explicit_execution_mode() -> None:
    script = Path(__file__).resolve().parents[2] / "scripts" / "reliability.sh"
    text = script.read_text(encoding="utf-8")

    run_invocation = re.search(
        r'uv run skep --home "\$HOME_DIR" run "\$TOY".*?--quiet',
        text,
        flags=re.DOTALL,
    )

    assert run_invocation is not None
    assert "--execution-mode workspace" in run_invocation.group(0)
