"""ES-F281: skep's loggers never write a credential the shared redact knows."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

import skep

_SECRET = "".join(["AAfake", "_not_a_real_bot_token", "0" * 12])
_URL = f"https://api.telegram.org/bot123456789:{_SECRET}/getUpdates"


def _skep_logger_names() -> set[str]:
    root = Path(skep.__file__).parent
    found: set[str] = set()
    for path in root.rglob("*.py"):
        found.update(re.findall(r"getLogger\(\"(skep[.\w]*)\"\)", path.read_text()))
    return found


def test_every_skep_logger_is_covered() -> None:
    from skep.supervisor.policy_v3 import SKEP_LOGGERS

    assert "skep.serve" in _skep_logger_names()
    assert _skep_logger_names() <= set(SKEP_LOGGERS)


@pytest.mark.parametrize("name", sorted(_skep_logger_names()))
def test_a_logged_error_carrying_a_bot_token_is_masked(
    name: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed Telegram poll logged the httpx error, whose text holds the
    request URL — and Telegram's URL holds the bot token. Message args and
    the traceback both pass through the shared redact."""
    import skep.supervisor  # noqa: F401  (installs the filter)

    logger = logging.getLogger(name)
    with caplog.at_level(logging.DEBUG, logger=name):
        logger.warning("telegram delivery failed: %s", f"Client error for url '{_URL}'")
        try:
            raise RuntimeError(f"Server error '502' for url '{_URL}'")
        except RuntimeError:
            logger.exception("telegram poll failed")
    assert _SECRET not in caplog.text, caplog.text
    assert "bot123456789:***" in caplog.text
