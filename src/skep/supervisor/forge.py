"""v71-F1: the forge — skep authors its own MCP tools.

The three subsystems this joins already existed; none was connected:

- worker-authored code landing as a patch via human approval (I1),
- the v17 plugin lifecycle state machine (``plugin_lifecycle.py`` — a
  complete governed ladder that, until now, had no consumer),
- the MCP registry (``mcp_client.py``) — activation IS registration, so a
  forged tool's calls decide under the one policy engine (I5), and
  suspension IS deregistration: registered ⟺ active, one authorization
  surface, no shadow state.

A forged tool is ONE Python file (stdlib-only unless it opts into a locked
PEP 723 dependency list, Phase 07) in the operator's forge repo
(``<skep home>/forge``), written by a normal coding run, landed by a normal
approval, then promoted through the lifecycle: a trial behind the
forged-tool wall (``tools/list`` plus a mandatory zero-argument
``self_test`` call, offline; then pinned to any approved hosts) gates
``tested`` — the supervisor reads every reply itself, never the worker's
word (I2); the operator's confirmed card gates ``approved`` (I6);
activation installs the landed source under ``<skep home>/tools/`` and
registers it as a stdio MCP server that only ever runs from its bound
record (``runtime_launch``).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .store import RunStore

PLUGINS_SETTINGS_KEY = "plugins"
FORGE_DIR_NAME = "forge"
INSTALLED_TOOLS_DIR_NAME = "tools"
TRIAL_MARKER = "FORGE_TRIAL "  # a skill pack's trial evidence (skill_packs.py)
GATE_MARKER = "FORGE_GATE "
# Review R1 (K1): the dispatched half of a trial is the R3-F2 role/chain gate
# and nothing else — it runs this line, never the candidate. Every candidate
# execution is behind forged_lane (``wall_trial``).
GATE_SCRIPT = f'print("{GATE_MARKER}the role chain admitted this trial")\n'
# Review R1 (K7): a promotion's last look at its record and its publish, and
# a suspend/rollback, happen one at a time — a verdict given elsewhere while
# a trial ran is never overwritten. Review R2 (L5): every plugin record write
# takes it too (``save_plugin`` rewrites the whole list), so it is reentrant.
PROMOTION_LOCK = threading.RLock()


@dataclass(frozen=True)
class ForgedPlugin:
    """One skep-authored MCP tool server and where it stands in the lifecycle."""

    plugin_id: str
    name: str
    purpose: str
    state: str
    repo: str  # host path of the forge repo
    rel_path: str  # the tool file inside the repo, e.g. tools/word-count.py
    task_id: str  # the authoring run whose landed patch is the source of truth
    server_id: str
    provenance: str = "forge"
    trial: dict[str, Any] | None = None  # last trial evidence, verbatim
    # Phase 07 (7.1/7.2, I6): what the operator's promotion card bound. Every
    # launch is re-derived from these alone (``runtime_launch``) — never from
    # the registered command, never from a looser default after a restart.
    approved_hosts: tuple[str, ...] = ()  # () = deny-all egress
    source_sha256: str = ""  # "" = activated before the binding: refused until re-promoted
    lock_sha256: str = ""  # "" = stdlib-only: no venv
    venv_sha256: str = ""  # review R1 (K5): the sealed venv tree the install produced


def plugin_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        raise ValueError("the tool name must contain at least one letter or digit")
    return slug


def forge_root(config: Any) -> Path:
    # config.home is <SKEP_HOME>/supervisor (build_config); forge sits beside it.
    return Path(config.home).parent / FORGE_DIR_NAME


def installed_tools_root(config: Any) -> Path:
    return Path(config.home).parent / INSTALLED_TOOLS_DIR_NAME


def load_plugins(store: RunStore) -> dict[str, ForgedPlugin]:
    raw = store.get_setting(PLUGINS_SETTINGS_KEY)
    if not raw:
        return {}
    entries = json.loads(raw) if isinstance(raw, str) else raw
    plugins: dict[str, ForgedPlugin] = {}
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("plugin_id"):
                continue
            hosts = entry.get("approved_hosts")
            record = ForgedPlugin(
                plugin_id=str(entry["plugin_id"]),
                name=str(entry.get("name") or entry["plugin_id"]),
                purpose=str(entry.get("purpose") or ""),
                state=str(entry.get("state") or "draft"),
                repo=str(entry.get("repo") or ""),
                rel_path=str(entry.get("rel_path") or ""),
                task_id=str(entry.get("task_id") or ""),
                server_id=str(entry.get("server_id") or f"forge-{entry['plugin_id']}"),
                provenance=str(entry.get("provenance") or "forge"),
                trial=entry.get("trial") if isinstance(entry.get("trial"), dict) else None,
                approved_hosts=tuple(map(str, hosts)) if isinstance(hosts, list) else (),
                source_sha256=str(entry.get("source_sha256") or ""),
                lock_sha256=str(entry.get("lock_sha256") or ""),
                venv_sha256=str(entry.get("venv_sha256") or ""),
            )
            plugins[record.plugin_id] = record
    return plugins


def save_plugin(store: RunStore, record: ForgedPlugin) -> None:
    with PROMOTION_LOCK:  # L5: a whole-list rewrite never drops another write
        plugins = load_plugins(store)
        plugins[record.plugin_id] = record
        store.set_setting(PLUGINS_SETTINGS_KEY, json.dumps([asdict(p) for p in plugins.values()]))


def authoring_instructions(name: str, purpose: str, rel_path: str) -> str:
    """The coding worker's brief. Every line is enforced by the trial (``wall_trial``)."""
    lock = lock_rel_path(rel_path)
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    return f"""Create the file {rel_path} — a single-file stdio MCP tool server named {name!r}.

Purpose: {purpose}

The contract (the supervisor's sandboxed trial enforces every line):
- Python 3, STANDARD LIBRARY ONLY by default. Only if a third-party package
  is truly needed: declare it pinned in a PEP 723 block at the top of the
  file (`# /// script`, `# dependencies = ["name==1.2.3"]`, `# ///`) AND
  write its hash lock to {lock} with `uv pip compile --generate-hashes
  --only-binary :all: --python-version {version} -o {lock} -` (requirements
  on stdin). skep installs wheels only, matched to that lock's hashes.
- NO network by default: the tool runs behind a deny-all wall. A tool that
  must reach the internet names every host on one line, e.g.
  `# skep-hosts: api.example.com` — the operator approves exactly that list
  and the tool can reach nothing else.
- Speak JSON-RPC 2.0 over stdio: read one JSON request per line from stdin,
  write one JSON response per line to stdout and flush it. NOTHING else may
  be printed to stdout — diagnostics go to stderr.
- Handle these methods, replying {{"jsonrpc": "2.0", "id": <request id>, "result": <result>}}:
  * "initialize" -> result {{"protocolVersion": "2024-11-05", "capabilities": {{}}}}
  * "tools/list" -> result {{"tools": [{{"name": ..., "description": ...,
    "inputSchema": ...}}, ...]}}
  * "tools/call" (params {{"name": ..., "arguments": {{...}}}}) -> result
    {{"content": <result text or data>}} on success, or {{"content": <what went
    wrong and what a valid call looks like>, "isError": true}} on failure.
    A bad call gets an error REPLY — the server never crashes.
- Exit cleanly when stdin reaches EOF.
- Expose the real tool(s) that serve the purpose above, PLUS one extra tool
  named "self_test" that takes NO arguments: it exercises the core logic
  offline (the trial runs with NO network access) and returns proof it works.
  A tool with `# skep-hosts:` may add a zero-argument "smoke_test" that
  reaches those hosts; the trial's second stage calls it.
- Tool descriptions are load-bearing: write each one as the complete manual
  for a small model — what the tool does, its arguments, and an example.

The file examples/echo_server.py in this repo is the exact shape to follow.
Do not modify any other file."""


