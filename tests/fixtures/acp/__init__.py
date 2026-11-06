"""ACP wire fixtures (README.md) and the shared envelope loader for the B1 tests."""

from __future__ import annotations

import json
from pathlib import Path

from skep.supervisor.acp.proxy import Direction

ACP_DIR = Path(__file__).parent


def envelope_frames(path: Path, repo: str | None = None) -> list[tuple[Direction, bytes]]:
    """The ``(dir, raw line + newline)`` frames of one envelope ``.ndjson`` file, in order.

    ``repo`` replaces the scrubbed ``$REPO`` placeholder in each raw line.
    """
    frames: list[tuple[Direction, bytes]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        env = json.loads(line)
        raw = env["raw"] if repo is None else env["raw"].replace("$REPO", repo)
        frames.append((env["dir"], raw.encode("utf-8") + b"\n"))
    return frames


ALL_FIXTURES = sorted(ACP_DIR.rglob("*.ndjson"))
