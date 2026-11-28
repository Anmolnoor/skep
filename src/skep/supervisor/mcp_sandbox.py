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
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .mcp_client import MCPError, MCPServerConfig

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
    return result