# The reference server the forge repo is seeded with: the contract, executable.
# It is also the fixture the forge's own tests hold the trial contract against.
ECHO_SERVER_SOURCE = '''"""echo — the forge's reference MCP server: the contract, executable."""

import json
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "Echo text back unchanged. Arguments: {text: string}. "
        "Example: {name: echo, arguments: {text: hi}} returns hi.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "self_test",
        "description": "Zero-argument offline self-check; returns proof the "
        "core logic works. Every forged server must expose one.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def call_tool(name, arguments):
    if name == "echo":
        if "text" not in arguments:
            return {
                "content": "echo needs {text: string}, e.g. {text: hi}",
                "isError": True,
            }
        return {"content": str(arguments["text"])}
    if name == "self_test":
        probe = call_tool("echo", {"text": "forge-self-test"})
        if probe.get("content") == "forge-self-test":
            return {"content": "self_test passed: echo round-trip intact"}
        return {"content": "echo round-trip broken", "isError": True}
    known = ", ".join(tool["name"] for tool in TOOLS)
    return {"content": "no tool named " + repr(name) + "; tools: " + known, "isError": True}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        method = request.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = request.get("params") or {}
            try:
                result = call_tool(str(params.get("name")), params.get("arguments") or {})
            except Exception as exc:  # a tool error is a reply, never a crash
                result = {"content": type(exc).__name__ + ": " + str(exc), "isError": True}
        else:
            result = {}
        print(json.dumps({"jsonrpc": "2.0", "id": request.get("id"), "result": result}), flush=True)


if __name__ == "__main__":
    main()
'''

FORGE_README = """# The forge

MCP tool servers skep authors for itself. Each tool is one stdlib-only
Python file under tools/, written by a coding run, landed by a human
approval, and activated only after a sandboxed trial plus a confirmed
promotion card. examples/echo_server.py is the contract, executable.
"""


def ensure_forge_seed(root: Path) -> bool:
    """Create the forge repo directory and its seed files. True if seeded now."""
    example = root / "examples" / "echo_server.py"
    if example.exists():
        return False
    (root / "examples").mkdir(parents=True, exist_ok=True)
    readme = root / "README.md"
    if not readme.exists():
        readme.write_text(FORGE_README, encoding="utf-8")
    example.write_text(ECHO_SERVER_SOURCE, encoding="utf-8")
    return True


