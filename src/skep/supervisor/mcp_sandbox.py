"""v135 Wave 2a (ADR 0069 sections 1 and 2): the strict lane for stdio MCP
servers the operator registers.

The registration is the authority (section 1): its confinement fields are
checked here, shown on the card before anything starts, and carry a
supervisor-issued ``incarnation`` that changes on every save, so an approval
cannot be re-aimed at a replaced server. A ``strict`` server runs behind the
one shared wall (``wall.walled``) that forged tools and script schedules use.

``host`` stays what it always was: the operator's command, unsandboxed, one
process per call, inheriting serve's environment (operator decision for
servers registered before 2.4.0; a new one is a HIGH card that says so).
"""

from __future__ import annotations

import contextlib
import re
import secrets
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .mcp_client import MCPError, MCPRunner, MCPServerConfig

if TYPE_CHECKING:
    from .store import RunStore

SANDBOX_MODES = ("strict", "host")
EGRESS_MODES = ("none", "hosts", "open")
# The id becomes a directory name (the install tree, the secrets file): a slug.
SERVER_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}")
INSTALL_KINDS = ("npm", "pypi")
_NPM_NAME = r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*"
_SEMVER = r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
NPM_PINNED = re.compile(rf"({_NPM_NAME})@({_SEMVER})")
PYPI_PINNED = re.compile(
    r"([A-Za-z0-9][A-Za-z0-9._-]*)(\[[A-Za-z0-9._,-]+\])?==([0-9][0-9A-Za-z.!+-]*)"
)
_BIN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# Runners that resolve a package at run time: never the strict lane's argv.
_RUNTIME_RESOLVERS = frozenset({"npx", "uvx", "pipx", "bunx", "pnpx", "yarn", "pnpm", "npm"})
MCP_TREE_DIR = "mcp"

HOST_HIGH = (
    "it runs this MCP server unsandboxed, as you: it can read every file your user can "
    "read, reach the network, and read skep's own serve token, so it could answer its "
    "own cards. This card discloses that; it does not prevent it"
)
STRICT_HIGH = "it adds an MCP server whose tools can act as you"
INSTALL_SENTENCE = (
    "the install runs no package scripts (npm --ignore-scripts, or wheels only); only "
    "the top package is pinned, and its dependencies are resolved when the install runs"
)
SECRET_SENTENCE = "a secret you give it is its to use, including sending it to a host it may reach"


def new_incarnation() -> str:
    """Supervisor-issued, fresh on every save (ADR 0069 1.1): a digest of the
    settings would let a replaced server inherit an old approval."""
    return secrets.token_hex(8)


# -- registration changes are serialized against validation and dispatch ----


