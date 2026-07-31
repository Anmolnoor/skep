#!/usr/bin/env python3
"""Release gate: verify public docs and landing-page relative links/assets."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {".git", ".venv", ".uv-cache"}
LINK_PATTERNS = (
    re.compile(r"\[[^\]]+\]\(([^)#][^)]+)\)"),
    re.compile("h" r'ref="(\./[^"]+|[^:>#][^"]*)"'),
    re.compile("s" r'rc="(\./[^"]+|[^:>#][^"]*)"'),
)


def _doc_paths() -> list[Path]:
    return sorted(ROOT.rglob("*.md")) + sorted((ROOT / "docs").rglob("*.html"))


def _relative_targets(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    targets: list[str] = []
    for pattern in LINK_PATTERNS:
        for match in pattern.findall(text):
            target = match.strip()
            if not target or target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            # v27-F1: an absolute path is never a repo-relative doc link
            # (uploaded snapshots carry author-machine paths).
            if target.startswith("/"):
                continue
            # LAUNCH-2: JS template/replace placeholders in inline scripts are
            # not links, and a query string addresses the same file on disk.
            if "$" in target:
                continue
            target = target.split("#", 1)[0].split("?", 1)[0]
            if not target:
                continue
            if target.endswith("/"):
                continue
            targets.append(target)
    return targets


def main() -> int:
    missing: list[str] = []
    for path in _doc_paths():
        if any(part in SKIP_PARTS for part in path.parts):
            continue
        for target in _relative_targets(path):
            if not (path.parent / target).resolve().exists():
                missing.append(f"{path.relative_to(ROOT)} -> {target}")

    if missing:
        print("missing relative doc links/assets:")
        print("\n".join(missing))
        return 1
    print("relative links/assets ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
