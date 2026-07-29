"""``skep serve`` — the long-running API daemon (v5 Stage A)."""

from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
from typing import Any

# v70-F2: the log file rides the operator home, size-bounded — a daemon
# launched from any shell leaves a trail (the 2026-07-20 field test had to be
# reconstructed from the store alone because nothing redirected stdout).
SERVE_LOG_FILE = "serve.log"
SERVE_LOG_MAX_BYTES = 2_000_000
SERVE_LOG_BACKUPS = 2


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

    config = build_config(args.home, args.worker_cmd, auto_approve=args.auto_approve)
    # v26-F3: channel confirm-pointers name the UI the operator actually runs.
    app = create_app(config, web_ui_url=f"http://{args.host}:{args.port}/")
    banner = f"skep serve: http://{args.host}:{args.port}  (home: {config.home})"
    print(banner, flush=True)
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
