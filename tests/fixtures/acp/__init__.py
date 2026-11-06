"""ACP wire fixtures (README.md) and the shared envelope loader for the B1 tests."""

from __future__ import annotations

import json
from pathlib import Path

from skep.supervisor.acp.proxy import Direction

ACP_DIR = Path(__file__).parent


def envelope_frames(path: Path) -> list[tuple[Direction, bytes]]:
    """The ``(dir, raw line + newline)`` frames of one envelope ``.ndjson`` file, in order."""
    frames: list[tuple[Direction, bytes]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        env = json.loads(line)
        frames.append((env["dir"], env["raw"].encode("utf-8") + b"\n"))
    return frames


ALL_FIXTURES = sorted(ACP_DIR.rglob("*.ndjson"))
