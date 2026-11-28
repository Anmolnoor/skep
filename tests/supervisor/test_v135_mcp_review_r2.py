"""v135 Wave 2a, review round 2 (ADR 0069 "As built"): each test names the
round-2 item (N1-N13) it pins.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import mcp_client, mcp_sandbox, sandbox
from skep.supervisor.mcp_client import (
    MCPClient,
    MCPServerConfig,
    load_mcp_servers,
    runner_for_config,
    save_mcp_server,
)
from skep.supervisor.store import RunStore

from .mcp_fixtures import PY_SERVER

needs_strict_wall = pytest.mark.skipif(
    sandbox.availability().backend not in sandbox.STRICT_READ_BACKENDS,
    reason="real sandbox proof (runs where bwrap or sandbox-exec is the backend)",
)
VALUE = "plain-test-value-for-redaction-42"


@pytest.fixture(autouse=True)
def clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from skep.supervisor import secret_floor

    sandbox.availability.cache_clear()
    mcp_client._LISTED.clear()
    monkeypatch.setattr(secret_floor, "HOME_DIRS", (f"{tmp_path}/no-homes/*",))
    for name in (*sandbox._SOCKET_ENV, "UV_TOOL_DIR", "CARGO_HOME", "RUSTUP_HOME", "PYENV_ROOT",
                 "NVM_DIR"):  # fmt: skip
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("UV_PYTHON_INSTALL_DIR", str(Path.home() / ".local/share/uv/python"))
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    yield
    getattr(sandbox.availability, "cache_clear", lambda: None)()
    mcp_client._LISTED.clear()


@pytest.fixture()
def skep_home(tmp_path: Path) -> Path:
    home = tmp_path / "skep"
    (home / "supervisor").mkdir(parents=True)
    return home


@pytest.fixture()
def store(skep_home: Path) -> Iterator[RunStore]:
    opened = RunStore(skep_home / "supervisor" / "supervisor.sqlite3")
    yield opened
    opened.close()


@pytest.fixture()
def server(tmp_path: Path) -> Path:
    root = tmp_path / "srv"
    root.mkdir()
    (root / "server.py").write_text(PY_SERVER, encoding="utf-8")
    return root


def _strict_config(server: Path, server_id: str = "weather", *names: str) -> MCPServerConfig:
    return MCPServerConfig(
        server_id, "stdio", command=(sys.executable, str(server / "server.py")),
        sandbox="strict", read_paths=(str(server),), secret_names=names,
    )  # fmt: skip


def _call(store: RunStore, skep_home: Path, server_id: str, tool: str, **arguments: Any) -> Any:
    config = load_mcp_servers(store)[server_id]
    client = MCPClient(config, runner=runner_for_config(config, skep_home=skep_home, store=store))
    return client.call_tool(tool, arguments)


def _seatbelt(monkeypatch: pytest.MonkeyPatch) -> None:
    probe = sandbox.SandboxAvailability(True, backend="seatbelt")
    monkeypatch.setattr(sandbox, "availability", lambda backend=None: probe)


# -- N1: no secret injection for a strict server on Seatbelt -----------------------


def test_a_strict_server_with_secrets_is_refused_on_seatbelt_at_every_step(
    store: RunStore, skep_home: Path, server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import wall

    mcp_sandbox.set_secret(skep_home, "weather", "WEATHER_KEY", VALUE)
    config = _strict_config(server, "weather", "WEATHER_KEY")
    _seatbelt(monkeypatch)
    started: list[Any] = []
    monkeypatch.setattr(wall, "walled", lambda *a, **k: started.append(a))
    # registration: the card and the executor
    problem = mcp_sandbox.registration_problem(config, skep_home)
    assert problem is not None and "not available on macOS in this release" in problem
    assert "sandbox='host'" in problem and "without secret_names" in problem
    args = {"server_id": "weather", "transport": "stdio", "command": list(config.command),
            "read_paths": [str(server)], "secret_names": ["WEATHER_KEY"]}  # fmt: skip
    with pytest.raises(ValueError, match="not available on macOS"):
        mcp_sandbox.register(store, args, chat_id=None)
    # launch: one saved before (another platform's registry, an old release)
    save_mcp_server(store, config)
    result = _call(store, skep_home, "weather", "env", names=["WEATHER_KEY"])
    assert result.outcome == "not_run" and "not available on macOS" in str(result.error)
    assert started == []
    # the doctor
    (line,) = mcp_sandbox.posture_lines(store, skep_home)
    assert "not available on macOS in this release" in line


def test_a_strict_server_without_secrets_still_registers_on_seatbelt(
    skep_home: Path, server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seatbelt(monkeypatch)
    assert mcp_sandbox.registration_problem(_strict_config(server, "weather"), skep_home) is None


def test_linux_keeps_strict_secrets(
    skep_home: Path, server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = sandbox.SandboxAvailability(True, backend="bubblewrap")
    monkeypatch.setattr(sandbox, "availability", lambda backend=None: probe)
    config = _strict_config(server, "weather", "WEATHER_KEY")
    assert mcp_sandbox.registration_problem(config, skep_home) is None


# -- N2: one immutable per-launch snapshot, the listed names only ------------------


@needs_strict_wall
def test_a_value_set_during_a_launch_is_never_one_the_scrubber_misses(
    store: RunStore, skep_home: Path, server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The value redaction knows is the value injected: a `secret set` between
    the load and the wall's last step changes neither."""
    mcp_sandbox.set_secret(skep_home, "weather", "WEATHER_KEY", VALUE)
    save_mcp_server(store, _strict_config(server, "weather", "WEATHER_KEY"))
    newer = "a-newer-value-set-mid-launch-77"
    real = mcp_sandbox.hidden_toolchains

    def mid_launch() -> dict[str, str]:  # runs after the values were read
        mcp_sandbox.set_secret(skep_home, "weather", "WEATHER_KEY", newer)
        return real()

    monkeypatch.setattr(mcp_sandbox, "hidden_toolchains", mid_launch)
    got = _call(store, skep_home, "weather", "env", names=["WEATHER_KEY"])
    assert got.ok, got.error
    assert newer not in str(got.content)
    assert json.loads(got.content[0]["text"]) == {"WEATHER_KEY": "[redacted]"}


