"""v130: skep tells the operator about a new release and updates itself on /update.

The ticker asks PyPI every 12 hours and announces each newer version ONCE in
the newest messenger chat.
Nothing installs on its own: the operator's typed ``/update`` is the decision
(I7). The update waits until no run is in flight, installs with the tool that
installed skep (pipx or uv tool), checks the new copy starts, puts the old copy
back if it does not, and restarts serve on the new code.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from importlib import metadata
from pathlib import Path
from typing import Any

import httpx

from ... import __version__
from ..store import RunStore

logger = logging.getLogger("skep.serve")

PYPI_URL = "https://pypi.org/pypi/skep/json"
RELEASE_URL = "https://github.com/Anmolnoor/skep/releases/tag/v{version}"
UPDATE_CHECK_INTERVAL = 12 * 60 * 60
ANNOUNCED_VERSION = "update_announced_version"
# The version a restart came from, so the new serve can say it updated.
UPDATED_FROM = "update_restarted_from"
# Same "running now" states as the dashboard and active_run_workspaces.
ACTIVE_STATES = ("created", "dispatched", "running")
IDLE_POLL_SECONDS = 60
INSTALL_TIMEOUT_SECONDS = 600

_UPDATE_LOCK = threading.Lock()

Say = Callable[[str], None]
Get = Callable[..., httpx.Response]
Run = Callable[..., "subprocess.CompletedProcess[str]"]


def parse_version(text: str) -> tuple[int, int, int] | None:
    """Plain X.Y.Z only, so a pre-release is never offered."""
    parts = text.strip().split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    return int(parts[0]), int(parts[1]), int(parts[2])


def is_newer(candidate: str, current: str = __version__) -> bool:
    new, old = parse_version(candidate), parse_version(current)
    return new is not None and old is not None and new > old


def latest_release(get: Get = httpx.get) -> str:
    response = get(PYPI_URL, timeout=20)
    response.raise_for_status()
    return str(response.json()["info"]["version"])


def install_command(prefix: Path | None = None) -> list[str] | None:
    """The installer that owns this environment, or None (a git checkout, a
    plain venv, a container): skep never guesses how to replace itself. The
    reinstall keeps this interpreter; left alone, pipx would rebuild the
    environment on its own default Python."""
    parts = (prefix or Path(sys.prefix)).parts
    python = str(Path(sys.executable).resolve())
    if "pipx" in parts and "venvs" in parts:
        return ["pipx", "install", "--force", "--python", python]
    if "uv" in parts and "tools" in parts:
        return ["uv", "tool", "install", "--force", "--python", python]
    return None


def current_spec() -> str:
    """What reinstalls the running copy: its git commit when it came from git
    (PEP 610 direct_url.json), else its version on PyPI."""
    try:
        raw = metadata.distribution("skep").read_text("direct_url.json")
        info: dict[str, Any] = json.loads(raw) if raw else {}
    except (metadata.PackageNotFoundError, ValueError):
        info = {}
    vcs = info.get("vcs_info") or {}
    if vcs.get("vcs") == "git" and vcs.get("commit_id") and info.get("url"):
        return f"git+{info['url']}@{vcs['commit_id']}"
    return f"skep=={__version__}"


def check_for_update(store: RunStore, announce: Say, *, get: Get = httpx.get) -> str | None:
    """Announce a newer release once per version; returns the version announced."""
    latest = latest_release(get)
    if not is_newer(latest) or store.get_setting(ANNOUNCED_VERSION) == latest:
        return None
    if install_command() is not None:
        how = "send /update on Telegram"
    else:
        how = "update it the way you installed it (this copy is not a pipx or uv tool install)"
    notes = RELEASE_URL.format(version=latest)
    announce(f"skep {latest} is out (this is {__version__}). Notes: {notes}. To update, {how}.")
    store.set_setting(ANNOUNCED_VERSION, latest)
    return latest


def _run(run: Run, argv: list[str], expect: str = "") -> str | None:
    """None when the command succeeds (and its output starts with ``expect``),
    else the tail of what it printed."""
    try:
        done = run(
            argv, capture_output=True, text=True, timeout=INSTALL_TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:  # ValueError: undecodable
        return str(exc)
    if done.returncode == 0 and done.stdout.startswith(expect):
        return None
    return (done.stderr or done.stdout or f"exit {done.returncode}").strip()[-500:]


def run_update(
    store: RunStore,
    say: Say,
    request_restart: Callable[[], None],
    *,
    get: Get = httpx.get,
    run: Run = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
    prefix: Path | None = None,
) -> bool:
    """Install the newest release and restart; True when a restart was requested."""
    prefix = prefix or Path(sys.prefix)
    installer = install_command(prefix)
    if installer is None:
        say(
            f"skep can't update itself here: {prefix} is not a pipx or uv tool install. "
            "Update it the way you installed it."
        )
        return False
    latest = latest_release(get)
    if not is_newer(latest):
        say(f"skep {__version__} is already the newest release.")
        return False
    waited = False
    while busy := len(store.runs_with_states(ACTIVE_STATES)):
        if not waited:
            say(f"skep {latest} will install once {busy} run(s) in progress finish.")
            waited = True
        sleep(IDLE_POLL_SECONDS)
    previous = current_spec()
    say(f"Installing skep {latest}…")
    failure = _run(run, [*installer, f"skep=={latest}"]) or _run(
        run, [str(prefix / "bin" / "skep"), "--version"], expect=f"skep {latest} "
    )
    if failure is None:
        store.set_setting(UPDATED_FROM, __version__)
        say(f"skep {latest} installed. Restarting now.")
        request_restart()
        return True
    undo = _run(run, [*installer, previous])
    if undo is None:
        say(f"skep {latest} did not install ({failure}). Put {__version__} back; nothing changed.")
    else:
        say(
            f"skep {latest} did not install ({failure}), and putting {__version__} back failed "
            f"too ({undo}). This serve keeps running, but before the next start run: "
            f"{' '.join(installer)} {previous}"
        )
    return False


def start_update(store: RunStore, say: Say, request_restart: Callable[[], None]) -> bool:
    """Run one update on its own thread; False when one is already under way."""
    if not _UPDATE_LOCK.acquire(blocking=False):
        say("An update is already under way.")
        return False

    def _update() -> None:
        try:
            run_update(store, say, request_restart)
        except Exception as exc:  # the operator asked, so the operator hears why
            logger.exception("self-update failed")
            say(f"skep update failed: {exc}")
        finally:
            _UPDATE_LOCK.release()

    threading.Thread(target=_update, name="serve-self-update", daemon=True).start()
    return True


def restart_report(store: RunStore) -> str | None:
    """After a self-update restart: the line saying what this serve now runs."""
    previous = store.get_setting(UPDATED_FROM)
    if previous is None:
        return None
    store.set_setting(UPDATED_FROM, None)
    if previous == __version__:
        return f"skep restarted after the update but still reports {__version__}."
    return f"skep updated: {previous} → {__version__}."