class _RegistryGate:
    """Many calls at once, or one registration change alone (1.4): a call
    checks its card's incarnation and runs under ``reading``; a save or a
    remove waits for every running call under ``writing``."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._readers = 0
        self._writing = False

    @contextlib.contextmanager
    def reading(self) -> Iterator[None]:
        with self._cond:
            while self._writing:
                self._cond.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._cond:
                self._readers -= 1
                self._cond.notify_all()

    @contextlib.contextmanager
    def writing(self) -> Iterator[None]:
        with self._cond:
            while self._writing or self._readers:
                self._cond.wait()
            self._writing = True
        try:
            yield
        finally:
            with self._cond:
                self._writing = False
                self._cond.notify_all()


REGISTRY_GATE = _RegistryGate()


# -- validation ---------------------------------------------------------------


def install_spec_problem(install: tuple[str, ...]) -> str | None:
    """Why ``install`` is not ``(kind, pinned spec)``, or None (2.2)."""
    if len(install) != 2 or install[0] not in INSTALL_KINDS:
        return (
            "install must be 'npm:<package>@<exact version>' or 'pypi:<package>==<exact "
            "version>', e.g. 'npm:@modelcontextprotocol/server-everything@2025.9.25'"
        )
    kind, spec = install
    if kind == "npm" and NPM_PINNED.fullmatch(spec) is None:
        name = (
            spec.rsplit("@", 1)[0] if spec.count("@") > (1 if spec.startswith("@") else 0) else spec
        )
        return (
            f"npm spec {spec!r} is not pinned to one exact version (@latest, a range, a tag, "
            f"a git or tarball URL are refused): use 'npm:{name or '<package>'}@<x.y.z>' — "
            f"`npm view {name or '<package>'} version` prints the current one"
        )
    if kind == "pypi" and PYPI_PINNED.fullmatch(spec) is None:
        name = re.split(r"[<>=!~\[ @]", spec, maxsplit=1)[0]
        return (
            f"pypi spec {spec!r} is not pinned to one exact version (a range, no version or "
            f"a URL are refused): use 'pypi:{name or '<package>'}==<version>'"
        )
    return None


def parse_install(raw: str) -> tuple[str, ...]:
    """``'npm:pkg@1.2.3'`` → ``("npm", "pkg@1.2.3")``; ``''`` → ``()``."""
    if not raw:
        return ()
    kind, sep, spec = raw.partition(":")
    return (kind.strip().lower(), spec.strip()) if sep else (raw,)


def confinement_problem(config: MCPServerConfig) -> str | None:
    """Why ``config``'s confinement fields cannot stand, or None (1.1, 1.4).
    ``MCPServerConfig.validate`` calls this; the skep-home checks on
    ``read_paths`` are ``registration_problem``'s (they need the home)."""
    from .netproxy import host_entry_problem

    if config.sandbox not in SANDBOX_MODES:
        return f"sandbox must be 'strict' or 'host', got {config.sandbox!r}"
    if config.egress not in EGRESS_MODES:
        return f"egress must be 'none' or 'hosts', got {config.egress!r}"
    confined = (
        bool(config.allowed_hosts or config.read_paths or config.secret_names or config.install)
        or config.egress != "none"
    )
    if config.transport != "stdio":
        if config.sandbox == "strict" or confined:
            return (
                "sandbox, egress, allowed_hosts, read_paths, secret_names and install mean "
                "nothing for an http server (skep starts no process for it); leave them out"
            )
        return None
    if config.sandbox == "host":
        if confined:
            return (
                "egress, allowed_hosts, read_paths, secret_names and install mean nothing for "
                "sandbox='host' (it runs unsandboxed, as you, with serve's environment); "
                "leave them out, or use sandbox='strict'"
            )
        return None
    if SERVER_SLUG.fullmatch(config.server_id) is None:
        return (
            f"server id {config.server_id!r} becomes a directory name for a strict server: "
            "use 1-48 lowercase letters, digits, '-' or '_', starting with a letter or digit"
        )
    if config.egress == "open":
        return (
            "egress='open' is for a sandboxed browser and arrives with the browse session "
            "pool (v135 Wave 2b); until then list concrete hosts with egress='hosts', or "
            "use sandbox='host'"
        )
    if config.egress == "hosts" and not config.allowed_hosts:
        return "egress='hosts' needs allowed_hosts, e.g. ['api.example.com']"
    if config.egress == "none" and config.allowed_hosts:
        return "allowed_hosts needs egress='hosts' (egress='none' reaches no host)"
    for host in config.allowed_hosts:
        if (problem := host_entry_problem(host)) is not None:
            return f"allowed_hosts: {problem}"
    for raw in config.read_paths:
        if not Path(raw).is_absolute():
            return f"read_paths: {raw!r} is not an absolute path"
    for name in config.secret_names:
        if (problem := secret_name_problem(name)) is not None:
            return f"secret_names: {problem}"
    command = config.command
    if config.install:
        if (problem := install_spec_problem(config.install)) is not None:
            return problem
        if not command or _BIN_NAME.fullmatch(command[0]) is None:
            return (
                "with install=, command[0] is the name of the entry point the package "
                "installs (its bin / console script, e.g. 'mcp-server-everything'), run "
                "from the installed tree; the rest are its arguments"
            )
        return None
    head = command[0] if command else ""
    if Path(head).name in _RUNTIME_RESOLVERS:
        return (
            f"{head!r} resolves a package when it starts, which the strict lane never does: "
            "give install='npm:<package>@<x.y.z>' (or 'pypi:<package>==<v>') and the entry "
            "point name, or a pre-installed absolute entry point, or use sandbox='host'"
        )
    if not Path(head).is_absolute():
        return (
            f"a strict server without install= runs a pre-installed entry point named by its "
            f"absolute path; {head!r} is not one. Give the full path (e.g. /usr/local/bin/x), "
            "or install='npm:<package>@<x.y.z>' with the entry point name, or sandbox='host'"
        )
    return None


def registration_problem(config: MCPServerConfig, skep_home: Path) -> str | None:
    """``confinement_problem`` plus the checks that need the skep home: each
    operator read path is judged as ``extra_read_paths`` are (ADR 0068)."""
    from . import sandbox

    if (problem := confinement_problem(config)) is not None:
        return problem
    for raw in config.read_paths:
        if (problem := sandbox.extra_read_path_problem(Path(raw), skep_home)) is not None:
            return f"read_paths: {problem}"
    return None


def _strings(value: Any) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


def registration_from_args(args: Mapping[str, Any]) -> MCPServerConfig:
    """A NEW registration from ``register_mcp_server``'s arguments: one that
    does not name ``sandbox`` is ``strict`` (1.2; a stored registration
    without the field loads as ``host``, ``load_mcp_servers``). ``egress``
    defaults to ``hosts`` when hosts are listed, else ``none``."""
    hosts = _strings(args.get("allowed_hosts"))
    return MCPServerConfig(
        server_id=str(args.get("server_id") or ""),
        transport=str(args.get("transport") or "stdio"),
        command=_strings(args.get("command")),
        url=None if args.get("url") is None else str(args["url"]),
        scope=str(args.get("scope") or "mcp"),
        sandbox=str(args.get("sandbox") or "strict"),
        egress=str(args.get("egress") or ("hosts" if hosts else "none")),
        allowed_hosts=hosts,
        read_paths=_strings(args.get("read_paths")),
        secret_names=_strings(args.get("secret_names")),
        install=parse_install(str(args.get("install") or "")),
    )


