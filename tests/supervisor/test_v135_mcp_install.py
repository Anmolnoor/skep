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
needs_npm = pytest.mark.skipif(
    NPM is None or NODE is None or (mcp_sandbox.npm_version(NPM) or (0,)) < mcp_sandbox.MIN_NPM,
    reason=f"needs node and npm {'.'.join(map(str, mcp_sandbox.MIN_NPM))}+ on PATH (review S9)",
)
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
        assert "registered again or removed before its install finished" in _reported(
            store, chat_id
        )
        assert not list((skep_home / "mcp" / "fx").glob("*.published"))
    finally:
        store.close()


# -- review round 1 ---------------------------------------------------------------

VALUE = "install-report-secret-value-77"


@needs_strict_wall
@needs_uv
def test_install_discovery_redacts_a_reply_error_and_its_stderr(
    skep_home: Path, reg: Registry, tmp_path: Path
) -> None:
    """S2: discovery takes the one redaction path calls take — a JSON-RPC
    error and the stderr behind it reach the install report masked."""
    reg.add_python(build_wheel(tmp_path), project="fxmcp")
    mcp_sandbox.set_secret(skep_home, "fxpy", "FX_LIST_ERROR", VALUE)
    store = _store(skep_home)
    chat_id = store.create_chat(title="t", model=None).chat_id
    _register(store, chat_id, server_id="fxpy", command=["fx-mcp"],
              install="pypi:fxmcp==1.0.0", secret_names=["FX_LIST_ERROR"])  # fmt: skip
    report = _reported(store, chat_id)
    assert "NOT installed" in report and "bad key" in report, report
    assert VALUE not in report and "[redacted]" in report
    store.close()


class _FixedTemp:
    """``tempfile.TemporaryDirectory`` at a name the test knows (S17)."""

    base: Path

    def __init__(self, *args: Any, prefix: str = "tmp", **kwargs: Any) -> None:
        self.name = str(self.base / f"fixed-{prefix}")
        Path(self.name).mkdir(parents=True, exist_ok=True)

    def __enter__(self) -> str:
        return self.name

    def __exit__(self, *exc: object) -> None:
        shutil.rmtree(self.name, ignore_errors=True)