def seed_tools_root() -> Path:
    """v83-F14: shipped seed tool servers — src/skep/seeds/tools/."""
    return Path(__file__).resolve().parents[1] / "seeds" / "tools"


def seed_tool_source(rel_path: str) -> str:
    """A seed plugin's source of truth: the versioned package file. The SAME
    trial and confirmed promotion card gate it — only the source location
    differs from a forge-authored tool (which reads its landed branch)."""
    path = seed_tools_root() / rel_path
    if not path.is_file():
        raise ValueError(
            f"seed tool source {rel_path!r} is missing from this build — "
            "reinstall skep or forge_tool a replacement"
        )
    return path.read_text(encoding="utf-8")


def sync_seed_tools(store: RunStore) -> list[str]:
    """Register draft plugin records for shipped seed tools not yet known.

    Only ABSENT ids are added — a suspended or rolled-back record is the
    operator's decision and is never resurrected (I8). Draft is inert:
    nothing runs until the operator's promote_tool card passes the trial."""
    plugins = load_plugins(store)
    added: list[str] = []
    root = seed_tools_root()
    if not root.is_dir():
        return added
    for path in sorted(root.glob("*.py")):
        plugin_id = plugin_slug(path.stem)
        if plugin_id in plugins:
            continue
        first_line = path.read_text(encoding="utf-8").splitlines()[0] if path.exists() else ""
        purpose = first_line.strip('"# ').split("—")[-1].strip() or path.stem
        save_plugin(
            store,
            ForgedPlugin(
                plugin_id=plugin_id,
                name=path.stem,
                purpose=purpose,
                state="draft",
                repo="",
                rel_path=path.name,
                task_id="",
                server_id=f"forge-{plugin_id}",
                provenance="seed",
            ),
        )
        added.append(plugin_id)
    return added


def seed_tool_updates(store: RunStore, config: Any) -> list[str]:
    """v126-F1: ACTIVE seed plugins whose installed copy differs from the
    source this build ships. sync_seed_tools only adds unknown ids, so a
    fixed seed never reached an install where it was already active;
    promote_tool takes the update (same trial, same card)."""
    waiting: list[str] = []
    for record in load_plugins(store).values():
        if record.provenance != "seed" or record.state != "active":
            continue
        installed = installed_tools_root(config) / f"{record.plugin_id}.py"
        current = installed.read_text(encoding="utf-8") if installed.is_file() else None
        try:
            shipped = seed_tool_source(record.rel_path)
        except ValueError:
            continue  # nothing shipped to update to
        if current != shipped:
            waiting.append(record.plugin_id)
    return sorted(waiting)


def landed_source(repo: Path, branch: str, rel_path: str) -> str:
    """The tool source EXACTLY as the approved landing left it (git show)."""
    source = _git_show(repo, branch, rel_path)
    if source is None:
        raise ValueError(
            f"{rel_path} is not on the landed branch {branch!r} of {repo} — the "
            "authoring run must create exactly that file; re-forge if it wrote "
            "elsewhere"
        )
    return source


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


VENVS_DIR_NAME = ".venvs"


def venv_python(skep_home: Path, plugin_id: str, lock_sha256: str) -> Path:
    """A locked tool's interpreter: one venv per (plugin, lock) under the
    ``<skep home>/tools/.venvs`` root the strict wall reads (ADR 0068)."""
    return (
        (skep_home / INSTALLED_TOOLS_DIR_NAME / VENVS_DIR_NAME / plugin_id / lock_sha256)
        / "bin"
        / "python"
    )


@dataclass(frozen=True)
class ForgedLaunch:
    source: str
    python: str
    hosts: tuple[str, ...]


def runtime_launch(store: RunStore, skep_home: Path, server_id: str) -> ForgedLaunch:
    """Phase 07 (7.1, I6): what an active forged tool runs as — re-derived
    from its bound record on EVERY launch: the approved bytes (sha256), the
    approved hosts (``()`` = deny-all) and its locked venv. The registered
    command is never read, so neither a stale config nor a restart can widen
    it. Anything unbound refuses (ValueError) with the verb that fixes it."""
    record = next((p for p in load_plugins(store).values() if p.server_id == server_id), None)
    if record is None:
        raise ValueError(
            f"{server_id!r} has no forged plugin behind it, and a forge-* server never "
            "runs without its bound record — forge_tool creates one"
        )
    plugin = record.plugin_id
    if record.state != "active":
        raise ValueError(
            f"forged plugin {plugin!r} is {record.state}, not active — promote_tool {plugin} "
            "trials and activates it"
        )
    if not record.source_sha256:
        raise ValueError(
            f"forged plugin {plugin!r} was activated with no approved digest (before skep "
            f"bound forged tools to their promotion card), so it does not run — promote_tool "
            f"{plugin} re-trials it and shows its digest, hosts and lock on one card"
        )
    installed = skep_home / INSTALLED_TOOLS_DIR_NAME / f"{plugin}.py"
    try:
        raw = installed.read_bytes()
    except OSError:
        raw = None
    if raw is None or hashlib.sha256(raw).hexdigest() != record.source_sha256:
        raise ValueError(
            f"the installed copy of {plugin!r} ({installed}) is not the source its card "
            f"approved (sha256 {record.source_sha256[:12]}…), so it does not run — "
            f"promote_tool {plugin} trials the current source and asks again"
        )
    python = sys.executable
    if record.lock_sha256:
        locked = venv_python(skep_home, plugin, record.lock_sha256)
        try:
            sealed = bool(record.venv_sha256) and seal_venv(locked.parent.parent) == (
                record.venv_sha256
            )
        except (OSError, ValueError):
            sealed = False
        if not sealed:
            raise ValueError(
                f"the locked environment of {plugin!r} ({locked.parent.parent}) is missing or "
                "is not the tree its install sealed, so it does not run — promote_tool "
                f"{plugin} rebuilds it from its approved lock"
            )
        python = str(locked)
    return ForgedLaunch(raw.decode("utf-8"), python, record.approved_hosts)