# -- where things live ----------------------------------------------------------


def tree_root(skep_home: Path, server_id: str) -> Path:
    return skep_home / MCP_TREE_DIR / server_id


def install_tree(skep_home: Path, config: MCPServerConfig, incarnation: str | None = None) -> Path:
    """The install tree is named by the incarnation it was installed for."""
    return tree_root(skep_home, config.server_id) / (incarnation or config.incarnation)


def published_marker(skep_home: Path, server_id: str, incarnation: str) -> Path:
    """Beside the tree, never in it: only the supervisor writes it, after the
    install AND the discovery handshake succeeded (2.2) — an installed
    package can drop files anywhere under its own tree."""
    return tree_root(skep_home, server_id) / f"{incarnation}.published"


def retire_trees(skep_home: Path, server_id: str, *, keep: str | None) -> None:
    """Delete every install tree (and marker) of ``server_id`` but ``keep``'s.
    Called under ``REGISTRY_GATE.writing()``, so no call is using one."""
    import shutil

    root = tree_root(skep_home, server_id)
    if SERVER_SLUG.fullmatch(server_id) is None or root.is_symlink() or not root.is_dir():
        return
    for child in root.iterdir():
        if keep is not None and child.name in (keep, f"{keep}.published"):
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def entry_point(tree: Path, config: MCPServerConfig) -> Path:
    """The installed entry point, invoked directly (never npx or uvx)."""
    if config.install[0] == "npm":
        return tree / "node_modules" / ".bin" / config.command[0]
    return tree / "bin" / config.command[0]


def runtime_argv(
    config: MCPServerConfig, skep_home: Path, incarnation: str | None = None
) -> list[str]:
    """What runs for ``config``: the installed entry point and its arguments,
    or the pre-installed absolute command as given."""
    if not config.install:
        return list(config.command)
    tree = install_tree(skep_home, config, incarnation)
    return [str(entry_point(tree, config)), *config.command[1:]]


# -- the card -------------------------------------------------------------------


def grant_view(config: MCPServerConfig, skep_home: Path) -> dict[str, Any]:
    """The effective grant a registration card shows (1.3), built without
    starting anything: the argv that will run, mode, egress, hosts, read
    paths, secret names and install spec — or why it cannot be registered."""
    if (problem := registration_problem(config, skep_home)) is not None:
        return {"error": problem}
    view: dict[str, Any] = {"server_id": config.server_id, "mode": config.sandbox}
    if config.transport != "stdio":
        view["url"] = config.url
        return view
    view["argv"] = runtime_argv(config, skep_home, "<new incarnation>")
    if config.sandbox == "host":
        return view
    view |= {
        "egress": config.egress,
        "hosts": list(config.allowed_hosts),
        "read_paths": list(config.read_paths),
        "secret_names": list(config.secret_names),
    }
    if unset := secret_names_unset(skep_home, config):
        view["secrets_unset"] = unset
    if config.install:
        view["install"] = f"{config.install[0]}:{config.install[1]}"
        view["install_note"] = INSTALL_SENTENCE
    return view


def grant_line(view: Mapping[str, Any]) -> str:
    """The card's risk line for a registration, from ``grant_view``."""
    if "error" in view:
        return f"it cannot be registered as shown: {view['error']}"
    if "argv" not in view:
        return f"connects to {view.get('url')} over http (no process starts here)"
    argv = " ".join(str(part) for part in view["argv"])
    if view["mode"] == "host":
        return f"runs `{argv}` on this host, unsandboxed, with serve's environment"
    reads = "system and toolchain dirs only"
    if view.get("read_paths"):
        reads += f", plus {', '.join(view['read_paths'])}"
    net = "no network" if view.get("egress") == "none" else f"only {', '.join(view['hosts'])}"
    line = f"runs `{argv}` sandboxed (strict): reads {reads}; {net}"
    if view.get("secret_names"):
        line += f"; secrets: {', '.join(view['secret_names'])} ({SECRET_SENTENCE})"
    if view.get("secrets_unset"):
        server = view["server_id"]
        steps = "; ".join(
            f"`skep mcp secret set {server} {name}`" for name in view["secrets_unset"]
        )
        line += f"; NOT SET yet, set before approving: {steps}"
    if view.get("install"):
        line += (
            f"; installs {view['install']} after you approve, off this request: "
            f"{view['install_note']}"
        )
    return line


def registration_high(args: Mapping[str, Any]) -> str:
    """The HIGH card's reason for ``register_mcp_server`` (1.5)."""
    return HOST_HIGH if str(args.get("sandbox") or "strict") == "host" else STRICT_HIGH