@needs_strict_wall
@needs_uv
def test_a_strict_server_cannot_write_its_install_tree_or_its_profile(
    skep_home: Path, reg: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S17: the published tree is read-only and the profile is out of reach,
    while its own HOME is writable — on bubblewrap and on Seatbelt."""
    import tempfile

    reg.add_python(build_wheel(tmp_path), project="fxmcp")
    store = _store(skep_home)
    chat_id = store.create_chat(title="t", model=None).chat_id
    _register(store, chat_id, server_id="fxpy", command=["fx-mcp"],
              install="pypi:fxmcp==1.0.0")  # fmt: skip
    tree = mcp_sandbox.install_tree(skep_home, load_mcp_servers(store)["fxpy"])
    entry, bins = tree / "bin" / "fx-mcp", tree / "bin" / "new-file"
    _FixedTemp.base = tmp_path / "fixed"
    monkeypatch.setattr(tempfile, "TemporaryDirectory", _FixedTemp)
    profile = _FixedTemp.base / "fixed-skep-mcp-profile-" / "sandbox.profile"
    paths = [str(entry), str(bins), str(profile), "$HOME/scratch-ok"]
    result = _call(store, skep_home, "fxpy", "write", paths=paths)
    assert result.ok, result.error
    seen = json.loads(result.content[0]["text"])
    assert seen["$HOME/scratch-ok"] == "wrote"  # the in-wall control
    for path in paths[:3]:
        assert seen[path] != "wrote", seen
    with entry.open("a"):  # the unconfined control: the host may write both
        pass
    assert not bins.exists()
    store.close()


# -- S9: exact versions, a new-enough npm, registry sources for the whole tree --------

# Strings the old pattern took that npm does not read as one exact version
# (npm-package-arg: a tag, or an error), or that strict semver refuses.
NPM_NOT_EXACT = ("1.2.3-a..b", "1.2.3-a.", "1.2.3-.a", "1.2.3-", "1.2.3-01", "01.2.3",
                 "1.2.99999999999999999", "1.2.3+a..b", "1.2.3+")  # fmt: skip
NPM_EXACT = ("1.2.3", "0.0.1", "1.2.3-rc.1+build.5", "1.2.3--", "2025.9.25", "1.0.0-0a")
PYPI_NOT_EXACT = ("1.0+local", "1.0.0a", "01.0", "1.0.0-rc1", "1.0.*", "v1.0", "1.0.0.post")
PYPI_EXACT = ("1.0.0", "2!1.0", "1.0rc1", "1.0.post1", "1.0.dev3", "1.0a1.post2.dev0")


def test_only_exact_versions_are_pinned() -> None:
    for version in NPM_NOT_EXACT:
        assert mcp_sandbox.install_spec_problem(("npm", f"fx-mcp@{version}")), version
    for version in NPM_EXACT:
        assert mcp_sandbox.install_spec_problem(("npm", f"@s/fx-mcp@{version}")) is None, version
    for version in PYPI_NOT_EXACT:
        assert mcp_sandbox.install_spec_problem(("pypi", f"fxmcp=={version}")), version
    for version in PYPI_EXACT:
        assert mcp_sandbox.install_spec_problem(("pypi", f"fxmcp=={version}")) is None, version


@needs_npm
def test_every_npm_pin_accepted_is_an_exact_version_to_npm_itself() -> None:
    """The cross-check against npm's own parser (npm-package-arg)."""
    npa = Path(str(NPM)).resolve().parent.parent / "node_modules" / "npm-package-arg"
    if not npa.is_dir():
        pytest.skip(f"npm-package-arg not found beside {NPM}")
    probe = (
        f"const npa = require({json.dumps(str(npa))});"
        "for (const v of JSON.parse(process.argv[1])) {"
        " let t; try { t = npa('x@' + v).type } catch (e) { t = 'error' }"
        " console.log(t) }"
    )
    out = subprocess.run([str(NODE), "-e", probe, json.dumps(NPM_EXACT)],
                         capture_output=True, text=True, check=True).stdout.split()  # fmt: skip
    assert out == ["version"] * len(NPM_EXACT)
    tagged = subprocess.run([str(NODE), "-e", probe, json.dumps(["1.2.3-a..b", "1.2.3-a."])],
                            capture_output=True, text=True, check=True).stdout.split()  # fmt: skip
    assert tagged == ["tag", "tag"]  # what the old pattern let through: mutable tags


def test_an_npm_older_than_the_minimum_is_refused_with_the_fix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(mcp_sandbox, "npm_version", lambda npm: (11, 9, 2))
    config = mcp_client.MCPServerConfig("fx", "stdio", command=("fx-mcp",), sandbox="strict",
                                        install=("npm", "fx-mcp@1.0.0"))  # fmt: skip
    with pytest.raises(mcp_client.MCPError, match=r"npm 11\.9\.2.*11\.10\.0 or later"):
        mcp_sandbox._installer_steps(config, tmp_path)
    monkeypatch.setattr(mcp_sandbox, "npm_version", lambda npm: (11, 10, 0))
    (step,) = mcp_sandbox._installer_steps(config, tmp_path)
    assert "--allow-git=none" in step and "--ignore-scripts" in step


@needs_strict_wall
@needs_npm
def test_a_dependency_from_outside_the_registry_refuses_the_whole_install(
    skep_home: Path, reg: Registry
) -> None:
    """A directory dependency (npm links it in) is not a registry source."""
    local = Path(str(NPM)).resolve().parent.parent / "node_modules" / "npm-package-arg"
    reg.add_npm(build_npm_package(dependencies={"npm-package-arg": f"file:{local}"}))
    store = _store(skep_home)
    chat_id = store.create_chat(title="t", model=None).chat_id
    _register(store, chat_id, server_id="fx", command=["fx-mcp"], install="npm:fx-mcp@1.0.0")
    report = _reported(store, chat_id)
    assert "NOT installed" in report and "not from the registry" in report, report
    assert not list((skep_home / "mcp" / "fx").glob("*.published"))
    store.close()


# -- S10: the entry point is the pinned package's own ----------------------------------


@needs_strict_wall
@needs_npm
def test_a_dependencys_bin_is_not_the_pinned_packages_entry_point(
    skep_home: Path, reg: Registry
) -> None:
    reg.add_npm(build_npm_package(name="fx-dep", bins=("fx-other",)), name="fx-dep")
    reg.add_npm(build_npm_package(dependencies={"fx-dep": "1.0.0"}))
    store = _store(skep_home)
    chat_id = store.create_chat(title="t", model=None).chat_id
    _register(store, chat_id, server_id="fx", command=["fx-other"], install="npm:fx-mcp@1.0.0")
    report = _reported(store, chat_id)
    assert "NOT installed" in report and "not an entry point fx-mcp itself declares" in report
    assert "fx-mcp" in report.split("declares")[-1]  # it names the package's own
    store.close()


@needs_strict_wall
@needs_uv
def test_the_venvs_own_python_is_not_the_pinned_packages_entry_point(
    skep_home: Path, reg: Registry, tmp_path: Path
) -> None:
    reg.add_python(build_wheel(tmp_path), project="fxmcp")
    store = _store(skep_home)
    chat_id = store.create_chat(title="t", model=None).chat_id
    # the venv's own python, running the package's server: it would answer
    _register(store, chat_id, server_id="fxpy",
              command=["python", "-c", "import fxmcp; fxmcp.main()"],
              install="pypi:fxmcp==1.0.0")  # fmt: skip
    report = _reported(store, chat_id)
    assert "NOT installed" in report and "not a console script fxmcp declares" in report, report
    store.close()


@needs_strict_wall
@needs_uv
def test_an_entry_point_swapped_after_publication_refuses_the_launch(
    skep_home: Path, reg: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg.add_python(build_wheel(tmp_path), project="fxmcp")
    store = _store(skep_home)
    chat_id = store.create_chat(title="t", model=None).chat_id
    _register(store, chat_id, server_id="fxpy", command=["fx-mcp"],
              install="pypi:fxmcp==1.0.0")  # fmt: skip
    assert "' installed (" in _reported(store, chat_id)
    entry = mcp_sandbox.install_tree(skep_home, load_mcp_servers(store)["fxpy"]) / "bin" / "fx-mcp"
    entry.unlink()
    entry.symlink_to("/usr/bin/true")
    from skep.supervisor import wall

    started: list[Any] = []
    monkeypatch.setattr(wall, "walled", lambda *a, **k: started.append(a))
    result = _call(store, skep_home, "fxpy", "env", names=[])
    assert started == [] and result.outcome == "not_run"  # refused before any wall
    assert "resolves outside its install tree" in str(result.error)
    store.close()