def trial_verdict(run_result: Mapping[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    """Supervisor-side reading of a skill pack's trial run (``TRIAL_MARKER``).

    The evidence line is parsed HERE (I2): neither the script's exit code nor
    any claim inside the sandbox gates the transition on its own — a missing
    or unparseable evidence line fails, honestly."""
    evidence: dict[str, Any] = {}
    for line in str(run_result.get("output") or "").splitlines():
        line = line.strip()
        if not line.startswith(TRIAL_MARKER):
            continue
        try:
            parsed = json.loads(line[len(TRIAL_MARKER) :])
        except ValueError:
            continue
        if isinstance(parsed, dict):
            evidence = parsed
    state = str(run_result.get("state") or "")
    if state != "completed":
        detail = str(run_result.get("error") or run_result.get("stderr") or "").strip()
        reason = f"the trial run ended {state or 'without a terminal state'}"
        return False, f"{reason}: {detail}" if detail else reason, evidence
    if not evidence:
        return False, "the trial produced no evidence line — the harness itself failed", evidence
    if evidence.get("ok") is True:
        return True, "trial passed", evidence
    return False, str(evidence.get("error") or "trial failed"), evidence


def gate_verdict(run_result: Mapping[str, Any]) -> tuple[bool, str]:
    """Review R1 (K1): the dispatched gate's outcome, read supervisor-side.
    It ran ``GATE_SCRIPT`` — no candidate code — so passing it means only
    that the Queen's role chain may run a script (R3-F2); the contract is
    judged behind the wall by ``wall_trial``."""
    state = str(run_result.get("state") or "")
    if state != "completed":
        detail = str(run_result.get("error") or run_result.get("stderr") or "").strip()
        reason = f"the trial's role gate ended {state or 'without a terminal state'}"
        return False, f"{reason}: {detail}" if detail else reason
    if GATE_MARKER not in str(run_result.get("output") or ""):
        return False, "the trial's role gate printed no marker — the script lane itself failed"
    return True, "the role chain admitted the trial"


# --- Phase 07 (7.2): opt-in locked dependencies + the card binding ---------

# PEP 723's reference regex: one ``# /// script`` block of ``#``-prefixed TOML.
_PEP723_BLOCK = re.compile(
    r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$"
)
_HOSTS_LINE = re.compile(r"(?m)^# skep-hosts:(?P<hosts>.*)$")
_LABEL = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
_HOST_ENTRY = re.compile(rf"(?:\*\.)?{_LABEL}(?:\.{_LABEL})*(?::[0-9]{{1,5}})?")
# What a lock may hold: ``name==version [; marker] --hash=sha256:…`` lines.
_LOCK_LINE = re.compile(
    r"(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)(?P<extras>\[[A-Za-z0-9._,-]+\])?"
    r"==(?P<version>[A-Za-z0-9.+!_-]+)"
    r"(?:\s*;\s*(?P<marker>[^-\\]+?))?"
    r"(?P<hashes>(?:\s+--hash=sha256:[0-9a-f]{64})+)"
)
# Review R1 (K13): a marker is PEP 508 environment markers and nothing else —
# the named variables, comparison operators, quoted literals, and/or/not and
# parentheses.
_MARKER_TOKEN = re.compile(
    r"\s+|\(|\)|===|==|!=|<=|>=|~=|<|>|\b(?:and|or|not|in)\b|\"[^\"\\]*\"|'[^'\\]*'|\b(?:"
    r"python_version|python_full_version|os_name|sys_platform|platform_release|"
    r"platform_system|platform_version|platform_machine|platform_python_implementation|"
    r"implementation_name|implementation_version|extra)\b"
)
PYPI_HOSTS = ("pypi.org", "files.pythonhosted.org")
INSTALL_TIMEOUT_SECONDS = 300.0
# The triple a promotion card binds (I6): any change asks again.
BOUND_KEYS = ("source_sha256", "lock_sha256", "approved_hosts")


def script_dependencies(source: str) -> tuple[str, ...]:
    """A tool's PEP 723 ``dependencies`` — () for a stdlib-only tool."""
    import tomllib

    blocks = [m for m in _PEP723_BLOCK.finditer(source) if m.group("type") == "script"]
    if len(re.findall(r"(?m)^# /// script$", source)) > 1:
        raise ValueError("a forged tool may hold only one `# /// script` block (PEP 723)")
    if not blocks:
        return ()
    content = "".join(
        line[2:] if line.startswith("# ") else line[1:]
        for line in blocks[0].group("content").splitlines(keepends=True)
    )
    try:
        deps = tomllib.loads(content).get("dependencies", [])
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"the `# /// script` block is not valid TOML: {exc}") from exc
    if not isinstance(deps, list) or not all(isinstance(dep, str) for dep in deps):
        raise ValueError(
            "the `# /// script` block's dependencies must be a list of strings, e.g. "
            'dependencies = ["pypdf==5.1.0"]'
        )
    return tuple(deps)


def declared_hosts(source: str) -> tuple[str, ...]:
    """Every host a ``# skep-hosts:`` line names, canonical (sorted, lower,
    deduplicated) so the bound tuple compares stably. ``*`` — every host —
    is never granted to a forged tool; ``*.example.com`` names a domain."""
    hosts: set[str] = set()
    for line in _HOSTS_LINE.finditer(source):
        for raw in re.split(r"[,\s]+", line.group("hosts").strip()):
            if not raw:
                continue
            if not _HOST_ENTRY.fullmatch(raw.lower()):
                raise ValueError(
                    f"`# skep-hosts:` entry {raw!r} is not a host name — list hosts like "
                    "`api.example.com`, `*.example.com` or `host:8443`; `*` (every host) is "
                    "never granted to a forged tool"
                )
            hosts.add(raw.lower())
    return tuple(sorted(hosts))


def parse_lock(text: str) -> tuple[str, ...]:
    """The lock's requirements as ``name==version [; marker] sha256:…`` lines.

    Strict on purpose: a requirements file can also carry options
    (``--index-url``), includes (``-r``), editables, paths and URLs — each a
    way past wheels-from-PyPI-by-hash. Only hashed pins pass."""
    logical: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = re.sub(r"(^|\s)#.*$", "", raw).rstrip()
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        line = (pending + line).strip()
        pending = ""
        if line:
            logical.append(line)
    if pending.strip():
        logical.append(pending.strip())
    pins: list[str] = []
    for line in logical:
        match = _LOCK_LINE.fullmatch(line)
        marker = (match.group("marker") or "").strip() if match else ""
        if match is None or (marker and not _is_marker(marker)):
            raise ValueError(
                f"lock line {line[:120]!r} is not a pinned, hash-locked requirement — the lock "
                "may hold only `name==version [; PEP 508 marker] --hash=sha256:…` lines "
                "(regenerate it with `uv pip compile --generate-hashes --only-binary :all:`)"
            )
        hashes = " ".join(h.removeprefix("--hash=") for h in match.group("hashes").split())
        pin = f"{match.group('name')}{match.group('extras') or ''}=={match.group('version')}"
        pins.append(f"{pin}{f' ; {marker}' if marker else ''} {hashes}")
    if not pins:
        raise ValueError("the lock lists no requirements — regenerate it from the tool's deps")
    return tuple(pins)


def _is_marker(marker: str) -> bool:
    pos = 0
    while pos < len(marker):
        token = _MARKER_TOKEN.match(marker, pos)
        if token is None or token.end() == pos:
            return False
        pos = token.end()
    return True


def _project(requirement: str) -> str:
    """PEP 503: the normalized project name a requirement or pin names."""
    name = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
    return re.sub(r"[-_.]+", "-", name.group(1)).lower() if name else requirement


# Review R2 (L12): a declared dependency is one exact pin — no range, no
# marker — so the lock can be held to it with no resolver (``packaging`` is
# not a skep dependency): every lock line for that project pins that version,
# unconditionally, for the one environment the tool runs in.
_DECLARED_PIN = re.compile(
    r"\s*(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)(?:\[[A-Za-z0-9._,\s-]+\])?"
    r"\s*==\s*(?P<version>[A-Za-z0-9.+!_-]+)\s*"
)


def _require_locked_as_declared(dep: str, locked: tuple[str, ...]) -> None:
    declared = _DECLARED_PIN.fullmatch(dep)
    if declared is None:
        raise ValueError(
            f"dependency {dep!r} is not pinned exactly — declare each one as name==1.2.3 (no "
            "range, no marker: the tool runs on this skep's Python only), so the lock must "
            "install exactly what the card shows"
        )
    version = declared.group("version").lower()
    for pin in locked:
        if _project(pin) != _project(dep):
            continue
        requirement, _, marker = pin.partition(" sha256:")[0].partition(" ; ")
        if marker:
            raise ValueError(
                f"the lock pins {requirement} only under a marker ({marker}) — regenerate "
                f"<tool>.lock.txt for this Python (`uv pip compile --python-version "
                f"{sys.version_info.major}.{sys.version_info.minor}`) so {dep} installs as declared"
            )
        if requirement.partition("==")[2].lower() != version:
            raise ValueError(
                f"the tool declares {dep} but its lock pins {requirement} — declare the exact "
                f"version the lock pins ({requirement}) in the tool's `# /// script` block, or "
                "regenerate <tool>.lock.txt for the declared version"
            )


def install_lock(pins: tuple[str, ...]) -> str:
    """Review R1 (K13): what the install lane is handed — the canonical pins
    ``parse_lock`` accepted, re-written, never the raw lock text."""
    lines = []
    for pin in pins:
        requirement, _, hashes = pin.partition(" sha256:")
        digests = ("sha256:" + hashes).split()
        lines.append(f"{requirement} " + " ".join(f"--hash={d}" for d in digests))
    return "\n".join(lines) + "\n"


def lock_rel_path(rel_path: str) -> str:
    """``tools/pdf.py`` → ``tools/pdf.lock.txt``: the lock lands beside its tool."""
    from pathlib import PurePosixPath

    return str(PurePosixPath(rel_path).with_suffix(".lock.txt"))


def promotion_binding(source: str, lock: str | None) -> dict[str, Any]:
    """What a promotion card binds (I6): the source digest, the lock digest
    ("" for a stdlib-only tool — no venv) and the approved hosts, plus what
    the card shows beside them. Refuses a tool it cannot bind."""
    deps = script_dependencies(source)
    hosts = declared_hosts(source)
    if deps and lock is None:
        raise ValueError(
            f"the tool declares dependencies ({', '.join(deps)}) but no hash lock landed "
            "beside it (<tool>.lock.txt) — the authoring run writes it with `uv pip compile "
            "--generate-hashes --only-binary :all:`; skep installs only locked wheels"
        )
    locked = parse_lock(lock) if deps and lock is not None else ()
    pinned = {_project(pin) for pin in locked}
    unpinned = [dep for dep in deps if _project(dep) not in pinned]
    if unpinned:
        raise ValueError(
            f"the tool declares {', '.join(unpinned)} but its lock pins no version of it — "
            "regenerate <tool>.lock.txt from the tool's dependencies (`uv pip compile "
            "--generate-hashes --only-binary :all:`)"
        )
    for dep in deps:
        _require_locked_as_declared(dep, locked)
    return {
        "source_sha256": sha256_text(source),
        "lock_sha256": sha256_text(lock) if locked and lock is not None else "",
        "approved_hosts": list(hosts),
        "dependencies": list(deps),
        "locked": list(locked),
    }


def _git_show(repo: Path, branch: str, rel_path: str) -> str | None:
    probe = subprocess.run(
        ["git", "-C", str(repo), "show", f"{branch}:{rel_path}"], capture_output=True, text=True
    )
    return probe.stdout if probe.returncode == 0 else None


def read_candidate(record: ForgedPlugin, branch: str | None) -> tuple[str, str | None]:
    """The source to promote and its lock (None when none landed): a seed's
    from this build, a forged tool's from its landed branch."""
    lock_path = lock_rel_path(record.rel_path)
    if record.provenance == "seed":
        lock_file = seed_tools_root() / lock_path
        lock = lock_file.read_text(encoding="utf-8") if lock_file.is_file() else None
        return seed_tool_source(record.rel_path), lock
    if branch is None:
        raise ValueError(
            f"the authoring run {record.task_id} has not landed — review and approve its "
            "patch first (approve_review) so there is approved source to promote"
        )
    return landed_source(Path(record.repo), branch, record.rel_path), _git_show(
        Path(record.repo), branch, lock_path
    )


def promotion_view(skep_home: Path, record: ForgedPlugin, branch: str | None) -> dict[str, Any]:
    """The promotion card's forge block: the binding plus the full source
    diff against the installed copy (all of it new on a first install)."""
    import difflib

    source, lock = read_candidate(record, branch)
    installed = skep_home / INSTALLED_TOOLS_DIR_NAME / f"{record.plugin_id}.py"
    before = installed.read_text(encoding="utf-8") if installed.is_file() else ""
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        source.splitlines(keepends=True),
        f"installed/{record.plugin_id}.py" if before else "/dev/null",
        f"candidate/{record.rel_path}",
    )
    return {**promotion_binding(source, lock), "diff": "".join(diff)}


