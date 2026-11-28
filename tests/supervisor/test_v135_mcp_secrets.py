"""v135 Wave 2a (ADR 0069 2.5): a strict server's secrets come from its own
file, set through the CLI, never from serve's environment — and its own
values are redacted from what it returns before a transcript sees them.
"""

from __future__ import annotations

import io
import json
import os
import stat
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
    monkeypatch.setenv("WEATHER_KEY", "from-serve-env-not-the-file")
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


def _cli(skep_home: Path, monkeypatch: pytest.MonkeyPatch, *argv: str, stdin: str = "") -> int:
    from skep.cli import main

    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    return int(main(["--home", str(skep_home), "mcp", "secret", *argv]) or 0)


def _call(store: RunStore, skep_home: Path, server_id: str, tool: str, **arguments: Any) -> Any:
    config = load_mcp_servers(store)[server_id]
    client = MCPClient(config, runner=runner_for_config(config, skep_home=skep_home, store=store))
    return client.call_tool(tool, arguments)


def _strict(store: RunStore, server_id: str, server: Path, *names: str) -> None:
    save_mcp_server(store, MCPServerConfig(
        server_id, "stdio", command=(sys.executable, str(server / "server.py")),
        sandbox="strict", read_paths=(str(server),), secret_names=names,
    ))  # fmt: skip


