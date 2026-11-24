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
approval, then promoted through the lifecycle: a sandboxed no-network trial
(``tools/list`` plus a mandatory zero-argument ``self_test`` call) gates
``tested`` — the supervisor parses the trial evidence itself, never the
worker's word (I2); the operator's confirmed card gates ``approved`` (I6);
activation installs the landed source under ``<skep home>/tools/`` and
registers it as an ordinary stdio MCP server.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .store import RunStore

PLUGINS_SETTINGS_KEY = "plugins"
FORGE_DIR_NAME = "forge"
INSTALLED_TOOLS_DIR_NAME = "tools"
TRIAL_MARKER = "FORGE_TRIAL "


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
            )
            plugins[record.plugin_id] = record
    return plugins


def save_plugin(store: RunStore, record: ForgedPlugin) -> None:
    plugins = load_plugins(store)
    plugins[record.plugin_id] = record
    store.set_setting(PLUGINS_SETTINGS_KEY, json.dumps([asdict(p) for p in plugins.values()]))


def authoring_instructions(name: str, purpose: str, rel_path: str) -> str:
    """The coding worker's brief. Every line is enforced by the trial harness."""
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
# It is also the fixture the forge's own tests hold the trial harness against.
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


def install_source(config: Any, plugin_id: str, source: str) -> Path:
    root = installed_tools_root(config)
    root.mkdir(parents=True, exist_ok=True)
    installed = root / f"{plugin_id}.py"
    installed.write_text(source, encoding="utf-8")
    return installed


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
        if not locked.is_file():
            raise ValueError(
                f"the locked environment of {plugin!r} ({locked.parent.parent}) is missing — "
                f"promote_tool {plugin} rebuilds it from its approved lock"
            )
        python = str(locked)
    return ForgedLaunch(raw.decode("utf-8"), python, record.approved_hosts)


# The sandboxed trial harness: write the candidate into the workspace, then
# hold it to the contract. chr(10) instead of a newline escape keeps this
# template free of backslashes that would need double-escaping.
_TRIAL_HARNESS = """import base64, json, os, subprocess, sys

SOURCE = base64.b64decode("__SOURCE_B64__").decode("utf-8")
TOOL = os.path.join(os.getcwd(), "forge_trial_candidate.py")
with open(TOOL, "w") as handle:
    handle.write(SOURCE)


PYTHON = __PYTHON__ or sys.executable


def exchange(method, params):
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {}}},
        {"jsonrpc": "2.0", "id": 2, "method": method, "params": params},
    ]
    payload = chr(10).join(json.dumps(request) for request in requests) + chr(10)
    try:
        proc = subprocess.run(
            [PYTHON, TOOL], input=payload, capture_output=True, text=True, timeout=60
        )
    except OSError as exc:  # the interpreter itself would not start
        proc = subprocess.CompletedProcess([PYTHON], 127, "", PYTHON + ": " + str(exc))
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and message.get("id") == 2:
            return message, proc
    return None, proc


evidence = {"ok": False, "tools": [], "self_test": None, "error": None}
reply, proc = exchange("tools/list", {})
if reply is None:
    evidence["error"] = "no JSON-RPC reply to tools/list; stderr tail: " + proc.stderr[-400:]
elif "error" in reply:
    evidence["error"] = "tools/list returned an error: " + json.dumps(reply["error"])[:400]
else:
    tools = (reply.get("result") or {}).get("tools") or []
    names = [str(tool.get("name")) for tool in tools if isinstance(tool, dict)]
    evidence["tools"] = names
    if "self_test" not in names:
        evidence["error"] = (
            "the contract requires a zero-argument self_test tool; tools/list has none"
        )
    elif len(names) < 2:
        evidence["error"] = "self_test alone is not a tool server — expose the real tool(s) too"
    else:
        reply, proc = exchange("tools/call", {"name": "self_test", "arguments": {}})
        result = None if reply is None else reply.get("result")
        if not isinstance(result, dict):
            evidence["error"] = (
                "no usable reply to the self_test call; stderr tail: " + proc.stderr[-400:]
            )
        elif result.get("isError"):
            evidence["error"] = "self_test FAILED: " + str(result.get("content"))[:400]
        else:
            evidence["self_test"] = str(result.get("content"))[:400]
            evidence["ok"] = True
print("FORGE_TRIAL " + json.dumps(evidence), flush=True)
"""