def require_card_binding(card: Mapping[str, Any] | None, binding: Mapping[str, Any]) -> None:
    """I6: the confirm installs exactly the triple its card showed."""
    shown = (card or {}).get("forge")
    if not isinstance(shown, Mapping) or "source_sha256" not in shown:
        raise ValueError(
            "promote_tool installs only what its card showed, and this card recorded no "
            "source digest — propose promote_tool again so a fresh card shows the digest, "
            "lock and hosts"
        )
    changed = [key for key in BOUND_KEYS if shown.get(key) != binding[key]]
    if changed:
        raise ValueError(
            f"the tool changed since its card was shown ({', '.join(changed)} differ), so "
            "nothing was installed — propose promote_tool again for a fresh trial and card"
        )


def venv_command(uv: str, staging: Path) -> list[str]:
    return [uv, "venv", "--no-config", "--relocatable", "--python", sys.executable, str(staging)]


def install_command(uv: str, python: Path, lock_file: Path) -> list[str]:
    """The one install a locked tool gets: wheels only (no sdist build code
    ever runs), every file matched to a lock hash, no user/project uv config."""
    return [
        uv, "pip", "install", "--no-config", "--python", str(python), "--no-deps",
        "--require-hashes", "--only-binary", ":all:", "-r", str(lock_file),
    ]  # fmt: skip


