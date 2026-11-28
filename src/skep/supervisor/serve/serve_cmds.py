"""``skep serve`` — the long-running API daemon (v5 Stage A)."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import MutableMapping
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import IO, Any

# v70-F2: the log file rides the operator home, size-bounded — a daemon
# launched from any shell leaves a trail (the 2026-07-20 field test had to be
# reconstructed from the store alone because nothing redirected stdout).
SERVE_LOG_FILE = "serve.log"
SERVE_LOG_MAX_BYTES = 2_000_000
SERVE_LOG_BACKUPS = 2
# v111-F1: the operator's env file beside the log, loaded at startup.
SERVE_ENV_FILE = "serve.env"
# One serve per home: two supervisors over one store double-resume follow-ups
# and re-adopt each other's workers.
SERVE_LOCK_FILE = "serve.lock"
# v130: _serve's return code when a self-update asked for a restart.
RESTART_AFTER_UPDATE = -1


def take_serve_lock(home: Path, port: int) -> tuple[IO[str] | None, str | None]:
    """An exclusive flock on ``<home>/serve.lock`` for the life of this serve:
    ``(handle, None)`` — keep the handle open — or ``(None, holder)`` naming
    the serve that holds it. The OS drops the lock when its holder dies, so
    there is no stale pid to judge; the pid and port written inside are only
    for the refusal to name."""
    import fcntl

    home.mkdir(parents=True, exist_ok=True)
    handle = (home / SERVE_LOCK_FILE).open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.seek(0)
        holder = handle.read().strip() or "an unknown pid"
        handle.close()
        return None, holder
    handle.truncate(0)
    handle.write(f"pid {os.getpid()}, port {port}\n")
    handle.flush()
    return handle, None


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
    # L12 (I8): skep's own loggers (startup re-verify sweep, recovery, channels)
    # had no handler — INFO was dropped, ERROR went only to stderr, which a
    # detached serve discards. Each skep.* logger already redacts at the source
    # (ES-F281 RedactFilter), so its records reach these handlers masked.
    # Propagation stays on: the root has no handler under serve, and pytest's
    # caplog listens there.
    config["loggers"]["skep"] = {"handlers": ["default", "serve_file"], "level": "INFO"}
    return config


def write_boot_banner(log_path: Path, banner: str) -> None:
    """Append the boot banner to the serve log (stdout keeps it too).

    The access token is deliberately NOT part of the banner written here —
    the log file must never become a second credential store; the token goes
    to stdout, and only when stdout is a terminal (``access_token_line``).
    """
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(banner + "\n")


def access_token_line(home: Path, interactive: bool) -> str:
    """The boot line that hands the operator the access token.

    Only a terminal gets the value. Off one, stdout IS a log (the systemd
    journal, ``docker logs``, a ``>> serve.log`` redirect) and the log is not
    a second credential store, so the line names the 0600 file instead.
    """
    from .auth import TOKEN_FILE, ensure_token

    token = ensure_token(home)  # mint on first boot either way
    if interactive:
        return f"  access token: {token}"
    return f"  access token: in {home / TOKEN_FILE} (not printed: stdout is not a terminal)"


def cmd_serve(args: argparse.Namespace) -> int:
    # Lazy imports: registering the parser must not pay the FastAPI import cost
    # for every other CLI invocation.
    from ..cli_cmds import build_config, note_retired_auto_approve

    # v111-F1: before anything reads the process env — engine auth rides it.
    loaded_env = load_serve_env(args.home.expanduser().resolve() / SERVE_ENV_FILE)
    note_retired_auto_approve(args)
    config = build_config(args.home, args.worker_cmd)
    # ADR 0060 §6: the kill switch for restart re-adoption — the flag, or
    # SKEP_READOPT_WORKERS=0 in the environment or serve.env (loaded above,
    # read by build_config).
    if getattr(args, "no_readopt", False):
        config = replace(config, readopt_workers=False)
    lock, holder = take_serve_lock(config.home, args.port)
    if lock is None:
        print(
            f"error: another skep serve already runs on this home ({holder}; home: "
            f"{config.home}). Two supervisors over one store resume the same follow-ups "
            "and re-adopt the same workers twice. Use that serve, stop it first, or start "
            "this one with a different --home.",
            file=sys.stderr,
        )
        return 1
    try:
        code = _serve(args, config, loaded_env)
    finally:
        lock.close()  # the OS would release it anyway; a test's next serve needs it now
    if code == RESTART_AFTER_UPDATE:
        # v130: the freshly installed code takes over this process. Same pid,
        # so systemd (or the shell that started it) keeps tracking it.
        os.execv(sys.executable, [sys.executable, "-m", "skep", *sys.argv[1:]])
    return code


def _serve(args: argparse.Namespace, config: Any, loaded_env: list[str]) -> int:
    from ..netproxy import record_serve_port
    from .app import create_app

    # v26-F3: channel confirm-pointers name the UI the operator actually runs.
    app = create_app(config, web_ui_url=f"http://{args.host}:{args.port}/")
    banner = f"skep serve: http://{args.host}:{args.port}  (home: {config.home})"
    print(banner, flush=True)
    if loaded_env:
        # Names only, stdout only — the log file is not a second credential
        # store and neither is this line.
        print(f"  serve.env: loaded {', '.join(loaded_env)}", flush=True)
    # flush, or block-buffered stdout holds the line hostage in container logs.
    print(access_token_line(config.home, sys.stdout.isatty()), flush=True)
    log_path = config.home.parent / SERVE_LOG_FILE
    write_boot_banner(log_path, banner)
    server = make_server(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
        log_config=serve_log_config(log_path),
    )
    # ES-F6 (B05) + #58 ES-F4: every run's proxy and every Seatbelt profile for
    # this home refuse this port on loopback (both read <home>/serve-port).
    # ADR 0069 review r2 N8: the port it LISTENS on (--port 0 picks one), bound
    # and recorded before the app starts, so no MCP work runs before it is known.
    listener = server.config.bind_socket()
    record_serve_port(config.home, listener.getsockname()[1])

    def request_restart() -> None:
        # v130: a graceful shutdown (lifespan stops the ticker and channels),
        # then cmd_serve re-executes into the new install.
        app.state.restart_requested = True
        server.should_exit = True

    app.state.request_restart = request_restart
    try:
        server.run(sockets=[listener])
    finally:
        listener.close()
    if getattr(app.state, "restart_requested", False):
        return RESTART_AFTER_UPDATE
    return 0 if server.started else 3  # uvicorn.run's startup-failure code


# ES B119: how long shutdown waits for in-flight requests before uvicorn
# cancels them. A hung chat turn held SIGTERM for 139 s; streaming turns
# resume from the store, so a short grace loses nothing durable.
SHUTDOWN_GRACE_SECONDS = 10


def make_server(app: Any, **config: Any) -> Any:
    """``uvicorn.run`` with two shutdown changes (ES B119): the FIRST signal
    halts the ticker before uvicorn drains requests (it kept firing schedules
    through the drain), and the drain is bounded by SHUTDOWN_GRACE_SECONDS."""
    import uvicorn

    class _Server(uvicorn.Server):
        def handle_exit(self, sig: int, frame: Any) -> None:
            ticker = getattr(app.state, "ticker", None)
            if ticker is not None:
                ticker.halt()
            super().handle_exit(sig, frame)

    return _Server(uvicorn.Config(app, timeout_graceful_shutdown=SHUTDOWN_GRACE_SECONDS, **config))


def register_serve_command(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    from ..cli_cmds import AUTO_APPROVE_HELP  # loaded already: it builds this parser

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
        help=AUTO_APPROVE_HELP,
    )
    serve.add_argument(
        "--no-readopt",
        action="store_true",
        help="kill workers still running from a previous serve instead of re-adopting "
        "them (ADR 0060); also SKEP_READOPT_WORKERS=0",
    )
    serve.set_defaults(func=cmd_serve)
