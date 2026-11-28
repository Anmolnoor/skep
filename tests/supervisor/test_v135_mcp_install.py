"""v135 Wave 2a (ADR 0069 2.2): skep installs a pinned server once, under the
card, without running package code — from a loopback registry here, so no
test needs the public internet.

npm with --ignore-scripts and uv wheels-only, each inside its own strict
wall that reaches only the registry. The tree is installed straight into its
incarnation's path and published by a marker written after install AND
handshake succeed; a failure deletes it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import mcp_client, mcp_sandbox, netproxy, sandbox
from skep.supervisor.mcp_client import MCPClient, load_mcp_servers, runner_for_config
from skep.supervisor.store import RunStore

from .mcp_fixtures import Registry, build_npm_package, build_sdist, build_wheel, registry

needs_strict_wall = pytest.mark.skipif(
    sandbox.availability().backend not in sandbox.STRICT_READ_BACKENDS,
    reason="real sandbox proof (runs where bwrap or sandbox-exec is the backend)",
)
NPM, NODE, UV = shutil.which("npm"), shutil.which("node"), shutil.which("uv")
needs_npm = pytest.mark.skipif(NPM is None or NODE is None, reason="needs node and npm on PATH")
needs_uv = pytest.mark.skipif(UV is None, reason="needs uv on PATH")


@pytest.fixture(autouse=True)
def clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from skep.supervisor import secret_floor

    sandbox.availability.cache_clear()
    mcp_client._LISTED.clear()
    monkeypatch.setattr(secret_floor, "HOME_DIRS", (f"{tmp_path}/no-homes/*",))
    real_home = Path.home()
    if NODE is not None and ".nvm" in Path(NODE).resolve().parts:
        parts = Path(NODE).resolve().parts
        monkeypatch.setenv("NVM_DIR", str(Path(*parts[: parts.index(".nvm") + 1])))
    for name in (*sandbox._SOCKET_ENV, "UV_TOOL_DIR", "CARGO_HOME", "RUSTUP_HOME", "PYENV_ROOT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("UV_PYTHON_INSTALL_DIR", str(real_home / ".local/share/uv/python"))
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    real = netproxy.resolve_public

    def resolve(host: str, port: int, resolver: Any = None) -> tuple[list[str], str]:
        return (["127.0.0.1"], "") if host.endswith(".test") else real(host, port)

    monkeypatch.setattr(netproxy, "resolve_public", resolve)
    yield
    sandbox.availability.cache_clear()
    mcp_client._LISTED.clear()


@pytest.fixture()
def skep_home(tmp_path: Path) -> Path:
    home = tmp_path / "skep"
    (home / "supervisor").mkdir(parents=True)
    return home


def _store(skep_home: Path) -> RunStore:
    return RunStore(skep_home / "supervisor" / "supervisor.sqlite3")


@pytest.fixture()
def reg(monkeypatch: pytest.MonkeyPatch) -> Iterator[Registry]:
    with registry() as running:
        monkeypatch.setattr(
            mcp_sandbox,
            "INSTALL_SOURCES",
            {
                "npm": mcp_sandbox.InstallSource((running.host,), ("--registry", running.url)),
                "pypi": mcp_sandbox.InstallSource(
                    (running.host,), ("--index-url", f"{running.url}/simple")
                ),
            },
        )
        yield running


def _register(store: RunStore, chat_id: str, **args: Any) -> dict[str, Any]:
    result = mcp_sandbox.register(store, {"transport": "stdio", **args}, chat_id=chat_id)
    mcp_sandbox.wait_for_install(store, str(args["server_id"]), timeout=240)
    return result


def _reported(store: RunStore, chat_id: str) -> str:
    rows = [m for m in store.chat_messages(chat_id) if (m.provenance or "").startswith("mcp-")]
    assert len(rows) == 1, rows
    return rows[0].content


def _call(store: RunStore, skep_home: Path, server_id: str, tool: str, **arguments: Any) -> Any:
    config = load_mcp_servers(store)[server_id]
    client = MCPClient(config, runner=runner_for_config(config, skep_home=skep_home, store=store))
    return client.call_tool(tool, arguments)


def _restart(store: RunStore, skep_home: Path) -> RunStore:
    """A fresh serve: nothing in memory, the store reopened from disk."""
    store.close()
    mcp_client._LISTED.clear()
    return _store(skep_home)


@needs_strict_wall
@needs_npm
def test_an_npm_server_installs_without_its_scripts_and_runs_after_a_restart(
    skep_home: Path, reg: Registry, tmp_path: Path
) -> None:
    marker = tmp_path / "scripts-ran"
    tarball = build_npm_package(marker=marker)
    # the control: a plain npm install of the same package runs its scripts
    (tmp_path / "pkg.tgz").write_bytes(tarball)
    control = tmp_path / "control"
    subprocess.run([str(NPM), "install", "--prefix", str(control), str(tmp_path / "pkg.tgz")],
                   check=True, capture_output=True, timeout=120)  # fmt: skip
    assert marker.read_text() == "ran"
    marker.unlink()
    reg.add_npm(tarball)
    store = _store(skep_home)
    chat_id = store.create_chat(title="t", model=None).chat_id
    result = _register(store, chat_id, server_id="fx", command=["fx-mcp"],
                       install="npm:fx-mcp@1.0.0")  # fmt: skip
    assert "installing off this request" in result["install"]
    report = _reported(store, chat_id)
    assert "' installed (npm:fx-mcp@1.0.0)" in report and "probe" in report, report
    assert not marker.exists()  # no preinstall, install or postinstall ran
    config = load_mcp_servers(store)["fx"]
    tree = skep_home / "mcp" / "fx" / config.incarnation
    assert (tree / "node_modules" / ".bin" / "fx-mcp").exists()
    assert mcp_sandbox.published_marker(skep_home, "fx", config.incarnation).is_file()
    assert any(hit.startswith("/fx-mcp") for hit in reg.hits)
    store = _restart(store, skep_home)
    hits = len(reg.hits)
    try:
        result_ = _call(store, skep_home, "fx", "env", names=["npm_config_offline"])
        assert result_.ok, result_.error
        assert len(reg.hits) == hits  # no network at run time
    finally:
        store.close()


@needs_strict_wall
@needs_uv
def test_a_python_server_installs_from_wheels_and_runs_after_a_restart(
    skep_home: Path, reg: Registry, tmp_path: Path
) -> None:
    reg.add_python(build_wheel(tmp_path), project="fxmcp")
    store = _store(skep_home)
    chat_id = store.create_chat(title="t", model=None).chat_id
    _register(store, chat_id, server_id="fxpy", command=["fx-mcp"],
              install="pypi:fxmcp==1.0.0")  # fmt: skip
    assert "' installed (pypi:fxmcp==1.0.0)" in _reported(store, chat_id)
    incarnation = load_mcp_servers(store)["fxpy"].incarnation
    entry = skep_home / "mcp" / "fxpy" / incarnation / "bin" / "fx-mcp"
    # installed in place: the absolute interpreter path in its launcher stays true
    # (a long path gets uv's `#!/bin/sh` + exec trampoline instead of a shebang)
    assert str(entry.parent) in "\n".join(entry.read_text().splitlines()[:3])
    for _ in range(2):  # after publication, and after a restart with no network
        result = _call(store, skep_home, "fxpy", "env", names=["UV_OFFLINE"])
        assert result.ok, result.error
        assert json.loads(result.content[0]["text"]) == {"UV_OFFLINE": "1"}
        store = _restart(store, skep_home)
    store.close()


@needs_strict_wall
@needs_uv
def test_a_source_only_package_is_refused_and_names_the_alternatives(
    skep_home: Path, reg: Registry, tmp_path: Path
) -> None:
    reg.add_python(build_sdist(tmp_path), project="fxsrc")
    store = _store(skep_home)
    try:
        chat_id = store.create_chat(title="t", model=None).chat_id
        _register(store, chat_id, server_id="src", command=["fx-src"],
                  install="pypi:fxsrc==1.0.0")  # fmt: skip
        report = _reported(store, chat_id)
        assert "NOT installed" in report and "no source builds" in report
        assert "sandbox='host'" in report and "absolute entry point" in report
        assert not (skep_home / "mcp" / "src").exists() or not any(
            (skep_home / "mcp" / "src").iterdir()
        )  # nothing published, the tree deleted
        result = _call(store, skep_home, "src", "probe")
        assert result.outcome == "not_run" and "no finished install" in str(result.error)
    finally:
        store.close()


@needs_strict_wall
@needs_npm
def test_a_failed_install_publishes_nothing(skep_home: Path, reg: Registry) -> None:
    store = _store(skep_home)
    try:
        chat_id = store.create_chat(title="t", model=None).chat_id
        _register(
            store, chat_id, server_id="gone", command=["fx-mcp"], install="npm:fx-mcp@9.9.9"
        )  # fmt: skip  (the registry has no such package)
        assert "NOT installed" in _reported(store, chat_id)
        assert list((skep_home / "mcp" / "gone").iterdir()) == []
    finally:
        store.close()


@needs_strict_wall
@needs_npm
def test_a_wrong_entry_point_name_lists_what_the_package_offers(
    skep_home: Path, reg: Registry
) -> None:
    reg.add_npm(build_npm_package())
    store = _store(skep_home)
    try:
        chat_id = store.create_chat(title="t", model=None).chat_id
        _register(store, chat_id, server_id="fx", command=["nope"], install="npm:fx-mcp@1.0.0")
        report = _reported(store, chat_id)
        assert "no entry point named 'nope'" in report and "fx-mcp" in report
        assert list((skep_home / "mcp" / "fx").iterdir()) == []
    finally:
        store.close()


def test_an_unset_secret_refuses_the_install_before_any_download(
    skep_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_sandbox, "sandbox_refusal", lambda server_id: None)
    monkeypatch.setattr(mcp_sandbox, "_run_installer", lambda *a: pytest.fail("downloaded"))
    store = _store(skep_home)
    try:
        chat_id = store.create_chat(title="t", model=None).chat_id
        _register(store, chat_id, server_id="k", command=["fx-mcp"],
                  install="npm:fx-mcp@1.0.0", secret_names=["K_TOKEN"])  # fmt: skip
        assert "skep mcp secret set k K_TOKEN" in _reported(store, chat_id)
    finally:
        store.close()


def test_a_replaced_registration_never_publishes_the_old_install(
    skep_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Install and re-registration race: the publish step re-checks the
    incarnation under the registry gate."""
    store = _store(skep_home)
    try:
        monkeypatch.setattr(mcp_sandbox, "sandbox_refusal", lambda server_id: None)

        def install(config: Any, tree: Path, home: Path, timeout: float) -> None:
            bin_dir = tree / "node_modules" / ".bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "fx-mcp").write_text("")
            mcp_client.save_mcp_server(store, config)  # replaced mid-install

        monkeypatch.setattr(mcp_sandbox, "_run_installer", install)
        monkeypatch.setattr(mcp_sandbox, "strict_exchange", lambda *a, **k: [
            '{"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}'])  # fmt: skip
        chat_id = store.create_chat(title="t", model=None).chat_id
        _register(store, chat_id, server_id="fx", command=["fx-mcp"], install="npm:fx-mcp@1.0.0")
        assert "registered again or removed while it installed" in _reported(store, chat_id)
        assert not list((skep_home / "mcp" / "fx").glob("*.published"))
    finally:
        store.close()