@needs_strict_wall
def test_the_server_reads_no_value_it_does_not_list(
    store: RunStore, skep_home: Path, server: Path
) -> None:
    mcp_sandbox.set_secret(skep_home, "weather", "WEATHER_KEY", VALUE)
    mcp_sandbox.set_secret(skep_home, "weather", "UNLISTED_KEY", "unlisted-value-not-for-it-9")
    save_mcp_server(store, _strict_config(server, "weather", "WEATHER_KEY"))
    held = str(mcp_sandbox.secrets_file(skep_home, "weather"))
    got = _call(store, skep_home, "weather", "probe", paths=[held])
    assert got.ok, got.error
    assert json.loads(got.content[0]["text"])[held] != "read"
    snapshots = list((skep_home / "supervisor" / mcp_sandbox.SECRETS_DIR).glob("*launch*"))
    assert snapshots == []  # each launch's snapshot is gone after it


# -- N3: an initialize error is scrubbed before it is formatted ---------------------


def test_an_initialize_error_is_scrubbed_before_it_is_formatted(tmp_path: Path) -> None:
    tricky = 'key"with\\odd-chars-1234'
    script = tmp_path / "bad_init.py"
    script.write_text(
        "import json, sys\n"
        "sys.stdin.readline()\n"
        f"print(json.dumps({{'jsonrpc': '2.0', 'id': 1, 'error': {{'code': -1, "
        f"'message': 'bad ' + {tricky!r}}}}}), flush=True)\n",
        encoding="utf-8",
    )
    payload = mcp_client._stdio_payload("tools/list", {})
    with pytest.raises(mcp_client.MCPNotRun) as caught:
        mcp_client._stdio_exchange(
            [sys.executable, str(script)], payload,
            scrub=lambda value: mcp_sandbox.redact(value, {"K": tricky}),
        )  # fmt: skip
    assert "odd-chars" not in str(caught.value) and "[redacted]" in str(caught.value)
    assert os.environ.get("K") is None