def seal_venv(root: Path) -> str:
    """Review R1 (K5, I6, I12): one digest of a venv tree — every path, every
    file's bytes and mode, every link's target. A link may leave the tree
    only as ``bin/python*`` (the base interpreter, the system toolchain);
    any other link out, or a node that is not a file, dir or link, refuses
    (ValueError). Hashed when the install lane finishes and again before
    every reuse and every launch, so a swapped interpreter or a modified
    package never runs under the approved hosts.
    ponytail: re-hashed on every launch; cache by inode+mtime if call volume
    on a large venv ever matters."""
    import stat

    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"{root} is not a venv directory")
    digest = hashlib.sha256()
    resolved_root = root.resolve()
    for current, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted([*dirs, *files]):
            path = Path(current) / name
            rel = path.relative_to(root).as_posix()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(path)
                inside = (path.parent / target).resolve().is_relative_to(resolved_root)
                if not inside and not re.fullmatch(r"bin/python[0-9.]*", rel):
                    raise ValueError(f"{rel} in {root} links out of the venv (to {target})")
                entry = f"l\0{rel}\0{target}"
            elif stat.S_ISDIR(info.st_mode):
                entry = f"d\0{rel}"
            elif stat.S_ISREG(info.st_mode):
                body = hashlib.sha256(path.read_bytes()).hexdigest()
                entry = f"f\0{rel}\0{info.st_mode & 0o111:o}\0{body}"
            else:
                raise ValueError(f"{rel} in {root} is not a file, directory or link")
            digest.update(entry.encode("utf-8", "surrogateescape") + b"\n")
    return digest.hexdigest()


