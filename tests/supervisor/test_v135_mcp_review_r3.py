"""v135 Wave 2a, review round 3 (ADR 0069 "As built"): each test names the
round-3 item (M1-M6) it pins.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import mcp_client, mcp_sandbox, sandbox
from skep.supervisor.mcp_client import MCPServerConfig
from skep.supervisor.store import RunStore


@pytest.fixture(autouse=True)
def clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    sandbox.availability.cache_clear()
    mcp_client._LISTED.clear()
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


# -- M1: a captured "no registration" is a capture, never a re-read ------------------


def test_a_registration_that_appears_after_the_capture_is_not_the_cards(
    store: RunStore, tmp_path: Path
) -> None:
    from skep.supervisor.serve.cards import recorded_card
    from skep.supervisor.serve.chat import pending_duplicate_action

    chat_id = store.create_chat(title="t", model=None).chat_id
    args = {"server_id": "late", "tool": "t", "arguments": {}}
    captured = mcp_sandbox.card_incarnation(store, args)
    assert captured is None  # the decision saw no server
    mcp_client.save_mcp_server(
        store, MCPServerConfig("late", "stdio", command=(sys.executable, "x.py"))
    )  # registered between the capture and the card
    store.add_chat_action(chat_id, tool="call_mcp_tool", args=args, card={"incarnation": None})
    _record, identical = pending_duplicate_action(
        store, chat_id, "call_mcp_tool", args, incarnation=None
    ) or (None, None)
    assert identical is True  # the check used the capture, as the card does
    card = recorded_card("call_mcp_tool", args, None, store, incarnation=None)
    assert card is not None and card["incarnation"] is None
    # nothing captured: the card reads the registration now
    read_now = recorded_card("call_mcp_tool", args, None, store)
    assert read_now is not None and read_now["incarnation"]


# -- M2: an http listing holds no registry gate ---------------------------------------


def test_a_slow_http_listing_does_not_block_a_registry_write(
    store: RunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    listing, release = threading.Event(), threading.Event()

    def slow(method: str, params: Any) -> Any:
        listing.set()
        release.wait(10)
        return {"tools": []}

    monkeypatch.setattr(mcp_client, "_transport_runner", lambda config, **_: slow)
    config = MCPServerConfig("web", "http", url="http://127.0.0.1:9/mcp")
    lister = threading.Thread(
        target=lambda: mcp_client.refresh_listed_tools(store, config), daemon=True
    )
    lister.start()
    assert listing.wait(5)
    wrote = threading.Event()

    def write() -> None:
        with mcp_sandbox.REGISTRY_GATE.writing():
            wrote.set()

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    try:
        assert wrote.wait(2), "a registry write waited on an http listing"
    finally:
        release.set()
        lister.join(5)
        writer.join(5)


# -- M5: initialize is validated as sent; only what is shown is redacted ---------------

_INIT_OK = (
    '{"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05", '
    '"capabilities": {}, "serverInfo": {"name": "fx", "version": "1"}}}'
)
_CALL_OK = '{"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": "hi"}]}}'


def _script(tmp_path: Path, init: str, reply: str = _CALL_OK) -> Path:
    script = tmp_path / "srv_m5.py"
    script.write_text(
        "import sys\n"
        "sys.stdin.readline()\n"
        f"print({init!r}, flush=True)\n"
        "sys.stdin.readline(); sys.stdin.readline()\n"
        f"print({reply!r}, flush=True)\n",
        encoding="utf-8",
    )
    return script


@pytest.mark.parametrize("secret", ["serverInfo", "protocolVersion", "capabilities"])
def test_a_secret_equal_to_a_required_field_name_does_not_refuse_the_handshake(
    tmp_path: Path, secret: str
) -> None:
    script = _script(tmp_path, _INIT_OK)
    payload = mcp_client._stdio_payload("tools/call", {"name": "t", "arguments": {}})
    lines = mcp_client._stdio_exchange(
        [sys.executable, str(script)], payload,
        scrub=lambda value: mcp_sandbox.redact(value, {"K": secret}),
    )  # fmt: skip
    assert json.loads(lines[-1])["id"] == 2


def test_an_initialize_error_is_still_shown_redacted(tmp_path: Path) -> None:
    masked = "an-error-value-to-mask-77"  # never `secret = "…"`: the hygiene scan
    init = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": f"bad {masked}"}}
    )
    payload = mcp_client._stdio_payload("tools/list", {})
    with pytest.raises(mcp_client.MCPNotRun) as caught:
        mcp_client._stdio_exchange(
            [sys.executable, str(_script(tmp_path, init))], payload,
            scrub=lambda value: mcp_sandbox.redact(value, {"K": masked}),
        )  # fmt: skip
    assert masked not in str(caught.value) and "[redacted]" in str(caught.value)


# -- M6: a registry tarball and a well-formed SRI, for every source -----------------

_REG = "https://registry.npmjs.org/"
_SHA512 = "sha512-" + base64.b64encode(bytes(64)).decode()
_SHA256 = "sha256-" + base64.b64encode(bytes(32)).decode()
_TARBALL = f"{_REG}fx-mcp/-/fx-mcp-1.0.0.tgz"
_TOP = {"version": "1.0.0", "resolved": _TARBALL, "integrity": _SHA512}
_NESTED = "node_modules/fx-mcp/node_modules/inner"


def _tree(tmp_path: Path, top: dict[str, Any], **extra: Any) -> Path:
    packages = {"": {"dependencies": {"fx-mcp": "^1.0.0"}}, "node_modules/fx-mcp": top, **extra}
    tree = tmp_path / "tree"
    tree.mkdir(parents=True)
    lock = {"name": "t", "lockfileVersion": 3, "requires": True, "packages": packages}
    (tree / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    for path in packages:
        if path:
            (tree / path).mkdir(parents=True, exist_ok=True)
            (tree / path / "package.json").write_text("{}", encoding="utf-8")
    return tree


@pytest.mark.parametrize(
    "field,value",
    [
        ("resolved", f"{_REG}fx-mcp"),  # the packument, not a tarball
        ("resolved", f"{_REG}fx-mcp/-/other-1.0.0.tgz"),
        ("resolved", f"{_REG}fx-mcp/-/fx-mcp-1.0.1.tgz"),
        ("resolved", f"{_TARBALL}?x=1"),
        ("resolved", f"{_TARBALL}#frag"),
        ("resolved", f"{_REG}evil/-/fx-mcp-1.0.0.tgz"),
        ("resolved", _TARBALL.replace("https://", "https://u:p@")),
        ("resolved", _TARBALL.replace("https://", "http://")),
        ("integrity", "sha512-a"),
        ("integrity", "yes"),
        ("integrity", True),
        ("integrity", _SHA256),  # well formed, but npm writes a sha512
        ("integrity", "sha1-" + base64.b64encode(bytes(20)).decode()),
        ("integrity", "sha512-" + base64.b64encode(bytes(32)).decode()),  # wrong length
        ("integrity", f"{_SHA512} sha512-a"),
        ("integrity", _SHA512.rstrip("=")),
    ],
)
def test_a_source_that_is_not_a_registry_tarball_with_a_valid_sri_is_refused(
    tmp_path: Path, field: str, value: Any
) -> None:
    tree = _tree(tmp_path, {**_TOP, field: value})
    problem = mcp_sandbox.npm_tree_problem(tree, "fx-mcp@1.0.0")
    assert problem is not None and "not from the registry" in problem, problem


@pytest.mark.parametrize("field,value", [("resolved", f"{_REG}fx-mcp"), ("integrity", "x")])
def test_a_bundling_ancestor_is_held_to_the_same_check(
    tmp_path: Path, field: str, value: Any
) -> None:
    bundler = {**_TOP, "bundleDependencies": ["inner"]}
    nested = {_NESTED: {"version": "1.0.0", "inBundle": True}}
    assert mcp_sandbox._bundled_in_registry_parent(
        _tree(tmp_path / "ok", bundler, **nested), _NESTED,
        {"node_modules/fx-mcp": bundler, **nested}, _REG,
    )  # fmt: skip
    bad = {**bundler, field: value}
    assert not mcp_sandbox._bundled_in_registry_parent(
        _tree(tmp_path / "bad", bad, **nested), _NESTED,
        {"node_modules/fx-mcp": bad, **nested}, _REG,
    )  # fmt: skip


def test_a_registry_tarball_with_npms_sri_is_accepted(tmp_path: Path) -> None:
    scoped = {
        "version": "2.0.0-beta.1",
        "resolved": f"{_REG}@scope/dep/-/dep-2.0.0-beta.1.tgz",
        "integrity": f"{_SHA512} {_SHA256}",
    }
    tree = _tree(tmp_path, dict(_TOP), **{"node_modules/@scope/dep": scoped})
    assert mcp_sandbox.npm_tree_problem(tree, "fx-mcp@1.0.0") is None


def test_the_product_registry_is_https() -> None:
    args = mcp_sandbox.INSTALL_SOURCES["npm"].args
    assert args[args.index("--registry") + 1].startswith("https://")


# -- M4: the wall reads the toolchain the supervisor resolved, and nothing else new ---


def _toolchain(root: Path) -> Path:
    """A Node install at ``root``: bin/node, and npm linking into its package."""
    (root / "bin").mkdir(parents=True)
    npm_bin = root / "lib" / "node_modules" / "npm" / "bin"
    npm_bin.mkdir(parents=True)
    for program in (root / "bin" / "node", npm_bin / "npm-cli.js"):
        program.write_text("", encoding="utf-8")
        program.chmod(0o755)
    (root / "bin" / "npm").symlink_to(npm_bin / "npm-cli.js")
    return root / "bin"


def _npm_server() -> MCPServerConfig:
    return MCPServerConfig(
        "fx", "stdio", command=("fx-mcp",), sandbox="strict", install=("npm", "fx-mcp@1.0.0")
    )


def _on_path(monkeypatch: pytest.MonkeyPatch, bin_dir: Path) -> None:
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    linux = sandbox.SandboxAvailability(True, backend="bubblewrap")
    monkeypatch.setattr(sandbox, "availability", lambda backend=None: linux)


def test_a_toolchain_outside_the_wall_roots_is_bound_as_its_resolved_prefix(
    skep_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = (tmp_path / "toolcache" / "node" / "24.1.0" / "x64").resolve()
    _on_path(monkeypatch, _toolchain(prefix))
    assert mcp_sandbox.toolchain_roots(_npm_server(), skep_home) == (prefix,)
    grant = mcp_sandbox.grant_view(_npm_server(), skep_home)
    assert grant["toolchain"] == [str(prefix)]
    assert f"plus the npm toolchain at {prefix}" in mcp_sandbox.grant_line(grant)


def test_a_toolchain_already_in_the_wall_roots_adds_nothing(
    skep_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "strict_reads_cover", lambda path, home: True)
    assert mcp_sandbox.toolchain_roots(_npm_server(), skep_home) == ()
    assert "toolchain" not in mcp_sandbox.grant_view(_npm_server(), skep_home)


@pytest.mark.parametrize("where", ["home", "root", "skep"])
def test_a_toolchain_prefix_over_the_home_or_the_skep_home_is_refused_before_any_download(
    skep_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    home = Path(str(tmp_path / "fakehome"))
    home.mkdir()
    prefix = {"home": home, "root": Path("/"), "skep": skep_home.parent}[where]
    if where == "root":  # nothing writes to /: a resolved prefix of / is faked
        monkeypatch.setattr(mcp_sandbox, "_toolchain_programs", lambda config: [Path("/node")])
    else:
        _on_path(monkeypatch, _toolchain(prefix))
    monkeypatch.setattr(sandbox, "strict_reads_cover", lambda path, home: False)
    with pytest.raises(mcp_client.MCPError) as caught:
        mcp_sandbox.toolchain_roots(_npm_server(), skep_home)
    assert str(prefix.resolve()) in str(caught.value) and "sandbox='host'" in str(caught.value)
    assert "toolchain_error" in mcp_sandbox.grant_view(_npm_server(), skep_home)
    monkeypatch.setattr(mcp_sandbox, "_installer_steps", lambda *a: pytest.fail("downloaded"))
    with pytest.raises(mcp_client.MCPError, match="toolchain"):
        mcp_sandbox._run_installer(_npm_server(), tmp_path / "tree", skep_home, 5)


def test_the_strict_reads_cover_the_system_roots_and_not_a_tool_cache(
    skep_home: Path, tmp_path: Path
) -> None:
    assert sandbox.strict_reads_cover(Path("/usr/bin"), skep_home)
    assert not sandbox.strict_reads_cover(tmp_path / "toolcache", skep_home)


def test_a_pre_installed_entry_point_the_wall_cannot_read_is_refused_naming_the_fix(
    store: RunStore, skep_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator's own command[0] outside every root (a CI tool cache, asdf):
    refused before the wall starts, naming the path and its read_paths fix."""
    bin_dir = _toolchain((tmp_path / "toolcache" / "node" / "24.1.0").resolve())
    monkeypatch.setattr(mcp_sandbox, "sandbox_refusal", lambda server_id: None)
    started: list[Any] = []
    from skep.supervisor import wall

    monkeypatch.setattr(wall, "walled", lambda *a, **k: started.append(a))
    config = mcp_client.save_mcp_server(store, MCPServerConfig(
        "pre", "stdio", command=(str(bin_dir / "node"), "/srv/x.js"), sandbox="strict",
    ))  # fmt: skip
    with pytest.raises(mcp_client.MCPNotRun) as caught:
        mcp_sandbox.strict_exchange(config, "", skep_home=skep_home, secret_values={})
    assert str(bin_dir / "node") in str(caught.value) and "read_paths" in str(caught.value)
    assert started == []
    named = dataclasses.replace(config, read_paths=(str(bin_dir.parent),))
    assert mcp_sandbox.entry_read_problem(named, skep_home) is None


# -- M3: on a real Seatbelt host, secrets are refused, never injected ---------------


@pytest.mark.skipif(
    sandbox.availability().backend != "seatbelt", reason="a real Seatbelt host (macOS)"
)
def test_on_seatbelt_a_strict_server_with_secrets_is_refused_not_injected(
    store: RunStore, skep_home: Path, tmp_path: Path
) -> None:
    mcp_sandbox.set_secret(skep_home, "weather", "WEATHER_KEY", "a-seatbelt-value-1234")
    config = mcp_client.save_mcp_server(store, MCPServerConfig(
        "weather", "stdio", command=(sys.executable, "-c", "pass"), sandbox="strict",
        secret_names=("WEATHER_KEY",),
    ))  # fmt: skip
    assert "not available on macOS" in str(mcp_sandbox.registration_problem(config, skep_home))
    with pytest.raises(mcp_client.MCPNotRun, match="not available on macOS"):
        mcp_sandbox.strict_exchange(config, "", skep_home=skep_home, secret_values={})
