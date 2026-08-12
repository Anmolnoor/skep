"""``skep serve`` — the long-running API daemon (v5 Stage A)."""

from __future__ import annotations

import argparse
import os
from collections.abc import MutableMapping
from copy import deepcopy
from pathlib import Path
from typing import Any

# v70-F2: the log file rides the operator home, size-bounded — a daemon
# launched from any shell leaves a trail (the 2026-07-20 field test had to be
# reconstructed from the store alone because nothing redirected stdout).
SERVE_LOG_FILE = "serve.log"
SERVE_LOG_MAX_BYTES = 2_000_000
SERVE_LOG_BACKUPS = 2
# v111-F1: the operator's env file beside the log, loaded at startup.
SERVE_ENV_FILE = "serve.env"


def load_serve_env(path: Path, environ: MutableMapping[str, str] = os.environ) -> list[str]:
    """Load ``KEY=VALUE`` lines from ``<home>/serve.env`` into the process env.

    v111-F1: the file predates any code reading it — engine auth
    (``CLAUDE_CODE_OAUTH_TOKEN``) sat in ``~/.skep/serve.env`` while every
    bare restart silently shed it and claude_code dispatches died on
    "Not logged in". The restart ritual was the only carrier; now the daemon
    carries it itself. An explicit export outranks the file (existing vars are
    never overridden), a missing file is a no-op, and values never reach any
    log or banner (the odysseus pattern) — only the loaded NAMES are returned
    for stdout.
    """
    if not path.is_file():
        return []
    loaded: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not key or key in environ:
            continue
        environ[key] = value
        loaded.append(key)
    return loaded


def serve_log_config(log_path: Path) -> dict[str, Any]:
    """Uvicorn's own log config plus ONE shared rotating file handler.

    Merged (not attached beforehand) because ``uvicorn.run`` applies its
    dictConfig at startup, which would wipe handlers added earlier. The single
    handler id is referenced by both the ``uvicorn`` and ``uvicorn.access``
    loggers so exactly one file handle rotates the file.
    """
    import uvicorn

    config: dict[str, Any] = deepcopy(uvicorn.config.LOGGING_CONFIG)
    config["handlers"]["serve_file"] = {
        "class": "logging.handlers.RotatingFileHandler",
        "filename": str(log_path),
        "maxBytes": SERVE_LOG_MAX_BYTES,
        "backupCount": SERVE_LOG_BACKUPS,
        "formatter": "default",
    }
    for name in ("uvicorn", "uvicorn.access"):
        config["loggers"][name]["handlers"].append("serve_file")
    return config


def write_boot_banner(log_path: Path, banner: str) -> None:
    """Append the boot banner to the serve log (stdout keeps it too).

    The access token is deliberately NOT part of the banner written here —
    the log file must never become a second credential store; the token stays
    stdout-only (the odysseus pattern).
    """
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(banner + "\n")


def cmd_serve(args: argparse.Namespace) -> int:
    # Lazy imports: registering the parser must not pay the FastAPI import cost
    # for every other CLI invocation.
    import uvicorn

    from ..cli_cmds import build_config
    from .app import create_app
    from .auth import ensure_token

    # v111-F1: before anything reads the process env — engine auth rides it.
    loaded_env = load_serve_env(args.home.expanduser().resolve() / SERVE_ENV_FILE)
    config = build_config(args.home, args.worker_cmd, auto_approve=args.auto_approve)
    # v26-F3: channel confirm-pointers name the UI the operator actually runs.
    app = create_app(config, web_ui_url=f"http://{args.host}:{args.port}/")
    banner = f"skep serve: http://{args.host}:{args.port}  (home: {config.home})"
    print(banner, flush=True)
    if loaded_env:
        # Names only, stdout only — the log file is not a second credential
        # store and neither is this line.
        print(f"  serve.env: loaded {', '.join(loaded_env)}", flush=True)
    # The odysseus pattern: the token reaches the operator via the boot log.
    # flush, or block-buffered stdout holds the token hostage in container logs.
    print(f"  access token: {ensure_token(config.home)}", flush=True)
    log_path = config.home.parent / SERVE_LOG_FILE
    write_boot_banner(log_path, banner)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
        log_config=serve_log_config(log_path),
    )
    return 0


def register_serve_command(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    serve = subcommands.add_parser("serve", help="run the HTTP API daemon (v5)")
    serve.add_argument("--host", default="127.0.0.1", help="HTTP bind host")
    serve.add_argument("--port", type=int, default=8765, help="HTTP bind port")
    serve.add_argument(
        "--worker-cmd",
        default=None,
        help="worker argv prefix (default: $SKEP_WORKER_CMD or skep's minimal coding worker)",
    )
    serve.add_argument(
        "--auto-approve",
        action="store_true",
        help="activate D3: auto-apply safe manifest-only fixes (U1)",
    )
    serve.set_defaults(func=cmd_serve)
