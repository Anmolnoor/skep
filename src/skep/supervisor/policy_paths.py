"""B1-2 (ADR 0054 D3/D5, A8): path containment shared by the hook and the ACP mapper.

Decides whether a path is the run contract's symbolic ``workspace``; any
doubt keeps the literal path, so only a trusted-root rule can admit it.
"""

from __future__ import annotations

from pathlib import Path


def workspace_value(path: str, cwd: str) -> str:
    """``"workspace"`` iff ``path``'s real location is inside ``cwd``'s; else ``path``.

    A relative path anchors on ``cwd``. ``Path.resolve`` realpaths the existing
    ancestors and appends the missing suffix, so a symlink out of the checkout
    stays literal. Fails closed: a ``cwd`` that is not a directory contains
    nothing (before B1-2 a missing ``cwd`` still contained its own subpaths).
    """
    if not path or not cwd:
        return path or "workspace"
    try:
        root = Path(cwd).resolve()
        if not root.is_dir():
            return path
        real = Path(cwd, path).resolve()
    except (OSError, RuntimeError, ValueError):
        return path
    return "workspace" if real.is_relative_to(root) else path
