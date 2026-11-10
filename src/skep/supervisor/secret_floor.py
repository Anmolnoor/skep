"""The secret floor (ES-F1, edge sweep V3; B01 B02).

Credential files are never readable by the Queen or a worker. Like the git
floor (I4) this sits ABOVE every policy layer: it is not a rule, so no
template, learned rule, session grant or confirmed card can lift it. Both
the requested path and its realpath are judged, so a symlink cannot launder
a secret into an allowed directory.

Policy half only. The sandbox half (tmpfs/deny over these paths, so a
worker's own ``cat ~/.ssh/id_rsa`` fails too) is the sandbox round's (B05).
"""

from __future__ import annotations

import os
from fnmatch import fnmatch
from pathlib import Path

# Relative to the user's home: a file, or a directory and everything under it.
HOME_SECRETS: tuple[str, ...] = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".config/gh",
    ".netrc",
    ".git-credentials",
    ".docker/config.json",
    ".kube",
)
# skep's own secrets, by file name, anywhere under the skep (supervisor) home:
# serve-token (auth.py), llm-secret / llm-secret-<id> (llm.py), and every
# ``<name>-secret`` — channel, webhook (channels.channel_secret_path) and
# mail (mcp_servers/mail.py). serve.env sits one level up, in the skep root.
SKEP_SECRET_NAMES: tuple[str, ...] = ("serve-token", "llm-secret*", "*-secret")
SERVE_ENV = "serve.env"


def _is_dotenv(name: str) -> bool:
    return name == ".env" or name.startswith(".env.")


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _reason(path: Path, user_homes: tuple[Path, ...], skep_homes: tuple[Path, ...]) -> str | None:
    if _is_dotenv(path.name):
        return f"{path} is a .env file"
    for home in user_homes:
        for rel in HOME_SECRETS:
            if _under(path, home / rel):
                return f"{path} is under ~/{rel}"
    for home in skep_homes:
        if path == home.parent / SERVE_ENV or (
            _under(path, home) and any(fnmatch(path.name, pat) for pat in SKEP_SECRET_NAMES)
        ):
            return f"{path} is one of skep's own secrets"
    return None


def secret_floor_reason(path: str | Path, *, skep_home: Path | None = None) -> str | None:
    """Why ``path`` is never readable, or None. Judges the requested path
    (``~`` expanded, made absolute without following links) and its realpath."""
    requested = Path(os.path.abspath(Path(path).expanduser()))
    try:
        real = requested.resolve()
    except (OSError, RuntimeError):  # pragma: no cover - a loop resolves nowhere
        real = requested
    user_home = Path(os.path.abspath(Path.home()))
    user_homes = tuple({user_home, user_home.resolve()})
    skep_homes: tuple[Path, ...] = ()
    if skep_home is not None:
        skep_abs = Path(os.path.abspath(skep_home.expanduser()))
        skep_homes = tuple({skep_abs, skep_abs.resolve()})
    for candidate in (requested, real):
        reason = _reason(candidate, user_homes, skep_homes)
        if reason is not None:
            return (
                f"{reason} — credential files are never readable by the Queen or a "
                "worker (the secret floor; no rule, grant or card lifts it)"
            )
    return None