def trial_script(source: str, python: str | None = None) -> str:
    """The harness for ``source``, run by ``python`` (a locked tool's staged
    venv; None = the script worker's own interpreter, a stdlib-only tool)."""
    encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
    return _TRIAL_HARNESS.replace("__SOURCE_B64__", encoded).replace("__PYTHON__", repr(python))


def trial_verdict(run_result: Mapping[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    """Supervisor-side reading of the trial run's output.

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
    r"(?P<pin>[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9._,-]+\])?==[A-Za-z0-9.+!_-]+)"
    r"(?:\s*;\s*(?P<marker>[^-\\]+?))?"
    r"(?P<hashes>(?:\s+--hash=sha256:[0-9a-f]{64})+)"
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
        if match is None:
            raise ValueError(
                f"lock line {line[:120]!r} is not a pinned, hash-locked requirement — the lock "
                "may hold only `name==version --hash=sha256:…` lines (regenerate it with "
                "`uv pip compile --generate-hashes --only-binary :all:`)"
            )
        hashes = " ".join(h.removeprefix("--hash=") for h in match.group("hashes").split())
        marker = f" ; {match.group('marker').strip()}" if match.group("marker") else ""
        pins.append(f"{match.group('pin')}{marker} {hashes}")
    if not pins:
        raise ValueError("the lock lists no requirements — regenerate it from the tool's deps")
    return tuple(pins)


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
        uv, "pip", "install", "--no-config", "--python", str(python),
        "--require-hashes", "--only-binary", ":all:", "-r", str(lock_file),
    ]  # fmt: skip


def stage_locked_python(
    skep_home: Path, plugin_id: str, lock: str | None, lock_sha256: str
) -> tuple[str, Path | None]:
    """The interpreter both trial stages run: skep's own for a stdlib-only
    tool; for a locked one its venv — already built for this exact lock, or
    built now in a staging dir (returned, for ``commit_locked_venv`` or the
    caller's cleanup) by the install lane: wheels only, every file matched
    to a lock hash, egress pinned to PyPI, inside the forged-tool wall."""
    import shutil
    import tempfile

    from . import mcp_client

    if not lock_sha256 or lock is None:
        return sys.executable, None
    final = venv_python(skep_home, plugin_id, lock_sha256)
    if final.is_file():
        return str(final), None
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
            lock_file.write_text(lock, encoding="utf-8")
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
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return str(staging / "bin" / "python"), staging


def commit_locked_venv(
    skep_home: Path, plugin_id: str, lock_sha256: str, staging: Path | None
) -> str:
    """After both stages passed: the staged venv moves to its lock-keyed
    home (``--relocatable``), and that interpreter is what runs.
    ponytail: superseded lock venvs stay on disk; prune them if disk matters."""
    import shutil

    if not lock_sha256:
        return sys.executable
    final = venv_python(skep_home, plugin_id, lock_sha256)
    if staging is not None:
        try:
            staging.rename(final.parent.parent)
        except OSError:  # a twin promote landed this exact lock first
            shutil.rmtree(staging, ignore_errors=True)
    if not final.is_file():
        raise ValueError(f"the locked environment did not land at {final.parent.parent}")
    return str(final)


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
        call = client.call_tool(probe, {})
        detail = str(call.content if call.ok else call.error)[:400]
        evidence[stage] = {"tools": names, "called": probe, "ok": call.ok, "result": detail}
        if not call.ok:
            return False, f"stage {stage} ({wall}): {probe} failed: {detail}", evidence
    return True, "trial passed", evidence
