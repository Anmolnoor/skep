"""The secret floor (ES-F1, edge sweep V3; B01 B02).

Credential files are never readable by the Queen or a worker. Like the git
floor (I4) this sits ABOVE every policy layer: it is not a rule, so no
template, learned rule, session grant or confirmed card can lift it. Both
the requested path and its realpath are judged, so a symlink cannot launder
a secret into an allowed directory. Case is folded on every OS: APFS and
NTFS open ``~/.SSH/id_rsa`` as ``~/.ssh/id_rsa``, and ``resolve()`` does not
normalize case, so a case-sensitive compare was a bypass (over-blocking
``.SSH`` on Linux is harmless and keeps one code path).

Policy half only. The sandbox half (tmpfs/deny over these paths, so a
worker's own ``cat ~/.ssh/id_rsa`` fails too) is ``sandbox.secret_paths``: it
masks HOME_SECRETS minus ``sandbox.SANDBOX_READABLE_SECRETS`` (engine logins
and ``~/.npmrc``), which stay on this floor.
"""

from __future__ import annotations

import os
from fnmatch import fnmatch
from glob import iglob
from itertools import islice
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
    # PR #64 review: the common stores the first list missed.
    ".secrets",
    ".config/gcloud",
    ".azure",
    ".npmrc",
    ".pypirc",
    ".claude/.credentials.json",
    ".codex/auth.json",
    "Library/Keychains",
    ".vault-token",
    ".terraform.d/credentials.tfrc.json",
    ".config/op",
    # B125: what a sandboxed worker could still read after #64. Shell rc and
    # history files routinely hold exported tokens and pasted secrets.
    ".cargo/credentials",
    ".cargo/credentials.toml",
    ".claude.json",
    ".password-store",
    ".bashrc",
    ".bash_profile",
    ".profile",
    ".zshrc",
    ".zshenv",
    ".zprofile",
    ".bash_history",
    ".zsh_history",
    ".python_history",
    ".node_repl_history",
    ".psql_history",
    ".mysql_history",
    ".sqlite_history",
)
# skep's own secrets, by file name, anywhere under the skep (supervisor) home:
# serve-token (auth.py), llm-secret / llm-secret-<id> (llm.py), and every
# ``<name>-secret`` — channel, webhook (channels.channel_secret_path) and
# mail (mcp_servers/mail.py). serve.env sits one level up, in the skep root.
SKEP_SECRET_NAMES: tuple[str, ...] = ("serve-token", "llm-secret*", "*-secret")
SERVE_ENV = "serve.env"


def _fold(path: Path) -> Path:
    return Path(str(path).casefold())


# macOS firmlinks the data volume's /Users, /home, ... onto /, and resolve()
# does not follow a firmlink: /System/Volumes/Data/Users/me/.ssh IS ~/.ssh.
_DATA_VOLUME = Path("/system/volumes/data")


def alias_key(path: Path) -> Path:
    """One spelling for every name APFS opens as ``path``: case folded, the
    macOS data-volume prefix dropped (over-matching on Linux is harmless)."""
    folded = _fold(path)
    if _DATA_VOLUME in folded.parents:
        return Path("/") / folded.relative_to(_DATA_VOLUME)
    return folded


def is_dotenv(name: str) -> bool:
    name = name.casefold()
    return name == ".env" or name.startswith(".env.")


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _reason(path: Path, user_homes: tuple[Path, ...], skep_homes: tuple[Path, ...]) -> str | None:
    folded = alias_key(path)
    if is_dotenv(folded.name):
        return f"{path} is a .env file"
    for home in user_homes:
        for rel in HOME_SECRETS:
            if _under(folded, alias_key(home / rel)):
                return f"{path} is under ~/{rel}"
    for home in skep_homes:
        if folded == alias_key(home.parent / SERVE_ENV) or (
            _under(folded, alias_key(home))
            and any(fnmatch(folded.name, pat) for pat in SKEP_SECRET_NAMES)
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


def shell_secret_floor_reason(
    command: str, *, cwd: str | None, skep_home: Path | None = None
) -> str | None:
    """ES-F8 (B73): the floor over a Queen host-shell line. Every argv token
    (also after ``=`` and a curl-style ``@``) is ``$VAR``/``~``-expanded,
    resolved against the command's cwd and globbed, then judged by
    ``secret_floor_reason``; so is the cwd itself.

    ponytail: static tokens only — a shell can build a path at run time
    (``cat $(printf ~/.s%s/id_rsa sh)``, ``cd ~ && cd .ssh``, a script that
    opens it). The real fix is sandboxing the Queen's shell (deny the floor
    paths like the worker sandbox will, B05) — out of scope here."""
    from .shell_prefixes import command_line_segments

    base = Path(cwd).expanduser() if cwd else Path.cwd()
    tokens = [token for argv in command_line_segments(command) or [] for token in argv]
    tokens += [token.strip("'\"") for token in command.split()]  # unparseable lines too
    candidates = [base]
    for token in tokens:
        if "://" in token:
            continue  # a URL names no local file
        for part in {token, token.partition("=")[2]}:
            part = part.removeprefix("@")
            if not part:
                continue
            path = base / Path(os.path.expandvars(part)).expanduser()
            candidates.append(path)
            if any(char in part for char in "*?["):
                candidates += [Path(hit) for hit in islice(iglob(str(path)), 200)]
    for candidate in candidates:
        reason = secret_floor_reason(candidate, skep_home=skep_home)
        if reason is not None:
            return reason
    return None
