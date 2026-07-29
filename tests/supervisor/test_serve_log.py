"""v70-F2: the daemon leaves a trail regardless of the launch shell.

The 2026-07-20 field test had to be reconstructed from the store alone —
``skep serve`` logged only to stdout and the launch shell redirected nothing.
The serve log now always exists at ``<home>/serve.log``.
"""

from __future__ import annotations

import logging
import logging.config
from pathlib import Path

from skep.supervisor.serve.serve_cmds import (
    SERVE_LOG_BACKUPS,
    SERVE_LOG_MAX_BYTES,
    serve_log_config,
    write_boot_banner,
)


def _detach_serve_handlers() -> None:
    """Undo the dictConfig side effects so no test leaks an open handler."""
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)


def test_serve_log_config_tees_uvicorn_loggers_to_the_home_file(tmp_path: Path) -> None:
    log_path = tmp_path / "serve.log"
    config = serve_log_config(log_path)

    # One shared, size-bounded handler id on both loggers — a single file
    # handle owns rotation (two instances on one file would fight rollover).
    handler = config["handlers"]["serve_file"]
    assert handler["filename"] == str(log_path)
    assert handler["maxBytes"] == SERVE_LOG_MAX_BYTES > 0
    assert handler["backupCount"] == SERVE_LOG_BACKUPS >= 1
    assert "serve_file" in config["loggers"]["uvicorn"]["handlers"]
    assert "serve_file" in config["loggers"]["uvicorn.access"]["handlers"]

    logging.config.dictConfig(config)
    try:
        # uvicorn.error propagates to uvicorn; access logs land directly.
        logging.getLogger("uvicorn.error").info("boot line %s", "alpha")
        logging.getLogger("uvicorn.access").info(
            '%s - "%s %s HTTP/%s" %d', "127.0.0.1", "GET", "/api/status", "1.1", 200
        )
    finally:
        _detach_serve_handlers()

    text = log_path.read_text(encoding="utf-8")
    assert "boot line alpha" in text
    assert "/api/status" in text


def test_boot_banner_lands_in_the_log_and_never_the_token(tmp_path: Path) -> None:
    log_path = tmp_path / "serve.log"
    banner = "skep serve: http://127.0.0.1:8765  (home: /tmp/home/supervisor)"
    write_boot_banner(log_path, banner)
    write_boot_banner(log_path, banner)  # a restart appends, never truncates

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert lines == [banner, banner]
    # The token is stdout-only — the log file is not a second credential store.
    assert "access token" not in log_path.read_text(encoding="utf-8")