def card_grant(tool: str, args: Mapping[str, Any], store: RunStore) -> dict[str, Any] | None:
    """The grant a ``register_mcp_server`` or ``setup_browser`` card records."""
    skep_home = store.db_path.parent.parent
    if tool == "setup_browser":
        return grant_view(browser_config(), skep_home)
    if tool == "register_mcp_server":
        return grant_view(registration_from_args(args), skep_home)
    return None


def browser_config() -> MCPServerConfig:
    """2.8: until the session pool lands a sandboxed browser cannot browse,
    so ``setup_browser`` proposes ``host`` — the only mode ``@latest`` keeps."""
    return MCPServerConfig(
        server_id="browser",
        transport="stdio",
        command=("npx", "@playwright/mcp@latest"),
        scope="browse",
        sandbox="host",
    )


def card_incarnation(store: RunStore, args: Mapping[str, Any]) -> str | None:
    """What an MCP act card binds to (1.6): the registration's incarnation as
    the SUPERVISOR reads it now, never a model-supplied value; None when no
    server is registered under the id."""
    from .mcp_client import load_mcp_servers

    config = load_mcp_servers(store).get(str(args.get("server_id") or ""))
    return None if config is None else config.incarnation


def card_binding_refusal(card: Mapping[str, Any], config: MCPServerConfig) -> str | None:
    """Why a confirmed MCP card no longer names the registration it was
    proposed for (1.6), or None. Recorded as **not run**. A card with no
    recorded incarnation (proposed before 2.4.0) is refused too."""
    if card.get("incarnation") == config.incarnation:
        return None
    return (
        f"not run: the MCP server {config.server_id!r} was registered again (or removed and "
        "re-registered) after this card was shown, so this approval is not for the server "
        "that is registered now. Nothing was sent. Ask again to get a card for it"
    )


# -- per-server secrets (2.5) ------------------------------------------------------

# Under the supervisor dir and named ``*-secret``: on the secret floor of every
# sandbox (``sandbox.secret_paths`` masks the whole dir) and of the Queen's
# file reads (``secret_floor``: each file is ``<server_id>-secret`` too).
SECRETS_DIR = "mcp-secret"
SECRET_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Names the wall sets itself, and loader controls (2.5).
_REFUSED_EXACT = frozenset(
    {"HOME", "PATH", "TMPDIR", "TMPPREFIX", "TEMP", "TMP", "XDG_RUNTIME_DIR",
     "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "LANG", "SHELL",
     "NODE_OPTIONS", "NODE_PATH", "NODE_USE_ENV_PROXY", "NODE_EXTRA_CA_CERTS",
     "PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME", "PYTHONUSERBASE",
     "PLAYWRIGHT_BROWSERS_PATH", "PIP_NO_INDEX", "PIP_INDEX_URL", "SSL_CERT_FILE",
     "SSL_CERT_DIR", "BASH_ENV", "ENV"}
)  # fmt: skip
_REFUSED_PREFIXES = ("LD_", "DYLD_", "NPM_CONFIG_", "UV_", "PIP_", "SKEP_")
_PROXY_NAMES = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"})


def secret_name_problem(name: str) -> str | None:
    """Why ``name`` cannot be a secret's environment name, or None."""
    if SECRET_NAME.fullmatch(name) is None:
        return f"{name!r} is not an environment variable name (letters, digits, '_')"
    upper = name.upper()
    if upper in _REFUSED_EXACT or upper in _PROXY_NAMES or upper.startswith(_REFUSED_PREFIXES):
        return (
            f"{name!r} is refused as a secret name: the wall sets it itself, or it controls "
            "how a program loads (HOME, PATH, TMPDIR, npm_config_*, UV_*, proxy variables, "
            "LD_*, DYLD_*, NODE_OPTIONS, NODE_PATH, PYTHONPATH, PYTHONSTARTUP). Pick the name "
            "the server reads its key from, e.g. EXAMPLE_API_KEY"
        )
    return None


def secrets_file(skep_home: Path, server_id: str) -> Path:
    return skep_home / "supervisor" / SECRETS_DIR / f"{server_id}-secret"


def load_secrets(skep_home: Path, server_id: str) -> dict[str, str]:
    """The values set for ``server_id`` (``skep mcp secret set``); {} if none."""
    import json
    import os

    path = secrets_file(skep_home, server_id)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise MCPError(f"the secrets file {path} cannot be read ({exc})") from exc
    with os.fdopen(fd, encoding="utf-8") as handle:
        try:
            data = json.load(handle)
        except ValueError as exc:
            raise MCPError(f"the secrets file {path} is not valid JSON ({exc})") from exc
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def secret_names_unset(skep_home: Path, config: MCPServerConfig) -> list[str]:
    try:
        held = load_secrets(skep_home, config.server_id)
    except MCPError:
        held = {}
    return [name for name in config.secret_names if not held.get(name)]


# -- the registration verb -------------------------------------------------------


