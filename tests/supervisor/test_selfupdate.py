"""v130: skep announces a new PyPI release once and updates itself on /update.

No network and no real installer anywhere here: PyPI is a fake ``get``, the
installer a fake ``run``, and the restart a recorded callable."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from skep import __version__
from skep.supervisor import SupervisorConfig
from skep.supervisor.serve import selfupdate
from skep.supervisor.serve.selfupdate import (
    ANNOUNCED_VERSION,
    PYPI_URL,
    UPDATED_FROM,
    check_for_update,
    install_command,
    is_newer,
    parse_version,
    restart_report,
    run_update,
)

_MAJOR, _MINOR, _PATCH = (int(part) for part in __version__.split("."))
NEWER = f"{_MAJOR}.{_MINOR}.{_PATCH + 1}"
PIPX = Path("/home/alfin/.local/share/pipx/venvs/skep")
PYTHON = str(Path(sys.executable).resolve())
PIPX_INSTALL = ["pipx", "install", "--force", "--python", PYTHON]


class _Store:
    """The three store calls selfupdate makes."""

    def __init__(self, busy: list[int] | None = None) -> None:
        self.settings: dict[str, Any] = {}
        self._busy = busy or []

    def get_setting(self, key: str) -> Any:
        return self.settings.get(key)

    def set_setting(self, key: str, value: Any) -> None:
        self.settings[key] = value

    def runs_with_states(self, states: Any) -> list[object]:
        return [object()] * (self._busy.pop(0) if self._busy else 0)


def _pypi(version: str) -> Any:
    def get(url: str, timeout: float) -> httpx.Response:
        assert url == PYPI_URL
        return httpx.Response(
            200, json={"info": {"version": version}}, request=httpx.Request("GET", url)
        )

    return get


class _Runner:
    """Records argv; fails the commands whose first or last word is listed."""

    def __init__(self, fail: tuple[str, ...] = ()) -> None:
        self.calls: list[list[str]] = []
        self._fail = fail

    def __call__(self, argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        failed = argv[-1] in self._fail or Path(argv[0]).name in self._fail
        return subprocess.CompletedProcess(argv, 1 if failed else 0, "", "boom" if failed else "")


def _update(store: _Store, runner: _Runner, version: str = NEWER, prefix: Path = PIPX) -> Any:
    said: list[str] = []
    restarts: list[int] = []
    restarted = run_update(
        store,  # type: ignore[arg-type]
        said.append,
        lambda: restarts.append(1),
        get=_pypi(version),
        run=runner,
        sleep=lambda _: None,
        prefix=prefix,
    )
    return restarted, said, restarts


def test_only_a_plain_newer_version_counts() -> None:
    assert parse_version("1.5.0") == (1, 5, 0)
    assert parse_version("1.6.0rc1") is None and parse_version("1.6") is None
    assert is_newer("1.10.0", "1.9.9")
    assert not is_newer("1.5.0", "1.5.0") and not is_newer("1.4.9", "1.5.0")
    assert not is_newer("1.6.0rc1", "1.5.0")  # a pre-release is never offered


def test_installer_is_the_tool_that_owns_the_environment() -> None:
    # Found live: without --python, pipx rebuilt a 3.12 install on 3.14.
    assert install_command(PIPX) == ["pipx", "install", "--force", "--python", PYTHON]
    assert install_command(Path("/home/a/.local/share/uv/tools/skep")) == [
        "uv",
        "tool",
        "install",
        "--force",
        "--python",
        PYTHON,
    ]
    assert install_command(Path("/Users/a/Developer/skep/.venv")) is None


def test_a_new_release_is_announced_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(selfupdate, "install_command", lambda prefix=None: ["pipx"])
    store = _Store()
    said: list[str] = []
    assert check_for_update(store, said.append, get=_pypi(NEWER)) == NEWER  # type: ignore[arg-type]
    assert len(said) == 1 and NEWER in said[0] and "/update" in said[0]
    assert f"releases/tag/v{NEWER}" in said[0]
    assert store.settings[ANNOUNCED_VERSION] == NEWER
    # the next check, same release: silence
    assert check_for_update(store, said.append, get=_pypi(NEWER)) is None  # type: ignore[arg-type]
    assert check_for_update(store, said.append, get=_pypi(__version__)) is None  # type: ignore[arg-type]
    assert len(said) == 1


def test_a_copy_skep_cannot_replace_is_told_how_instead(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(selfupdate, "install_command", lambda prefix=None: None)
    said: list[str] = []
    check_for_update(_Store(), said.append, get=_pypi(NEWER))  # type: ignore[arg-type]
    assert "/update" not in said[0] and "the way you installed it" in said[0]


def test_update_installs_checks_and_restarts() -> None:
    store, runner = _Store(), _Runner()
    restarted, said, restarts = _update(store, runner)
    assert restarted and restarts == [1]
    assert runner.calls == [
        [*PIPX_INSTALL, f"skep=={NEWER}"],
        [str(PIPX / "bin" / "skep"), "--version"],  # the new copy starts
    ]
    assert store.settings[UPDATED_FROM] == __version__
    assert said[-1] == f"skep {NEWER} installed. Restarting now."


def test_update_waits_until_no_run_is_in_progress() -> None:
    store, runner = _Store(busy=[2, 1, 0]), _Runner()
    restarted, said, _ = _update(store, runner)
    assert restarted
    assert said[0] == f"skep {NEWER} will install once 2 run(s) in progress finish."
    assert sum("will install once" in line for line in said) == 1  # said once, not per poll


def test_a_failed_install_puts_the_old_copy_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(selfupdate, "current_spec", lambda: "git+https://x/skep.git@abc")
    store, runner = _Store(), _Runner(fail=("skep",))  # the new copy will not start
    restarted, said, restarts = _update(store, runner)
    assert not restarted and restarts == []
    assert runner.calls[-1] == [*PIPX_INSTALL, "git+https://x/skep.git@abc"]
    assert UPDATED_FROM not in store.settings
    assert "did not install (boom)" in said[-1] and "nothing changed" in said[-1]


def test_a_failed_put_back_names_the_command_to_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(selfupdate, "current_spec", lambda: "skep==1.0.0")
    runner = _Runner(fail=(f"skep=={NEWER}", "skep==1.0.0"))
    restarted, said, _ = _update(_Store(), runner)
    assert not restarted
    assert said[-1].endswith(f"run: {' '.join(PIPX_INSTALL)} skep==1.0.0")


def test_update_refuses_what_it_cannot_or_need_not_do() -> None:
    runner = _Runner()
    restarted, said, _ = _update(_Store(), runner, prefix=Path("/src/skep/.venv"))
    assert not restarted and "can't update itself here" in said[0]
    restarted, said, _ = _update(_Store(), runner, version=__version__)
    assert not restarted and said == [f"skep {__version__} is already the newest release."]
    assert runner.calls == []


def test_the_restarted_serve_reports_once() -> None:
    store = _Store()
    assert restart_report(store) is None  # type: ignore[arg-type]
    store.settings[UPDATED_FROM] = "1.0.0"
    assert restart_report(store) == f"skep updated: 1.0.0 → {__version__}."  # type: ignore[arg-type]
    assert restart_report(store) is None  # type: ignore[arg-type]


def test_a_restart_request_re_executes_serve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor.serve import serve_cmds

    def fake_run(self: uvicorn.Server, sockets: object = None) -> None:
        self.started = True
        app: Any = self.config.app
        app.state.request_restart()
        assert self.should_exit

    execs: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(uvicorn.Server, "run", fake_run)
    monkeypatch.setattr(os, "execv", lambda path, argv: execs.append((path, argv)))
    monkeypatch.setattr(sys, "argv", ["skep", "serve", "--port", "8799"])
    args = argparse.Namespace(
        home=tmp_path, worker_cmd=None, auto_approve=False, host="127.0.0.1", port=8799
    )
    serve_cmds.cmd_serve(args)
    exe = sys.executable
    assert execs == [(exe, [exe, "-m", "skep", "serve", "--port", "8799"])]


# -- Telegram /update -------------------------------------------------------


def _telegram(config: SupervisorConfig, *, can_confirm: bool) -> Any:
    from .conftest import serve_client
    from .test_channels_live import _TelegramHarness

    serve_client(config).put(
        "/api/channels/telegram",
        json={
            "enabled": True,
            "allowed_identities": ["42"],
            "allowed_users": ["42"],
            "secret": "bot-token",
            "channel_can_confirm": can_confirm,
        },
    )
    return _TelegramHarness(config)


def _message(text: str) -> dict[str, object]:
    return {"update_id": 5, "message": {"chat": {"id": 42}, "from": {"id": 42}, "text": text}}


def test_telegram_update_needs_channel_can_confirm(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[object] = []
    monkeypatch.setattr(selfupdate, "start_update", lambda *a: started.append(a))
    harness = _telegram(config, can_confirm=False)
    try:
        harness.updates.append([_message("/update")])
        assert harness.poller.poll_once() == 1
        assert started == []
        assert "needs channel_can_confirm" in harness.sent[0][2]
    finally:
        harness.close()


def test_telegram_update_starts_the_update_without_a_model_turn(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[tuple[Any, ...]] = []
    monkeypatch.setattr(selfupdate, "start_update", lambda *a: started.append(a))
    harness = _telegram(config, can_confirm=True)
    restarts: list[int] = []
    harness.poller._request_restart = lambda: restarts.append(1)
    try:
        harness.updates.append([_message("/update@Alfinskepbot")])
        assert harness.poller.poll_once() == 1
        assert len(started) == 1  # no LLM is configured: a model turn would have failed
        _, say, restart = started[0]
        say("hello")
        restart()
        assert harness.sent == [("bot-token", "42", "hello")] and restarts == [1]
        assert harness.store.channel_session("telegram:42") is None  # no chat turn ran
    finally:
        harness.close()


# -- the ticker's release check ---------------------------------------------


def test_ticker_checks_every_interval_and_zero_turns_it_off(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import RunStore
    from skep.supervisor.serve.settings import ConfigHolder
    from skep.supervisor.serve.ticker import Ticker

    checks: list[int] = []

    def fake_check(store: Any, announce: Any) -> None:
        checks.append(1)
        announce("skep 9.9.9 is out")

    monkeypatch.setattr(selfupdate, "check_for_update", fake_check)
    store = RunStore(config.db_path)
    try:
        ticker = Ticker(ConfigHolder(config, store), store)
        ticker._check_for_update()
        ticker._check_for_update()  # inside the 12 h interval: no second request
        assert checks == [1]
        # no messenger chat yet: the announcement is a durable note
        assert [note.content for note in store.list_notes()] == ["skep 9.9.9 is out"]
        store.set_setting(selfupdate.UPDATE_CHECK_INTERVAL_SECONDS, 0)
        ticker._last_update_check = 0.0
        ticker._check_for_update()
        assert checks == [1]
    finally:
        store.close()


# -- the edges: what reinstalls, a missing installer, one update at a time ---


class _Dist:
    def __init__(self, text: str | None) -> None:
        self._text = text

    def read_text(self, name: str) -> str | None:
        assert name == "direct_url.json"
        return self._text


def test_the_put_back_spec_is_the_running_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    git = json.dumps(
        {"url": "https://github.com/x/skep.git", "vcs_info": {"vcs": "git", "commit_id": "c0ffee"}}
    )
    monkeypatch.setattr(metadata, "distribution", lambda name: _Dist(git))
    assert selfupdate.current_spec() == "git+https://github.com/x/skep.git@c0ffee"
    monkeypatch.setattr(metadata, "distribution", lambda name: _Dist(None))
    assert selfupdate.current_spec() == f"skep=={__version__}"  # from PyPI

    def missing(name: str) -> None:
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, "distribution", missing)
    assert selfupdate.current_spec() == f"skep=={__version__}"


def test_a_missing_installer_is_a_named_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(selfupdate, "current_spec", lambda: "skep==1.0.0")

    def no_pipx(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    restarted, said, _ = _update(_Store(), no_pipx)  # type: ignore[arg-type]
    assert not restarted and "No such file or directory" in said[-1]


def test_only_one_update_runs_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    release, finished = threading.Event(), threading.Event()

    def slow_update(store: Any, say: Any, restart: Any) -> None:
        release.wait(5)
        finished.set()
        raise RuntimeError("PyPI is down")

    monkeypatch.setattr(selfupdate, "run_update", slow_update)
    said: list[str] = []
    assert selfupdate.start_update(_Store(), said.append, lambda: None)  # type: ignore[arg-type]
    assert not selfupdate.start_update(_Store(), said.append, lambda: None)  # type: ignore[arg-type]
    assert said == ["An update is already under way."]
    release.set()
    assert finished.wait(5)
    for _ in range(50):  # the thread reports, then frees the lock
        if not selfupdate._UPDATE_LOCK.locked():
            break
        threading.Event().wait(0.05)
    assert said[-1] == "skep update failed: PyPI is down"
    assert not selfupdate._UPDATE_LOCK.locked()


def test_a_restart_on_the_same_version_says_so() -> None:
    store = _Store()
    store.settings[UPDATED_FROM] = __version__
    assert "still reports" in str(restart_report(store))  # type: ignore[arg-type]


def test_telegram_update_refuses_without_a_server_to_restart(config: SupervisorConfig) -> None:
    harness = _telegram(config, can_confirm=True)  # the harness has no request_restart
    try:
        harness.updates.append([_message("/update")])
        assert harness.poller.poll_once() == 1
        assert "can't restart itself" in harness.sent[0][2]
    finally:
        harness.close()


def test_the_update_lines_reach_the_messenger_chat(config: SupervisorConfig) -> None:
    from skep.supervisor import RunStore
    from skep.supervisor.serve.settings import ConfigHolder
    from skep.supervisor.serve.ticker import Ticker

    pushed: list[tuple[str, str, str]] = []

    class _Pushing(Ticker):
        def _notify(self, chat_id: str, text: str, kind: str = "info") -> None:
            pushed.append((chat_id, text, kind))

    store = RunStore(config.db_path)
    try:
        chat = store.create_chat(title="dm", model=None)
        store.bind_channel_session(
            session_key="telegram:42", channel="telegram", identity_id="42", chat_id=chat.chat_id
        )
        store.set_setting(UPDATED_FROM, "1.0.0")
        ticker = _Pushing(ConfigHolder(config, store), store)
        ticker._stop_event.set()  # run() reports at start, then the loop exits at once
        ticker.run()
        line = f"skep updated: 1.0.0 → {__version__}."
        assert pushed == [(chat.chat_id, line, "action_needed")]
        assert store.chat_messages(chat.chat_id)[-1].content == line
    finally:
        store.close()