def stage_locked_python(
    skep_home: Path, plugin_id: str, lock: str | None, lock_sha256: str, sealed: str = ""
) -> tuple[str, Path | None, str]:
    """The interpreter both trial stages run, its staging dir and its seal:
    skep's own for a stdlib-only tool; for a locked one its venv — reused
    only when it still matches ``sealed`` (the seal its last install
    recorded), else built in a staging dir (returned, for
    ``commit_locked_venv`` or the caller's cleanup) by the install lane:
    wheels only, the canonical pins only, every file matched to a lock
    hash, egress pinned to PyPI, inside the forged-tool wall."""
    import shutil
    import tempfile

    from . import mcp_client

    if not lock_sha256 or lock is None:
        return sys.executable, None, ""
    final = venv_python(skep_home, plugin_id, lock_sha256)
    if sealed and final.parent.parent.is_dir():
        try:
            if seal_venv(final.parent.parent) == sealed:
                return str(final), None, sealed
        except (OSError, ValueError):
            pass  # not the sealed tree any more: built afresh below
    uv = shutil.which("uv")
    if uv is None:
        raise ValueError("installing a forged tool's locked dependencies needs uv on PATH")
    root = final.parent.parent.parent
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=root))
    try:
        with tempfile.TemporaryDirectory(prefix="skep-forge-install-") as tmp:
            workspace = Path(tmp)
            lock_file = workspace / "lock.txt"
            lock_file.write_text(install_lock(parse_lock(lock)), encoding="utf-8")
            steps = (
                (venv_command(uv, staging), ()),
                (install_command(uv, staging / "bin" / "python", lock_file), PYPI_HOSTS),
            )
            for command, hosts in steps:
                try:
                    proc = mcp_client.forged_lane(
                        command, skep_home=skep_home, hosts=hosts, workspace=workspace,
                        writable=(staging,), timeout=INSTALL_TIMEOUT_SECONDS,
                    )  # fmt: skip
                except mcp_client.MCPError as exc:
                    raise ValueError(str(exc)) from exc
                if proc.returncode != 0:
                    raise ValueError(
                        f"installing the locked dependencies failed (`uv {command[1]}`): "
                        f"{(proc.stderr or proc.stdout).strip()[-600:]} — every package must "
                        "be a wheel (an sdist is never built) whose hash the lock lists, "
                        f"served by {' / '.join(PYPI_HOSTS)}"
                    )
        seal = seal_venv(staging)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return str(staging / "bin" / "python"), staging, seal


def commit_locked_venv(
    skep_home: Path, plugin_id: str, lock_sha256: str, staging: Path | None, seal: str
) -> str:
    """After both stages passed: the staged venv moves to its lock-keyed
    home (``--relocatable``) — replacing a tree there that no longer matches
    — and must still match the seal the trials ran against (I2: what was
    trialed is what runs). Review R2 (L6): the staged tree is checked
    BEFORE anything is replaced, the previous tree is kept until the moved
    one still matches (and put back if it does not), staging never outlives
    a failure, and the committed tree is read-only — the seal ignores write
    bits, so the tree that launches is the tree that was sealed.
    ponytail: superseded lock venvs stay on disk; prune them if disk matters."""
    import shutil
    import tempfile

    if not lock_sha256:
        return sys.executable
    final = venv_python(skep_home, plugin_id, lock_sha256)
    home = final.parent.parent
    if staging is None:  # the sealed tree was reused as it stands
        _require_seal(home, seal)
        return str(final)
    previous: Path | None = None
    try:
        _require_seal(staging, seal)
        if home.exists() or home.is_symlink():
            previous = Path(tempfile.mkdtemp(prefix=".previous-", dir=home.parent)) / "venv"
            _writable(home, True)  # a read-only tree cannot move
            home.rename(previous)
        moved = False
        try:
            staging.rename(home)
            moved = True
            _require_seal(home, seal)
            _writable(home, False)  # R3: inside the rollback — a failure restores the old
        except BaseException:
            if moved:
                _writable(home, True)
                shutil.rmtree(home, ignore_errors=True)
            if previous is not None:
                previous.rename(home)
                with contextlib.suppress(OSError):  # the old tree is back either way
                    _writable(home, False)
            raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if previous is not None:
            if previous.exists():
                _writable(previous, True)
            shutil.rmtree(previous.parent, ignore_errors=True)
    return str(final)