def register(store: RunStore, args: Mapping[str, Any], *, chat_id: str | None) -> dict[str, Any]:
    """``register_mcp_server``'s executor — it runs only after the card's
    verdict (1.3). Saves the registration under a new incarnation; a strict
    server with ``install`` starts its install off this request, and the
    result is reported in ``chat_id``. Nothing else starts here."""
    from .mcp_client import FORGED_SERVER_PREFIX, save_mcp_server

    if str(args.get("server_id") or "").startswith(FORGED_SERVER_PREFIX):
        # Review R1 (K8): reserved before any transport is chosen (1.4)
        raise ValueError(
            f"server ids starting {FORGED_SERVER_PREFIX!r} are reserved for forged tools, "
            "which run only from their bound record inside the sandbox — pick another id; "
            "promote_tool registers a forged tool itself"
        )
    config = registration_from_args(args)
    skep_home = store.db_path.parent.parent
    if (problem := registration_problem(config, skep_home)) is not None:
        raise ValueError(problem)
    try:
        config = save_mcp_server(store, config)
    except MCPError as exc:
        raise ValueError(str(exc)) from exc
    result: dict[str, Any] = {
        "registered": config.server_id,
        "transport": config.transport,
        "command": list(config.command),
        "url": config.url,
        "scope": config.scope,
        "sandbox": config.sandbox,
    }
    if config.transport == "stdio" and config.sandbox == "strict":
        result |= {"egress": config.egress, "allowed_hosts": list(config.allowed_hosts)}
    if config.install and config.sandbox == "strict":
        start_install(store, config, chat_id=chat_id)
        result["install"] = (
            f"{config.install[0]}:{config.install[1]} is installing off this request; its "
            "result is reported in this chat, and calls are refused until it succeeds"
        )
    return result


# -- the run-time wall (2.3, 2.4, 2.6, 2.7) --------------------------------------

# The package managers set offline at run time (2.3), and Node told to use the
# proxy the wall names (2.6: a Node before 24 / 22.21 ignores it).
RUNTIME_ENV = {
    "npm_config_offline": "true",
    "npm_config_update_notifier": "false",
    "UV_OFFLINE": "1",
    "PIP_NO_INDEX": "1",
    "NODE_USE_ENV_PROXY": "1",
}


def sandbox_refusal(server_id: str) -> str | None:
    """2.7 (I12, I9): no backend that confines reads refuses the call, saying
    what to install; None when the strict wall can be built here."""
    from . import sandbox

    probe = sandbox.availability()
    if probe.usable and probe.backend in sandbox.STRICT_READ_BACKENDS:
        return None
    here = f"the {probe.backend} backend" if probe.usable else (probe.reason or "none usable")
    return (
        f"not run: {server_id!r} is a strict MCP server, and it runs only inside a sandbox "
        f"that confines reads (bubblewrap on Linux, sandbox-exec on macOS); here: {here}. "
        "Install bubblewrap (`sudo dnf install bubblewrap` or `sudo apt install "
        "bubblewrap`) and run `skep doctor`, or register it again with sandbox='host' "
        "(unsandboxed, a HIGH card)"
    )


def hidden_toolchains() -> tuple[dict[str, str], tuple[Path, ...]]:
    """Toolchain paths a moved HOME hides from the child, passed explicitly
    and read-only (2.3): uv's managed Pythons (already a strict root) and
    Playwright's browsers (bound as a trusted read root)."""
    import os

    home = Path.home()
    env: dict[str, str] = {}
    roots: list[Path] = []
    uv_python = Path(os.environ.get("UV_PYTHON_INSTALL_DIR") or home / ".local/share/uv/python")
    if uv_python.is_dir():
        env["UV_PYTHON_INSTALL_DIR"] = str(uv_python)
    browsers = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or home / ".cache/ms-playwright")
    if browsers.is_dir() and not browsers.is_symlink():
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)
        roots.append(browsers.resolve())
    return env, tuple(roots)


