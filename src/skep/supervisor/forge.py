"""v71-F1: the forge — skep authors its own MCP tools.

The three subsystems this joins already existed; none was connected:

- worker-authored code landing as a patch via human approval (I1),
- the v17 plugin lifecycle state machine (``plugin_lifecycle.py`` — a
  complete governed ladder that, until now, had no consumer),
- the MCP registry (``mcp_client.py``) — activation IS registration, so a
  forged tool's calls decide under the one policy engine (I5), and
  suspension IS deregistration: registered ⟺ active, one authorization
  surface, no shadow state.

A forged tool is ONE stdlib-only Python file in the operator's forge repo
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
import json
import re
import subprocess
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
            )
            plugins[record.plugin_id] = record
    return plugins


def save_plugin(store: RunStore, record: ForgedPlugin) -> None:
    plugins = load_plugins(store)
    plugins[record.plugin_id] = record
    store.set_setting(PLUGINS_SETTINGS_KEY, json.dumps([asdict(p) for p in plugins.values()]))


def authoring_instructions(name: str, purpose: str, rel_path: str) -> str:
    """The coding worker's brief. Every line is enforced by the trial harness."""
    return f"""Create the file {rel_path} — a single-file stdio MCP tool server named {name!r}.

Purpose: {purpose}

The contract (the supervisor's sandboxed trial enforces every line):
- Python 3, STANDARD LIBRARY ONLY. No pip installs, no third-party imports.
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


def landed_source(repo: Path, branch: str, rel_path: str) -> str:
    """The tool source EXACTLY as the approved landing left it (git show)."""
    probe = subprocess.run(
        ["git", "-C", str(repo), "show", f"{branch}:{rel_path}"],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        raise ValueError(
            f"{rel_path} is not on the landed branch {branch!r} of {repo} — the "
            "authoring run must create exactly that file; re-forge if it wrote "
            f"elsewhere ({probe.stderr.strip() or 'git show failed'})"
        )
    return probe.stdout


def install_source(config: Any, plugin_id: str, source: str) -> Path:
    root = installed_tools_root(config)
    root.mkdir(parents=True, exist_ok=True)
    installed = root / f"{plugin_id}.py"
    installed.write_text(source, encoding="utf-8")
    return installed


# The sandboxed trial harness: write the candidate into the workspace, then
# hold it to the contract. chr(10) instead of a newline escape keeps this
# template free of backslashes that would need double-escaping.
_TRIAL_HARNESS = """import base64, json, os, subprocess, sys

SOURCE = base64.b64decode("__SOURCE_B64__").decode("utf-8")
TOOL = os.path.join(os.getcwd(), "forge_trial_candidate.py")
with open(TOOL, "w") as handle:
    handle.write(SOURCE)


def exchange(method, params):
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {}}},
        {"jsonrpc": "2.0", "id": 2, "method": method, "params": params},
    ]
    payload = chr(10).join(json.dumps(request) for request in requests) + chr(10)
    proc = subprocess.run(
        [sys.executable, TOOL], input=payload, capture_output=True, text=True, timeout=60
    )
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


def trial_script(source: str) -> str:
    encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
    return _TRIAL_HARNESS.replace("__SOURCE_B64__", encoded)


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