def _require_seal(root: Path, seal: str) -> None:
    try:
        landed = seal_venv(root)
    except (OSError, ValueError) as exc:
        raise ValueError(f"the locked environment at {root} is not whole: {exc}") from exc
    if landed != seal:
        raise ValueError(
            f"the locked environment at {root} changed after its trial (its seal no longer "
            "matches) — nothing was activated; propose promote_tool again"
        )


def _writable(root: Path, writable: bool) -> None:
    """Owner write on (``writable``) or every write bit off, for the whole
    tree. Links are left alone (chmod would follow them out of the tree)."""
    import stat

    if root.is_symlink():  # never followed: chmod would reach out of the tree
        return
    for current, dirs, files in os.walk(root):
        for path in [Path(current), *(Path(current) / name for name in [*dirs, *files])]:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                continue
            mode = stat.S_IMODE(info.st_mode)
            os.chmod(path, mode | stat.S_IWUSR if writable else mode & ~0o222)


def publish_source(
    config: Any, plugin_id: str, source: str, commit: Any, restore: Any = None
) -> Path:
    """Review R1 (K7): swap the installed copy for ``source`` and run
    ``commit`` (the record write that binds it) as one step: the new bytes
    are staged beside the old ones first, and if anything after the swap
    fails the old bytes go back — the previous tool stays byte-identical
    and keeps running. ``restore`` undoes the caller's other writes."""
    import tempfile

    root = installed_tools_root(config)
    root.mkdir(parents=True, exist_ok=True)
    installed = root / f"{plugin_id}.py"
    old = installed.read_bytes() if installed.is_file() else None
    with tempfile.NamedTemporaryFile("wb", dir=root, prefix=f".{plugin_id}.", delete=False) as fh:
        fh.write(source.encode("utf-8"))
    staged = Path(fh.name)
    try:
        os.replace(staged, installed)
        commit()
    except BaseException:
        if old is None:
            installed.unlink(missing_ok=True)
        else:
            with tempfile.NamedTemporaryFile("wb", dir=root, delete=False) as back:
                back.write(old)
            os.replace(back.name, installed)
        if restore is not None:
            restore()
        raise
    finally:
        staged.unlink(missing_ok=True)
    return installed


def wall_trial(
    skep_home: Path, server_id: str, source: str, python: str, hosts: tuple[str, ...]
) -> tuple[bool, str, dict[str, Any]]:
    """Phase 07 (7.2, I2): the trial behind the runtime wall itself
    (``mcp_client.forged_lane``). Stage 1, always: initialize, tools/list
    and the zero-argument ``self_test``, strict reads and deny-all egress.
    Stage 2, only when ``hosts`` is non-empty: the same exchange with egress
    pinned to them through the FilteringProxy, calling ``smoke_test`` when
    the tool exposes one (else ``self_test``). The supervisor reads every
    reply itself; a failure names the stage and its wall."""
    from . import mcp_client

    evidence: dict[str, Any] = {"offline": None, "egress": None}
    stages: list[tuple[str, tuple[str, ...]]] = [("offline", ())]
    if hosts:
        stages.append(("egress", hosts))
    for stage, stage_hosts in stages:
        wall = "deny-all" if not stage_hosts else "egress pinned to " + ", ".join(stage_hosts)
        client = mcp_client.MCPClient(
            mcp_client.MCPServerConfig(server_id, "stdio", command=(python,)),
            runner=mcp_client.forged_runner(source, python, skep_home=skep_home, hosts=stage_hosts),
        )
        try:
            names = [tool.name for tool in client.list_tools()]
        except mcp_client.MCPError as exc:
            return False, f"stage {stage} ({wall}): tools/list failed: {exc}", evidence
        probe = "smoke_test" if stage_hosts and "smoke_test" in names else "self_test"
        if probe not in names:
            return False, f"stage {stage} ({wall}): tools/list has no {probe} tool", evidence
        if not set(names) - {"self_test", "smoke_test"}:  # L10: the probes are not tools
            return (
                False,
                (
                    f"stage {stage} ({wall}): self_test alone (smoke_test included) is not a "
                    "tool server — expose the real tool(s) too"
                ),
                evidence,
            )
        call = client.call_tool(probe, {})
        detail = str(call.content if call.ok else call.error)[:400]
        evidence[stage] = {"tools": names, "called": probe, "ok": call.ok, "result": detail}
        if not call.ok:
            return False, f"stage {stage} ({wall}): {probe} failed: {detail}", evidence
    return True, "trial passed", evidence