def test_the_cli_reads_the_value_from_stdin_and_stores_it_0600(
    skep_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(skep_home, monkeypatch, "set", "weather", "WEATHER_KEY", stdin=VALUE + "\n") == 0
    path = mcp_sandbox.secrets_file(skep_home, "weather")
    assert json.loads(path.read_text()) == {"WEATHER_KEY": VALUE}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert VALUE not in capsys.readouterr().out
    assert _cli(skep_home, monkeypatch, "list", "weather") == 0
    listed = capsys.readouterr().out
    assert "WEATHER_KEY: set" in listed and VALUE not in listed
    assert _cli(skep_home, monkeypatch, "unset", "weather", "WEATHER_KEY") == 0
    assert json.loads(path.read_text()) == {}


def test_the_value_is_never_an_argument(skep_home: Path) -> None:
    from skep.cli import _parser

    with pytest.raises(SystemExit):
        _parser().parse_args(["mcp", "secret", "set", "weather", "WEATHER_KEY", VALUE])


def test_refused_names_are_refused_by_the_cli_and_the_registration(
    skep_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("HOME", "PATH", "TMPDIR", "npm_config_registry", "UV_INDEX_URL", "HTTPS_PROXY",
                 "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "NODE_OPTIONS", "NODE_PATH",
                 "PYTHONPATH", "PYTHONSTARTUP"):  # fmt: skip
        assert _cli(skep_home, monkeypatch, "set", "weather", name, stdin="x" * 20) != 0
        assert "refused as a secret name" in capsys.readouterr().err
        assert mcp_sandbox.secret_name_problem(name) is not None
    assert not mcp_sandbox.secrets_file(skep_home, "weather").exists()


def test_the_secrets_file_is_on_the_secret_floor(skep_home: Path) -> None:
    from skep.supervisor.secret_floor import secret_floor_reason

    path = mcp_sandbox.set_secret(skep_home, "weather", "WEATHER_KEY", VALUE)
    assert secret_floor_reason(str(path), skep_home=skep_home / "supervisor") is not None
    masked = sandbox.secret_paths(skep_home)
    assert path.parent.resolve() in masked


@needs_strict_wall
def test_a_secret_reaches_only_its_server_and_no_other_sandbox(
    store: RunStore, skep_home: Path, server: Path, tmp_path: Path
) -> None:
    from skep.supervisor.wall import walled

    path = mcp_sandbox.set_secret(skep_home, "weather", "WEATHER_KEY", VALUE)
    _strict(store, "weather", server, "WEATHER_KEY")
    _strict(store, "other", server)
    names = {"names": ["WEATHER_KEY"]}
    own = _call(store, skep_home, "weather", "env", **names)
    assert own.ok, own.error  # its value came in, then left redacted
    assert json.loads(own.content[0]["text"]) == {"WEATHER_KEY": "[redacted]"}
    leak = _call(store, skep_home, "weather", "leak", name="WEATHER_KEY")
    assert "[redacted]" in str(leak.content) and VALUE not in str(leak.content)
    assert json.loads(_call(store, skep_home, "other", "env", **names).content[0]["text"]) == {
        "WEATHER_KEY": None  # not from serve's env either
    }
    work = tmp_path / "work"
    work.mkdir()
    probe = [sys.executable, "-c", f"print(open({str(path)!r}).read())"]
    with walled(probe, skep_home=skep_home, hosts=(), workspace=work,
                read_allowlist=(skep_home.parent / "srv",)) as (argv, env):  # fmt: skip
        import subprocess

        proc = subprocess.run(argv, env=env, capture_output=True, text=True)
    assert VALUE not in proc.stdout and proc.returncode != 0
    assert VALUE in path.read_text()  # the control: on the host it is there


@needs_strict_wall
def test_a_secret_is_redacted_from_tool_errors_and_diagnostics(
    store: RunStore, skep_home: Path, server: Path
) -> None:
    mcp_sandbox.set_secret(skep_home, "weather", "WEATHER_KEY", VALUE)
    _strict(store, "weather", server, "WEATHER_KEY")
    as_error = _call(store, skep_home, "weather", "leak", name="WEATHER_KEY", as_error=True)
    assert not as_error.ok and VALUE not in str(as_error.error)
    on_stderr = _call(store, skep_home, "weather", "leak", name="WEATHER_KEY", stderr=True)
    assert on_stderr.outcome == "unknown" and "stderr ended with" in str(on_stderr.error)
    assert VALUE not in str(on_stderr.error) and "[redacted]" in str(on_stderr.error)


def test_an_unset_secret_is_a_teaching_error_at_the_handshake(
    store: RunStore, skep_home: Path, server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_sandbox, "sandbox_refusal", lambda server_id: None)
    _strict(store, "weather", server, "WEATHER_KEY")
    result = _call(store, skep_home, "weather", "env")
    assert result.outcome == "not_run"
    assert "skep mcp secret set weather WEATHER_KEY" in str(result.error)


def test_redaction_reaches_nested_results() -> None:
    values = {"K": VALUE}
    got = mcp_sandbox.redact({"a": [f"x{VALUE}y", {VALUE: 1}], "b": 2}, values)
    assert got == {"a": ["x[redacted]y", {"[redacted]": 1}], "b": 2}
    assert os.environ.get("WEATHER_KEY") == "from-serve-env-not-the-file"


# -- review round 1 (S1, S2, S3, S17) ---------------------------------------------


@needs_strict_wall
@pytest.mark.parametrize("hosts", [(), ("api.example.test",)])
def test_the_launcher_never_holds_a_secret_and_the_server_still_gets_it(
    store: RunStore, skep_home: Path, server: Path, monkeypatch: pytest.MonkeyPatch,
    hosts: tuple[str, ...],
) -> None:  # fmt: skip
    """S1: bwrap / sandbox-exec and the netshim start with no secret in their
    environment or argv; only the process inside the wall gets it."""
    import subprocess

    mcp_sandbox.set_secret(skep_home, "weather", "WEATHER_KEY", VALUE)
    save_mcp_server(store, MCPServerConfig(
        "weather", "stdio", command=(sys.executable, str(server / "server.py")),
        sandbox="strict", read_paths=(str(server),), secret_names=("WEATHER_KEY",),
        egress="hosts" if hosts else "none", allowed_hosts=hosts,
    ))  # fmt: skip
    launched: list[tuple[list[str], dict[str, str]]] = []
    real = subprocess.Popen

    def spy(argv: Any, *args: Any, **kwargs: Any) -> Any:
        launched.append((list(argv), dict(kwargs.get("env") or os.environ)))
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spy)
    got = _call(store, skep_home, "weather", "env", names=["WEATHER_KEY"])
    assert got.ok, got.error
    assert json.loads(got.content[0]["text"]) == {"WEATHER_KEY": "[redacted]"}  # it arrived
    assert launched
    for argv, env in launched:
        assert not any(VALUE in part for part in argv)
        assert not any(VALUE in value for value in env.values())


def test_a_value_too_short_to_redact_is_refused_at_set(skep_home: Path) -> None:
    """S2: a value redaction cannot mask safely is refused, naming the minimum."""
    with pytest.raises(mcp_client.MCPError, match=f"at least {mcp_sandbox.MIN_SECRET_LENGTH}"):
        mcp_sandbox.set_secret(skep_home, "weather", "WEATHER_KEY", "abc")
    assert not mcp_sandbox.secrets_file(skep_home, "weather").exists()
    assert mcp_sandbox.redact("x-abcdefgh-y", {"K": "abcdefgh"}) == "x-[redacted]-y"


@needs_strict_wall
def test_stderr_is_masked_before_it_is_cropped(
    store: RunStore, skep_home: Path, server: Path
) -> None:
    """S2: a value cut by the stderr crop leaves no fragment behind."""
    mcp_sandbox.set_secret(skep_home, "weather", "WEATHER_KEY", VALUE)
    _strict(store, "weather", server, "WEATHER_KEY")
    pad = 500 - len("value=") - 10  # the per-line crop lands 10 chars into the value
    got = _call(store, skep_home, "weather", "leak", name="WEATHER_KEY", stderr=True, pad=pad)
    assert got.outcome == "unknown" and "stderr ended with" in str(got.error)
    assert VALUE[:10] not in str(got.error)


def test_a_reply_error_is_redacted_after_it_is_parsed() -> None:
    """S2: protocol fields first, then the server's data — a value JSON escapes
    (a quote, a backslash) is still masked in a JSON-RPC error."""
    tricky = 'key"with\\odd-chars-1234'
    line = json.dumps({"jsonrpc": "2.0", "id": 2, "error": {"message": f"bad {tricky}"}})
    with pytest.raises(mcp_client.MCPReplyError) as caught:
        mcp_sandbox.redacted_reply([line], {"K": tricky})
    assert tricky not in str(caught.value) and "[redacted]" in str(caught.value)
    assert "odd-chars" not in str(caught.value)


def _denylist_wall(skep_home: Path, work: Path, argv: list[str]) -> list[str]:
    """A wall that reads everything but the secret floor: only a mask stands
    between it and the secrets dir (S17)."""
    profile = sandbox.write_profile(
        work.parent / f"{work.name}.profile", workspace=work, skep_home=skep_home
    )
    return sandbox.wrap_command(argv, profile)


@needs_strict_wall
def test_a_sandbox_started_before_the_first_secret_cannot_read_it(
    skep_home: Path, tmp_path: Path
) -> None:
    """S3 + S17: the secrets dir is made before any sandbox starts and masked
    while empty, in a wall that would otherwise read it."""
    import subprocess

    path = mcp_sandbox.secrets_file(skep_home, "weather")
    beside = skep_home / "supervisor" / "not-a-key.txt"
    beside.write_text("readable", encoding="utf-8")  # the control: the wall reads the dir
    work = tmp_path / "work"
    work.mkdir()
    probe = (
        "import sys; sys.stdin.readline(); "
        f"print(open({str(beside)!r}).read()); "
        f"print(open({str(path)!r}).read())"
    )
    argv = _denylist_wall(skep_home, work, [sys.executable, "-c", probe])
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)  # fmt: skip
    mcp_sandbox.set_secret(skep_home, "weather", "WEATHER_KEY", VALUE)  # after it started
    out, err = proc.communicate("go\n", timeout=60)
    assert "readable" in out, err
    assert VALUE not in out and proc.returncode != 0
    assert VALUE in path.read_text()  # the unconfined control


def test_the_in_wall_step_sets_only_listed_names_and_refuses_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """S1: ``skep.secretenv`` — what the wall's last step does, run here."""
    from skep import secretenv

    held = tmp_path / "x-secret"
    held.write_text(json.dumps({"A_KEY": VALUE, "OTHER": "y" * 9}), encoding="utf-8")
    ran: list[list[str]] = []
    monkeypatch.setattr(os, "execvp", lambda head, argv: ran.append(list(argv)))
    monkeypatch.delenv("A_KEY", raising=False)
    assert secretenv.main([str(held), "A_KEY", "--", "srv", "-v"]) == 127
    assert ran == [["srv", "-v"]] and os.environ.pop("A_KEY") == VALUE
    assert "OTHER" not in os.environ
    monkeypatch.setenv("A_KEY", "the-wall-set-it")
    for argv in ([str(held), "A_KEY", "--", "srv"], [str(held), "MISSING", "--", "srv"],
                 [str(tmp_path / "none"), "A_KEY", "--", "srv"], [str(held), "A_KEY", "--"],
                 [str(held), "A_KEY"]):  # fmt: skip
        assert secretenv.main(argv) == 2
    assert len(ran) == 1 and VALUE not in capsys.readouterr().err