def node_note(config: MCPServerConfig, argv: list[str]) -> str:
    """2.6: when a Node server's listed host fails, the version that decides
    whether it honours the proxy; "" for anything else."""
    import shutil
    import subprocess

    node_program = config.install[:1] == ("npm",) or Path(argv[0]).name == "node"
    if config.egress != "hosts" or not node_program:
        return ""
    node = shutil.which("node")
    try:
        version = subprocess.run(
            [node or "node", "--version"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        version = "not found"
    return (
        f" (Node here: {version or 'unknown'}; a Node before 24, or 22 before 22.21, ignores "
        "NODE_USE_ENV_PROXY, so its fetch skips the proxy and every listed host fails)"
    )


def redact(value: Any, values: Mapping[str, str]) -> Any:
    """2.5: this server's own secret values, replaced wherever they appear in
    what it returned (strings, nested lists and objects)."""
    secrets_ = [v for v in values.values() if len(v) >= 4]
    if not secrets_:
        return value
    if isinstance(value, str):
        for secret in sorted(secrets_, key=len, reverse=True):
            value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, Mapping):
        return {redact(k, values): redact(v, values) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(item, values) for item in value]
    return value


def strict_exchange(
    config: MCPServerConfig,
    payload: str,
    *,
    skep_home: Path,
    secret_values: Mapping[str, str],
    require_published: bool = True,
) -> list[str]:
    """One exchange with a strict server behind the shared wall (2.3): the
    strict read allowlist plus ``read_paths`` and its install tree (a trusted
    read root), per-launch scratch as HOME and temp, its egress through the
    FilteringProxy, the package managers offline, nothing from serve's
    environment but PATH and LANG, and its own secrets. Refused before
    anything starts: no backend, an unpublished install, an unset secret."""
    import tempfile

    from . import sandbox
    from .mcp_client import MCPNotRun, _stdio_exchange
    from .wall import walled

    if (refusal := sandbox_refusal(config.server_id)) is not None:
        raise MCPNotRun(refusal)
    argv = runtime_argv(config, skep_home)
    trusted: list[Path] = []
    if config.install:
        marker = published_marker(skep_home, config.server_id, config.incarnation)
        if require_published and not marker.is_file():
            raise MCPNotRun(
                f"not run: {config.server_id!r} has no finished install for this registration "
                f"({config.install[0]}:{config.install[1]}): it is still installing, or the "
                "install failed (its result is in the chat that registered it). Register it "
                "again to retry the install"
            )
        trusted.append(install_tree(skep_home, config).resolve())
    unset = [name for name in config.secret_names if not secret_values.get(name)]
    if unset:
        steps = "; ".join(f"`skep mcp secret set {config.server_id} {n}`" for n in unset)
        raise MCPNotRun(
            f"not run: {config.server_id!r} lists secret(s) {', '.join(unset)} with no value "
            f"set. The operator sets each on this machine (never in chat): {steps}"
        )
    gone = [raw for raw in config.read_paths if not Path(raw).exists()]
    if gone:
        raise MCPNotRun(
            f"not run: read path(s) {', '.join(gone)} of {config.server_id!r} do not exist; "
            "create them, or register the server again without them"
        )
    tool_env, tool_roots = hidden_toolchains()
    hosts = config.allowed_hosts if config.egress == "hosts" else ()
    own = {name: secret_values[name] for name in config.secret_names}
    with (
        tempfile.TemporaryDirectory(prefix="skep-mcp-") as scratch,
        tempfile.TemporaryDirectory(prefix="skep-mcp-profile-") as profiles,
    ):
        try:
            with walled(
                argv,
                skep_home=skep_home,
                hosts=hosts,
                workspace=Path(scratch),
                profile_dir=Path(profiles),
                read_allowlist=tuple(Path(raw) for raw in config.read_paths),
                trusted_read_roots=(*trusted, *tool_roots),
                skep_home_roots=False,
            ) as (wrapped, wall_env):
                # the wall's own names win; the refusal is checked on the FINAL env
                env = {**own, **wall_env, **RUNTIME_ENV, **tool_env}
                for name, value in own.items():
                    if secret_name_problem(name) is not None or env.get(name) != value:
                        raise MCPNotRun(
                            f"not run: secret name {name!r} collides with a name the wall "
                            "sets; register the server again with another name"
                        )
                diagnostics: list[str] = []
                try:
                    return _stdio_exchange(
                        wrapped, payload, cwd=Path(scratch), env=env, diagnostics=diagnostics
                    )
                except MCPError as exc:
                    said = "".join(diagnostics).strip()[-800:]
                    detail = f"{exc}{node_note(config, argv)}"
                    if said:
                        detail += f". The server's stderr ended with: {said}"
                    raise type(exc)(detail) from None
        except sandbox.SandboxUnavailableError as exc:
            raise MCPNotRun(
                f"not run: the strict wall for {config.server_id!r} could not be built: {exc}"
            ) from exc


def strict_runner(config: MCPServerConfig, *, skep_home: Path | None) -> MCPRunner:
    """``runner_for_config``'s strict branch: an MCPRunner whose results and
    errors carry none of this server's own secret values (2.5)."""
    from .mcp_client import MCPNotRun, _stdio_payload, _stdio_reply

    def run(method: str, params: Mapping[str, object]) -> Mapping[str, object]:
        if skep_home is None:
            raise MCPNotRun(f"strict MCP server {config.server_id!r} needs its skep home")
        values = load_secrets(skep_home, config.server_id)
        try:
            lines = strict_exchange(
                config, _stdio_payload(method, params), skep_home=skep_home, secret_values=values
            )
            reply = _stdio_reply(lines)
        except MCPError as exc:
            raise type(exc)(redact(str(exc), values)) from None
        return dict(redact(reply, values))

    return run


# -- the install lane (2.2) --------------------------------------------------------


@dataclass(frozen=True)
class InstallSource:
    """Where one package manager installs from: the registry hosts its wall
    may reach (nothing else) and the installer flags that name them."""

    hosts: tuple[str, ...]
    args: tuple[str, ...]


# uv needs the index AND the file host; npm one registry.
INSTALL_SOURCES: dict[str, InstallSource] = {
    "npm": InstallSource(("registry.npmjs.org",), ("--registry", "https://registry.npmjs.org/")),
    "pypi": InstallSource(
        ("pypi.org", "files.pythonhosted.org"), ("--index-url", "https://pypi.org/simple")
    ),
}
INSTALL_TIMEOUT_SECONDS = 300.0
INSTALL_LIMITS = (
    "This lane installs with no package scripts, from wheels only (no source builds), and "
    "from the registry only (a dependency given as a git or tarball URL cannot be fetched). "
    "A package that needs one of those does not install here: register it with "
    "sandbox='host' (unsandboxed, a HIGH card), or install it yourself and register its "
    "absolute entry point (command=['/abs/path/to/server', ...])"
)
_INSTALLS: dict[tuple[str, str], threading.Thread] = {}


def _private_dir(parent: Path, name: str, *, fresh: bool = False) -> Path:
    """``parent/name`` as a real directory of its own: made without following
    a link, and checked to resolve inside ``parent`` (1.1)."""
    import os
    import stat

    path = parent / name
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        if fresh:
            raise MCPError(f"{path} already exists; register the server again") from None
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or path.resolve().parent != parent.resolve():
        raise MCPError(f"{path} is not a plain directory inside {parent}; remove it")
    return path


def _installer_steps(config: MCPServerConfig, tree: Path) -> list[list[str]]:
    """The argv the install wall runs, in order (2.2): the installer, never
    the package — npm with --ignore-scripts, uv wheels only."""
    import shutil
    import sys

    kind, spec = config.install
    source = INSTALL_SOURCES[kind]
    if kind == "npm":
        npm = shutil.which("npm")
        if npm is None:
            raise MCPError("npm is not on PATH here; install Node.js (it brings npm)")
        return [[npm, "install", "--ignore-scripts", "--no-audit", "--no-fund",
                 "--prefix", str(tree), *source.args, spec]]  # fmt: skip
    uv = shutil.which("uv")
    if uv is None:
        raise MCPError("uv is not on PATH here; install uv (https://docs.astral.sh/uv/)")
    python = tree / "bin" / "python"
    return [
        [uv, "venv", "--allow-existing", "--python", sys.executable, str(tree)],
        [uv, "pip", "install", "--python", str(python), "--only-binary", ":all:",
         *source.args, spec],
    ]  # fmt: skip


def _installer_env(scratch: Path, wall_env: Mapping[str, str]) -> dict[str, str]:
    proxy = wall_env.get("HTTPS_PROXY", "")
    tool_env, _ = hidden_toolchains()
    return {
        **wall_env,
        **tool_env,
        "npm_config_cache": str(scratch / "npm-cache"),
        "npm_config_userconfig": str(scratch / "npmrc"),
        "npm_config_ignore_scripts": "true",
        "npm_config_update_notifier": "false",
        "npm_config_proxy": proxy,
        "npm_config_https_proxy": proxy,
        "UV_CACHE_DIR": str(scratch / "uv-cache"),
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_LINK_MODE": "copy",
        "UV_NO_CONFIG": "1",
    }


def _run_installer(config: MCPServerConfig, tree: Path, skep_home: Path, timeout: float) -> None:
    """Each installer step inside its own strict wall: scrubbed env,
    toolchain reads only, private scratch as HOME, only ``tree`` writable,
    the registry hosts only, no read_paths and no secrets (2.2)."""
    import subprocess
    import tempfile
    import time

    from . import sandbox
    from .wall import walled

    kind, spec = config.install
    deadline = time.monotonic() + timeout
    with (
        tempfile.TemporaryDirectory(prefix="skep-mcp-install-") as scratch,
        tempfile.TemporaryDirectory(prefix="skep-mcp-profile-") as profiles,
    ):
        for argv in _installer_steps(config, tree):
            try:
                with walled(
                    argv,
                    skep_home=skep_home,
                    hosts=INSTALL_SOURCES[kind].hosts,
                    workspace=tree,
                    home=Path(scratch),
                    profile_dir=Path(profiles),
                    read_allowlist=(),
                    skep_home_roots=False,
                ) as (wrapped, wall_env):
                    proc = subprocess.run(
                        wrapped,
                        cwd=tree,
                        env=_installer_env(Path(scratch), wall_env),
                        capture_output=True,
                        text=True,
                        timeout=max(1.0, deadline - time.monotonic()),
                    )
            except sandbox.SandboxUnavailableError as exc:
                raise MCPError(f"the install wall could not be built: {exc}") from exc
            except subprocess.TimeoutExpired:
                raise MCPError(
                    f"the {kind} install of {spec} passed its {timeout:g}s limit"
                ) from None
            if proc.returncode != 0:
                said = (proc.stderr or proc.stdout).strip()[-1200:]
                raise MCPError(
                    f"the {kind} install of {spec} failed (exit {proc.returncode}): {said}. "
                    f"{INSTALL_LIMITS}"
                )


def _require_current(store: RunStore, config: MCPServerConfig) -> None:
    from .mcp_client import load_mcp_servers

    current = load_mcp_servers(store).get(config.server_id)
    if current is None or current.incarnation != config.incarnation:
        raise MCPError("it was registered again or removed while it installed")


def install_server(
    store: RunStore, config: MCPServerConfig, *, timeout: float = INSTALL_TIMEOUT_SECONDS
) -> tuple[str, ...]:
    """Install ``config``'s pinned package straight into its incarnation's
    tree, run the discovery handshake there, then publish by writing the
    marker beside the tree (2.2; nothing is renamed, so a Python install's
    absolute shebangs stay true). Any failure deletes the tree: nothing is
    published. Returns the tool names the handshake listed."""
    import json
    import os
    import shutil

    from .mcp_client import _LISTED, MCPClient, MCPTool, _stdio_payload, _stdio_reply

    skep_home = store.db_path.parent.parent
    if (refusal := sandbox_refusal(config.server_id)) is not None:
        raise MCPError(refusal.removeprefix("not run: "))
    if unset := secret_names_unset(skep_home, config):
        steps = "; ".join(f"`skep mcp secret set {config.server_id} {n}`" for n in unset)
        raise MCPError(
            f"secret(s) {', '.join(unset)} have no value, so the handshake could not pass and "
            f"nothing was downloaded. Set them on this machine ({steps}), then ask to "
            "register the server again"
        )
    root = _private_dir(_private_dir(skep_home, MCP_TREE_DIR), config.server_id)
    tree = _private_dir(root, config.incarnation, fresh=True)
    try:
        _run_installer(config, tree, skep_home, timeout)
        _require_current(store, config)
        entry = entry_point(tree, config)
        if not entry.exists():
            bins = entry.parent
            offered = sorted(p.name for p in bins.iterdir()) if bins.is_dir() else []
            raise MCPError(
                f"the package installed, but has no entry point named {config.command[0]!r} "
                f"(it offers: {', '.join(offered) or 'none'}); register it again with "
                "command[0] set to one of those"
            )
        values = load_secrets(skep_home, config.server_id)

        def handshake(method: str, params: Mapping[str, object]) -> Mapping[str, object]:
            lines = strict_exchange(
                config, _stdio_payload(method, params), skep_home=skep_home,
                secret_values=values, require_published=False,
            )  # fmt: skip
            return dict(redact(_stdio_reply(lines), values))

        tools: list[MCPTool] = MCPClient(config, runner=handshake).list_tools()
        with REGISTRY_GATE.writing():
            _require_current(store, config)
            marker = published_marker(skep_home, config.server_id, config.incarnation)
            staged = marker.with_suffix(".tmp")
            staged.write_text(json.dumps({"entry": str(entry)}), encoding="utf-8")
            os.replace(staged, marker)
            _LISTED[(str(store.db_path), config.server_id)] = (config, (), tuple(tools))
    except BaseException:
        shutil.rmtree(tree, ignore_errors=True)
        raise
    return tuple(tool.name for tool in tools)


def start_install(store: RunStore, config: MCPServerConfig, *, chat_id: str | None) -> None:
    """Off the request path (2.2): the install runs on its own thread and its
    result lands in ``chat_id`` as an outside line (installer output is not
    the Queen's words)."""
    import logging

    spec = f"{config.install[0]}:{config.install[1]}"

    def job() -> None:
        try:
            names = install_server(store, config)
            text = (
                f"MCP server {config.server_id!r} installed ({spec}) and answered its handshake "
                f"inside the strict wall: {len(names)} tool(s): {', '.join(names) or 'none'}."
            )
        except Exception as exc:  # reported, never raised into a dead thread
            text = (
                f"MCP server {config.server_id!r} was NOT installed ({spec}): {exc}. Nothing "
                "was published; its calls are refused until a fresh registration installs."
            )
        try:
            if chat_id is not None and store.get_chat(chat_id) is not None:
                store.add_chat_message(
                    chat_id, role="user", content=text, provenance=f"mcp-install:{config.server_id}"
                )
            else:
                logging.getLogger("skep.serve").warning("%s", text)
        except Exception:  # the store closed under us (serve shutting down)
            logging.getLogger("skep.serve").warning("%s", text)

    thread = threading.Thread(target=job, name=f"skep-mcp-install-{config.server_id}", daemon=True)
    _INSTALLS[(str(store.db_path), config.server_id)] = thread
    thread.start()


def wait_for_install(store: RunStore, server_id: str, timeout: float | None = None) -> None:
    """Join the install thread of ``server_id`` (tests and the CLI)."""
    thread = _INSTALLS.get((str(store.db_path), server_id))
    if thread is not None:
        thread.join(timeout)
