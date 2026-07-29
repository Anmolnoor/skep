from __future__ import annotations

import json
import shlex
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from skep.worker_contract import EventType
from skep.workers import capabilities as capabilities_mod
from skep.workers.capabilities import (
    CapabilityApprovalRequired,
    CapabilityDenied,
    CapabilityRegistry,
    PluginToolSpec,
)

from .conftest import git


class _OriginHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"ORIGIN-OK"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture()
def origin_url() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _OriginHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://localhost:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


def test_filesystem_write_records_workspace_relative_change(tmp_path: Path) -> None:
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(tmp_path, emit=lambda t, p: events.append((t, p)))

    result = registry.invoke(
        "filesystem.write",
        {"path": "hello.py", "content": 'print("Hello, world!")\n', "overwrite": False},
    )

    assert result.capability_id == "filesystem.write"
    assert result.status == "allowed"
    assert result.changed_files == ("hello.py",)
    assert (tmp_path / "hello.py").read_text(encoding="utf-8") == 'print("Hello, world!")\n'
    assert events == [
        (
            EventType.FILE_CHANGED,
            {"path": "hello.py", "change": "created", "capability_id": "filesystem.write"},
        )
    ]


def test_capability_reason_prefix_and_terms_are_frozen(tmp_path: Path) -> None:
    assert capabilities_mod.CAPABILITY_REASON_PREFIX == "capability."
    registry = CapabilityRegistry(
        tmp_path,
        emit=lambda _t, _p: None,
        shell_allowlist=(("echo",),),
    )

    allowed = registry.invoke("shell.run", {"argv": ["echo", "ok"], "purpose": "modify"})
    assert allowed.status == "allowed"

    with pytest.raises(CapabilityApprovalRequired) as excinfo:
        CapabilityRegistry(tmp_path, emit=lambda _t, _p: None).invoke(
            "shell.run", {"argv": ["echo", "ok"], "purpose": "modify"}
        )
    assert excinfo.value.decision is not None
    reason = excinfo.value.decision.reason
    assert reason.startswith(capabilities_mod.CAPABILITY_REASON_PREFIX)
    term = reason.removeprefix(capabilities_mod.CAPABILITY_REASON_PREFIX).split(".", 1)[0]
    assert term in capabilities_mod.CAPABILITY_REASON_TERMS


def test_filesystem_write_denies_workspace_escape(tmp_path: Path) -> None:
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(tmp_path / "workspace", emit=lambda t, p: events.append((t, p)))
    outside = tmp_path / "outside.txt"

    # v67-F4 (R3): the deny names the acceptable shape, not just the verdict.
    with pytest.raises(CapabilityDenied, match="use a workspace-relative path"):
        registry.invoke("filesystem.write", {"path": str(outside), "content": "pwned"})

    assert not outside.exists()
    assert events == []


def test_filesystem_read_returns_workspace_file_content(repo: Path) -> None:
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(repo, emit=lambda t, p: events.append((t, p)))

    result = registry.invoke("filesystem.read", {"path": "existing.py"})

    assert result.capability_id == "filesystem.read"
    assert result.status == "allowed"
    assert result.output == "value = 0\n"
    assert events[0] == (
        EventType.COMMAND_START,
        {"command": "READ existing.py", "purpose": "read", "capability_id": "filesystem.read"},
    )
    assert events[1][0] == EventType.COMMAND_RESULT
    assert events[1][1]["command"] == "READ existing.py"
    assert events[1][1]["exit_code"] == 0


def test_filesystem_read_repairs_absolute_repo_path_suffix(repo: Path) -> None:
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(repo, emit=lambda t, p: events.append((t, p)))

    result = registry.invoke(
        "filesystem.read",
        {"path": "/data/skep/repos/skep-testing/existing.py"},
    )

    assert result.output == "value = 0\n"
    assert events[0] == (
        EventType.COMMAND_START,
        {"command": "READ existing.py", "purpose": "read", "capability_id": "filesystem.read"},
    )


def test_filesystem_read_chunk_returns_requested_slice(repo: Path) -> None:
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(repo, emit=lambda t, p: events.append((t, p)))

    result = registry.invoke(
        "filesystem.read_chunk",
        {"path": "existing.py", "offset": 6, "max_bytes": 3},
    )

    assert result.capability_id == "filesystem.read_chunk"
    assert result.status == "allowed"
    assert result.output == "= 0"
    assert events[0] == (
        EventType.COMMAND_START,
        {
            "command": "READ_CHUNK existing.py 6 3",
            "purpose": "read",
            "capability_id": "filesystem.read_chunk",
        },
    )


def test_filesystem_edit_replaces_text_and_records_change(repo: Path) -> None:
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(repo, emit=lambda t, p: events.append((t, p)))

    result = registry.invoke(
        "filesystem.edit",
        {"path": "existing.py", "old": "value = 0", "new": "value = 1"},
    )

    assert result.capability_id == "filesystem.edit"
    assert result.changed_files == ("existing.py",)
    assert (repo / "existing.py").read_text(encoding="utf-8") == "value = 1\n"
    assert events[-1] == (
        EventType.FILE_CHANGED,
        {"path": "existing.py", "change": "modified", "capability_id": "filesystem.edit"},
    )


def test_filesystem_apply_diff_applies_patch_and_reports_changed_files(repo: Path) -> None:
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(repo, emit=lambda t, p: events.append((t, p)))
    patch = """diff --git a/existing.py b/existing.py
index e4c2928..3208f24 100644
--- a/existing.py
+++ b/existing.py
@@ -1 +1 @@
-value = 0
+value = 2
"""

    result = registry.invoke("filesystem.apply_diff", {"patch": patch})

    assert result.capability_id == "filesystem.apply_diff"
    assert result.changed_files == ("existing.py",)
    assert (repo / "existing.py").read_text(encoding="utf-8") == "value = 2\n"
    assert events[-1] == (
        EventType.FILE_CHANGED,
        {
            "path": "existing.py",
            "change": "modified",
            "capability_id": "filesystem.apply_diff",
        },
    )


def test_repo_list_files_returns_tracked_paths(repo: Path) -> None:
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(repo, emit=lambda t, p: events.append((t, p)))

    result = registry.invoke("repo.list_files", {"max_files": 10})

    assert result.capability_id == "repo.list_files"
    assert result.status == "allowed"
    assert result.output == "existing.py\n"
    assert events[0] == (
        EventType.COMMAND_START,
        {"command": "LIST_FILES", "purpose": "read", "capability_id": "repo.list_files"},
    )
    assert events[1][0] == EventType.COMMAND_RESULT
    assert events[1][1]["exit_code"] == 0


def test_repo_search_finds_text_in_tracked_files(repo: Path) -> None:
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(repo, emit=lambda t, p: events.append((t, p)))

    result = registry.invoke("repo.search", {"query": "value = 0", "max_matches": 5})

    assert result.capability_id == "repo.search"
    assert result.status == "allowed"
    assert result.output == "existing.py:1:value = 0\n"
    assert events[0] == (
        EventType.COMMAND_START,
        {"command": "SEARCH value = 0", "purpose": "read", "capability_id": "repo.search"},
    )
    assert events[1][0] == EventType.COMMAND_RESULT
    assert events[1][1]["exit_code"] == 0


def test_git_read_capabilities_return_status_show_and_log(repo: Path) -> None:
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(repo, emit=lambda t, p: events.append((t, p)))

    status = registry.invoke("git.status", {})
    show = registry.invoke("git.show", {"rev": "HEAD:existing.py"})
    log = registry.invoke("git.log", {"max_count": 1})

    assert status.capability_id == "git.status"
    assert status.exit_code == 0
    assert show.output == "value = 0\n"
    assert "seed" in (log.output or "")
    command_starts = [
        event[1]["capability_id"] for event in events if event[0] == EventType.COMMAND_START
    ]
    assert command_starts == [
        "git.status",
        "git.show",
        "git.log",
    ]


def test_git_diff_captures_committed_worktree_work(repo: Path) -> None:
    """v20-F2: a worker-side commit must not make work vanish from the patch.

    The registry records the baseline HEAD at construction, so ``git.diff``
    diffs against it and captures committed *and* uncommitted changes.
    """
    registry = CapabilityRegistry(repo, emit=lambda _t, _p: None)

    # Real work committed inside the worktree (the F1-style mid-run commit).
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    git(repo, "add", "calc.py")
    git(repo, "commit", "-qm", "add calculator")
    # An uncommitted edit alongside the commit still belongs in the patch.
    (repo / "README.md").write_text("# Calculator\n")

    diff = registry.invoke("git.diff", {}).output or ""

    assert "calc.py" in diff
    assert "def add" in diff
    assert "README.md" in diff


def test_git_diff_excludes_pycache_noise(repo: Path) -> None:
    """v20-F2: __pycache__/*.pyc junk never appears in a patch."""
    registry = CapabilityRegistry(repo, emit=lambda _t, _p: None)

    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    pycache = repo / "__pycache__"
    pycache.mkdir()
    (pycache / "calc.cpython-312.pyc").write_bytes(b"\x00compiled")

    diff = registry.invoke("git.diff", {}).output or ""

    assert "calc.py" in diff
    assert "__pycache__" not in diff
    assert ".pyc" not in diff


def test_git_unstage_requires_approval(repo: Path) -> None:
    registry = CapabilityRegistry(repo, emit=lambda _t, _p: None)

    with pytest.raises(CapabilityApprovalRequired, match=r"git\.unstage requires approval"):
        registry.invoke("git.unstage", {"paths": ["existing.py"]})


def test_worker_plugin_manifest_loads_declared_tools(tmp_path: Path) -> None:
    from skep.workers import capabilities

    plugin_root = tmp_path / "home" / "worker_plugins"
    plugin_root.mkdir(parents=True)
    (plugin_root / "reader.json").write_text(
        json.dumps(
            {
                "plugin_id": "reader",
                "tools": [
                    {
                        "id": "reader.peek",
                        "description": "Read a file through the reader plugin.",
                        "risk": "read",
                        "command": ["python", "reader.py"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    tools = capabilities.load_plugin_tools(tmp_path / "home")

    assert [tool.tool_id for tool in tools] == ["reader.peek"]
    assert tools[0].command == ("python", str(plugin_root / "reader.py"))
    assert tools[0].to_manifest() == {
        "id": "reader.peek",
        "description": "Read a file through the reader plugin.",
        "risk": "read",
        "source": "plugin:reader",
    }


def test_capability_registry_executes_read_plugin_tool(tmp_path: Path) -> None:
    from skep.workers.capabilities import PluginToolSpec

    plugin = tmp_path / "reader.py"
    plugin.write_text(
        (
            "import json, sys\n"
            "payload = json.load(sys.stdin)\n"
            "print(json.dumps({'output': payload['args']['value'], 'exit_code': 0}))\n"
        ),
        encoding="utf-8",
    )
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(
        tmp_path,
        emit=lambda t, p: events.append((t, p)),
        plugin_tools=(
            PluginToolSpec(
                plugin_id="reader",
                tool_id="reader.echo",
                description="Echo a value.",
                risk="read",
                command=("python", str(plugin)),
            ),
        ),
    )

    result = registry.invoke("reader.echo", {"value": "plugin-ok"})

    assert registry.has_tool("reader.echo")
    assert result.capability_id == "reader.echo"
    assert result.status == "allowed"
    assert result.exit_code == 0
    assert result.output == "plugin-ok"
    assert registry.tool_manifest()[-1] == {
        "id": "reader.echo",
        "description": "Echo a value.",
        "risk": "read",
        "source": "plugin:reader",
    }
    assert events[0] == (
        EventType.COMMAND_START,
        {
            "command": "PLUGIN reader.echo",
            "purpose": "read",
            "capability_id": "reader.echo",
            "decision": {
                "verdict": "allow",
                "reason": "capability.allow.plugin_safe_risk",
                "detail": "read",
            },
        },
    )
    assert events[1][0] == EventType.COMMAND_RESULT
    assert events[1][1]["command"] == "PLUGIN reader.echo"
    assert events[1][1]["exit_code"] == 0


def test_builtin_tool_manifest_hides_git_mutating_tools_from_model(tmp_path: Path) -> None:
    registry = CapabilityRegistry(tmp_path, emit=lambda _t, _p: None)

    manifest_ids = {tool["id"] for tool in registry.tool_manifest()}

    assert registry.has_tool("git.stage")
    assert registry.has_tool("git.commit")
    assert "git.stage" not in manifest_ids
    assert "git.commit" not in manifest_ids
    assert "shell.run" in manifest_ids


def test_read_plugin_cannot_mutate_workspace_even_if_manifest_claims_read(tmp_path: Path) -> None:
    plugin = tmp_path / "reader.py"
    plugin.write_text(
        (
            "import json, pathlib, sys\n"
            "payload = json.load(sys.stdin)\n"
            "path = pathlib.Path(payload['workspace']) / 'note.txt'\n"
            "path.write_text('side effect\\n')\n"
            "print(json.dumps({'output': payload['args']['value'], 'exit_code': 0}))\n"
        ),
        encoding="utf-8",
    )
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(
        tmp_path,
        emit=lambda t, p: events.append((t, p)),
        plugin_tools=(
            PluginToolSpec(
                plugin_id="reader",
                tool_id="reader.echo",
                description="Echo a value.",
                risk="read",
                command=("python", str(plugin)),
            ),
        ),
    )

    with pytest.raises(CapabilityDenied):
        registry.invoke("reader.echo", {"value": "plugin-ok"})

    assert not (tmp_path / "note.txt").exists()


def test_shell_run_records_command_events(tmp_path: Path) -> None:
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(tmp_path, emit=lambda t, p: events.append((t, p)))
    argv = [sys.executable, "-c", "print('ok')"]

    result = registry.invoke("shell.run", {"argv": argv, "purpose": "verify"})

    command = shlex.join(argv)
    assert result.capability_id == "shell.run"
    assert result.status == "allowed"
    assert result.exit_code == 0
    assert result.output == "ok\n"
    assert events[0] == (
        EventType.COMMAND_START,
        {
            "command": command,
            "purpose": "verify",
            "capability_id": "shell.run",
            "decision": {
                "verdict": "allow",
                "reason": "capability.allow.shell_verify",
                "detail": command,
            },
        },
    )
    assert events[1][0] == EventType.COMMAND_RESULT
    assert events[1][1]["command"] == command
    assert events[1][1]["exit_code"] == 0
    assert events[1][1]["stdout_tail"] == "ok\n"


def test_shell_run_accepts_verify_command_string(tmp_path: Path) -> None:
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(tmp_path, emit=lambda t, p: events.append((t, p)))

    result = registry.invoke(
        "shell.run",
        {"command": f"{shlex.quote(sys.executable)} -c \"print('ok')\"", "verify": True},
    )

    command = shlex.join([sys.executable, "-c", "print('ok')"])
    assert result.capability_id == "shell.run"
    assert result.status == "allowed"
    assert result.exit_code == 0
    assert result.output == "ok\n"
    assert events[0] == (
        EventType.COMMAND_START,
        {
            "command": command,
            "purpose": "verify",
            "capability_id": "shell.run",
            "decision": {
                "verdict": "allow",
                "reason": "capability.allow.shell_verify",
                "detail": command,
            },
        },
    )


def test_shell_run_reports_missing_verify_command_without_crashing(tmp_path: Path) -> None:
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(tmp_path, emit=lambda t, p: events.append((t, p)))
    argv = ["definitely-missing-skep-command", "--version"]

    result = registry.invoke("shell.run", {"argv": argv, "purpose": "verify"})

    command = shlex.join(argv)
    assert result.capability_id == "shell.run"
    assert result.status == "allowed"
    assert result.exit_code == 127
    assert result.output == ""
    assert result.error is not None
    assert "definitely-missing-skep-command" in result.error
    assert events[0] == (
        EventType.COMMAND_START,
        {
            "command": command,
            "purpose": "verify",
            "capability_id": "shell.run",
            "decision": {
                "verdict": "allow",
                "reason": "capability.allow.shell_verify",
                "detail": command,
            },
        },
    )
    assert events[1][0] == EventType.COMMAND_RESULT
    assert events[1][1]["command"] == command
    assert events[1][1]["exit_code"] == 127


def test_shell_run_uses_worker_interpreter_for_python_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(tmp_path, emit=lambda t, p: events.append((t, p)))

    result = registry.invoke(
        "shell.run",
        {"argv": ["python", "-c", "print('ok')"], "purpose": "verify"},
    )

    command = shlex.join([sys.executable, "-c", "print('ok')"])
    assert result.capability_id == "shell.run"
    assert result.status == "allowed"
    assert result.exit_code == 0
    assert result.output == "ok\n"
    assert events[0] == (
        EventType.COMMAND_START,
        {
            "command": command,
            "purpose": "verify",
            "capability_id": "shell.run",
            "decision": {
                "verdict": "allow",
                "reason": "capability.allow.shell_verify",
                "detail": command,
            },
        },
    )
    assert events[1][0] == EventType.COMMAND_RESULT
    assert events[1][1]["command"] == command
    assert events[1][1]["exit_code"] == 0


def test_shell_run_rebuilds_child_environment_from_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WORKER_CANARY_SECRET", "leak-me-if-you-can")
    monkeypatch.setenv("ALLOWED_PROVIDER_KEY", "ok-to-pass")
    registry = CapabilityRegistry(
        tmp_path,
        emit=lambda _t, _p: None,
        env_allowlist=("ALLOWED_PROVIDER_KEY",),
    )

    result = registry.invoke(
        "shell.run",
        {
            "argv": [
                sys.executable,
                "-c",
                (
                    "import json, os; "
                    "print(json.dumps({k: v for k, v in os.environ.items() if k in "
                    "{'WORKER_CANARY_SECRET', 'ALLOWED_PROVIDER_KEY', 'PATH', 'HOME'}}))"
                ),
            ],
            "purpose": "verify",
        },
    )

    child_env = json.loads(result.output or "{}")
    assert "WORKER_CANARY_SECRET" not in child_env
    assert child_env.get("ALLOWED_PROVIDER_KEY") == "ok-to-pass"
    assert "PATH" in child_env
    assert "HOME" in child_env


def test_proxy_environment_passes_only_when_network_allowlist_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")

    blocked = CapabilityRegistry(tmp_path, emit=lambda _t, _p: None).invoke(
        "shell.run",
        {
            "argv": [sys.executable, "-c", "import os; print(os.environ.get('HTTP_PROXY', ''))"],
            "purpose": "verify",
        },
    )
    allowed = CapabilityRegistry(
        tmp_path,
        emit=lambda _t, _p: None,
        network_allowlist=("example.com",),
    ).invoke(
        "shell.run",
        {
            "argv": [sys.executable, "-c", "import os; print(os.environ.get('HTTP_PROXY', ''))"],
            "purpose": "verify",
        },
    )

    assert blocked.output == "\n"
    assert allowed.output == "http://127.0.0.1:9\n"


def test_git_read_capabilities_rebuild_child_environment_from_allowlist(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_DIR", "/definitely-missing")
    registry = CapabilityRegistry(
        repo,
        emit=lambda _t, _p: None,
        env_allowlist=(),
    )

    result = registry.invoke("git.status", {})

    assert result.capability_id == "git.status"
    assert result.exit_code == 0
    assert "fatal: not a git repository" not in (result.error or "")


def test_shell_run_requires_approval_for_non_verification_commands(tmp_path: Path) -> None:
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(tmp_path, emit=lambda t, p: events.append((t, p)))

    with pytest.raises(CapabilityApprovalRequired) as excinfo:
        registry.invoke("shell.run", {"argv": ["echo", "ok"], "purpose": "modify"})

    assert excinfo.value.capability_id == "shell.run"
    assert "approval" in excinfo.value.reason
    assert events == [
        (
            EventType.COMMAND_START,
            {
                "command": "echo ok",
                "purpose": "modify",
                "capability_id": "shell.run",
                "decision": {
                    "verdict": "require_approval",
                    "reason": "capability.require_approval.shell_nonverify_not_allowlisted",
                    "detail": "echo ok",
                },
            },
        ),
        (
            EventType.COMMAND_RESULT,
            {
                "command": "echo ok",
                "exit_code": 126,
                "duration_ms": 0,
                "stdout_tail": "",
                "stderr_tail": "shell.run requires approval for command: echo ok",
                "capability_id": "shell.run",
                "decision": {
                    "verdict": "require_approval",
                    "reason": "capability.require_approval.shell_nonverify_not_allowlisted",
                    "detail": "echo ok",
                },
            },
        ),
    ]


def test_shell_run_allows_non_verify_command_with_allowed_prefix(tmp_path: Path) -> None:
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(
        tmp_path,
        emit=lambda t, p: events.append((t, p)),
        shell_allowlist=[["echo"]],
    )

    result = registry.invoke("shell.run", {"argv": ["echo", "ok"], "purpose": "modify"})

    assert result.capability_id == "shell.run"
    assert result.status == "allowed"
    assert result.exit_code == 0
    assert result.output == "ok\n"
    assert events[0] == (
        EventType.COMMAND_START,
        {
            "command": "echo ok",
            "purpose": "modify",
            "capability_id": "shell.run",
            "decision": {
                "verdict": "allow_with_constraints",
                "reason": "capability.allow.shell_allowlist_prefix",
                "detail": "echo ok",
            },
        },
    )
    assert events[1][0] == EventType.COMMAND_RESULT
    assert events[1][1]["decision"] == {
        "verdict": "allow_with_constraints",
        "reason": "capability.allow.shell_allowlist_prefix",
        "detail": "echo ok",
    }


def test_shell_run_marks_resumed_command_as_resume_approved(tmp_path: Path) -> None:
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(
        tmp_path,
        emit=lambda t, p: events.append((t, p)),
        approved_shell_commands=[["echo", "ok"]],
    )

    result = registry.invoke("shell.run", {"argv": ["echo", "ok"], "purpose": "modify"})

    assert result.capability_id == "shell.run"
    assert result.status == "allowed"
    assert result.exit_code == 0
    assert result.output == "ok\n"
    assert events[0] == (
        EventType.COMMAND_START,
        {
            "command": "echo ok",
            "purpose": "modify",
            "capability_id": "shell.run",
            "decision": {
                "verdict": "allow_with_constraints",
                "reason": "capability.allow.resume_approved.shell_command",
                "detail": "echo ok",
            },
        },
    )
    assert events[1][0] == EventType.COMMAND_RESULT
    assert events[1][1]["decision"] == {
        "verdict": "allow_with_constraints",
        "reason": "capability.allow.resume_approved.shell_command",
        "detail": "echo ok",
    }


def test_git_diff_returns_patch_for_untracked_files(repo: Path) -> None:
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(repo, emit=lambda t, p: events.append((t, p)))
    (repo / "new.py").write_text("value = 1\n", encoding="utf-8")

    result = registry.invoke("git.diff", {})

    assert result.capability_id == "git.diff"
    assert result.status == "allowed"
    assert result.output is not None
    assert "diff --git a/new.py b/new.py" in result.output
    assert "+value = 1" in result.output
    assert events == []


def test_git_commit_requires_approval_without_mutating_repo(repo: Path) -> None:
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(repo, emit=lambda t, p: events.append((t, p)))
    head_before = git(repo, "rev-parse", "HEAD").stdout.strip()

    with pytest.raises(CapabilityApprovalRequired) as excinfo:
        registry.invoke("git.commit", {"message": "commit from worker"})

    assert excinfo.value.capability_id == "git.commit"
    assert "approval" in excinfo.value.reason
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == head_before
    assert events == [
        (
            EventType.COMMAND_START,
            {
                "command": "GIT_COMMIT commit from worker",
                "purpose": "git",
                "capability_id": "git.commit",
                "decision": {
                    "verdict": "require_approval",
                    "reason": "capability.require_approval.git_mutation_task_permission_missing",
                    "detail": "git.commit",
                },
            },
        ),
        (
            EventType.COMMAND_RESULT,
            {
                "command": "GIT_COMMIT commit from worker",
                "exit_code": 126,
                "duration_ms": 0,
                "stdout_tail": "",
                "stderr_tail": "git.commit requires approval",
                "capability_id": "git.commit",
                "decision": {
                    "verdict": "require_approval",
                    "reason": "capability.require_approval.git_mutation_task_permission_missing",
                    "detail": "git.commit",
                },
            },
        ),
    ]


def test_instruction_guard_denies_forbidden_git_mutation_even_when_allowed(repo: Path) -> None:
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(
        repo,
        emit=lambda t, p: events.append((t, p)),
        instructions="Create the file, but Do NOT run any git commands.",
        allow_git_mutation=True,
    )
    head_before = git(repo, "rev-parse", "HEAD").stdout.strip()

    with pytest.raises(CapabilityDenied, match="forbidden by task instructions"):
        registry.invoke("git.commit", {"message": "commit from worker"})

    assert git(repo, "rev-parse", "HEAD").stdout.strip() == head_before
    assert events == [
        (
            EventType.COMMAND_START,
            {
                "command": "GIT_COMMIT commit from worker",
                "purpose": "git",
                "capability_id": "git.commit",
                "decision": {
                    "verdict": "deny",
                    "reason": "capability.deny.instruction_guard.git_forbidden",
                    "detail": "git.commit",
                },
            },
        ),
        (
            EventType.COMMAND_RESULT,
            {
                "command": "GIT_COMMIT commit from worker",
                "exit_code": 126,
                "duration_ms": 0,
                "stdout_tail": "",
                "stderr_tail": "git.commit forbidden by task instructions",
                "capability_id": "git.commit",
                "decision": {
                    "verdict": "deny",
                    "reason": "capability.deny.instruction_guard.git_forbidden",
                    "detail": "git.commit",
                },
            },
        ),
    ]


def test_instruction_guard_denies_shell_git_even_for_verification(repo: Path) -> None:
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(
        repo,
        emit=lambda t, p: events.append((t, p)),
        instructions="Inspect the repo, but do not run git commands.",
    )

    with pytest.raises(CapabilityDenied, match="forbidden by task instructions"):
        registry.invoke("shell.run", {"argv": ["git", "status"], "purpose": "verify"})

    assert events == [
        (
            EventType.COMMAND_START,
            {
                "command": "git status",
                "purpose": "verify",
                "capability_id": "shell.run",
                "decision": {
                    "verdict": "deny",
                    "reason": "capability.deny.instruction_guard.git_forbidden",
                    "detail": "git status",
                },
            },
        ),
        (
            EventType.COMMAND_RESULT,
            {
                "command": "git status",
                "exit_code": 126,
                "duration_ms": 0,
                "stdout_tail": "",
                "stderr_tail": "shell.run forbidden by task instructions: git status",
                "capability_id": "shell.run",
                "decision": {
                    "verdict": "deny",
                    "reason": "capability.deny.instruction_guard.git_forbidden",
                    "detail": "git status",
                },
            },
        ),
    ]


def test_git_stage_runs_but_commit_requires_approval_with_git_mutation_grant(
    repo: Path,
) -> None:
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(
        repo,
        emit=lambda t, p: events.append((t, p)),
        allow_git_mutation=True,
    )
    head_before = git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "existing.py").write_text("value = 2\n", encoding="utf-8")

    stage = registry.invoke("git.stage", {"paths": ["existing.py"]})
    with pytest.raises(CapabilityApprovalRequired) as excinfo:
        registry.invoke("git.commit", {"message": "commit from worker"})

    assert stage.capability_id == "git.stage"
    assert stage.exit_code == 0
    assert stage.changed_files == ("existing.py",)
    assert excinfo.value.capability_id == "git.commit"
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == head_before
    command_starts = [
        event[1]["capability_id"] for event in events if event[0] == EventType.COMMAND_START
    ]
    assert command_starts == ["git.stage", "git.commit"]
    git_events = [
        event
        for event in events
        if event[0] == EventType.COMMAND_START
        and event[1]["capability_id"] in {"git.stage", "git.commit"}
    ]
    assert git_events[0][1]["decision"] == {
        "verdict": "allow_with_constraints",
        "reason": "capability.allow.git_mutation_task_permission",
        "detail": "git.stage",
    }
    assert git_events[1][1]["decision"] == {
        "verdict": "require_approval",
        "reason": "capability.require_approval.git_mutation_task_permission_missing",
        "detail": "git.commit",
    }


def test_git_commit_runs_when_resume_approved(repo: Path) -> None:
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(
        repo,
        emit=lambda t, p: events.append((t, p)),
        allow_git_mutation=True,
        approved_capability_ids=("git.commit",),
    )
    head_before = git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "existing.py").write_text("value = 2\n", encoding="utf-8")
    registry.invoke("git.stage", {"paths": ["existing.py"]})

    commit = registry.invoke("git.commit", {"message": "commit from worker"})

    assert commit.capability_id == "git.commit"
    assert commit.exit_code == 0
    assert git(repo, "rev-parse", "HEAD").stdout.strip() != head_before
    assert git(repo, "log", "-1", "--pretty=%s").stdout.strip() == "commit from worker"
    git_events = [
        event
        for event in events
        if event[0] == EventType.COMMAND_START and event[1]["capability_id"] == "git.commit"
    ]
    assert git_events[0][1]["decision"] == {
        "verdict": "allow_with_constraints",
        "reason": "capability.allow.resume_approved.git_mutation",
        "detail": "git.commit",
    }


def test_git_unstage_runs_when_git_mutation_is_explicitly_allowed(repo: Path) -> None:
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(
        repo,
        emit=lambda t, p: events.append((t, p)),
        allow_git_mutation=True,
    )
    (repo / "existing.py").write_text("value = 2\n", encoding="utf-8")

    stage = registry.invoke("git.stage", {"paths": ["existing.py"]})
    unstage = registry.invoke("git.unstage", {"paths": ["existing.py"]})

    assert stage.exit_code == 0
    assert unstage.capability_id == "git.unstage"
    assert unstage.exit_code == 0
    assert git(repo, "diff", "--cached", "--name-only").stdout == ""
    command_starts = [
        event[1]["capability_id"] for event in events if event[0] == EventType.COMMAND_START
    ]
    assert command_starts == ["git.stage", "git.unstage"]


def test_git_restore_requires_approval_without_mutating_workspace(repo: Path) -> None:
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(repo, emit=lambda t, p: events.append((t, p)))
    (repo / "existing.py").write_text("value = 2\n", encoding="utf-8")

    with pytest.raises(CapabilityApprovalRequired) as excinfo:
        registry.invoke("git.restore", {"paths": ["existing.py"]})

    assert excinfo.value.capability_id == "git.restore"
    assert "approval" in excinfo.value.reason
    assert (repo / "existing.py").read_text(encoding="utf-8") == "value = 2\n"
    assert events == [
        (
            EventType.COMMAND_START,
            {
                "command": "GIT_RESTORE existing.py",
                "purpose": "git",
                "capability_id": "git.restore",
                "decision": {
                    "verdict": "require_approval",
                    "reason": "capability.require_approval.git_mutation_task_permission_missing",
                    "detail": "git.restore",
                },
            },
        ),
        (
            EventType.COMMAND_RESULT,
            {
                "command": "GIT_RESTORE existing.py",
                "exit_code": 126,
                "duration_ms": 0,
                "stdout_tail": "",
                "stderr_tail": "git.restore requires approval",
                "capability_id": "git.restore",
                "decision": {
                    "verdict": "require_approval",
                    "reason": "capability.require_approval.git_mutation_task_permission_missing",
                    "detail": "git.restore",
                },
            },
        ),
    ]


def test_git_restore_runs_when_git_mutation_is_explicitly_allowed(repo: Path) -> None:
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(
        repo,
        emit=lambda t, p: events.append((t, p)),
        allow_git_mutation=True,
    )
    (repo / "existing.py").write_text("value = 2\n", encoding="utf-8")

    restored = registry.invoke("git.restore", {"paths": ["existing.py"]})

    assert restored.capability_id == "git.restore"
    assert restored.exit_code == 0
    assert restored.changed_files == ("existing.py",)
    assert (repo / "existing.py").read_text(encoding="utf-8") == "value = 0\n"
    git_events = [
        event
        for event in events
        if event[0] == EventType.COMMAND_START and event[1]["capability_id"] == "git.restore"
    ]
    assert git_events[0][1]["decision"] == {
        "verdict": "allow_with_constraints",
        "reason": "capability.allow.git_mutation_task_permission",
        "detail": "git.restore",
    }


def test_git_restore_emits_file_changed_event_for_restored_workspace_file(repo: Path) -> None:
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(
        repo,
        emit=lambda t, p: events.append((t, p)),
        allow_git_mutation=True,
    )
    (repo / "existing.py").write_text("value = 2\n", encoding="utf-8")

    restored = registry.invoke("git.restore", {"paths": ["existing.py"]})

    assert restored.capability_id == "git.restore"
    assert restored.changed_files == ("existing.py",)
    assert (
        EventType.FILE_CHANGED,
        {
            "path": "existing.py",
            "change": "modified",
            "capability_id": "git.restore",
        },
    ) in events


def test_git_restore_runs_when_resume_approved(repo: Path) -> None:
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(
        repo,
        emit=lambda t, p: events.append((t, p)),
        approved_capability_ids=("git.restore",),
    )
    (repo / "existing.py").write_text("value = 2\n", encoding="utf-8")

    restored = registry.invoke("git.restore", {"paths": ["existing.py"]})

    assert restored.capability_id == "git.restore"
    assert restored.exit_code == 0
    assert restored.changed_files == ("existing.py",)
    assert (repo / "existing.py").read_text(encoding="utf-8") == "value = 0\n"
    git_events = [
        event
        for event in events
        if event[0] == EventType.COMMAND_START and event[1]["capability_id"] == "git.restore"
    ]
    assert git_events[0][1]["decision"] == {
        "verdict": "allow_with_constraints",
        "reason": "capability.allow.resume_approved.git_mutation",
        "detail": "git.restore",
    }


def test_git_stage_requires_approval_without_mutating_index(repo: Path) -> None:
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(repo, emit=lambda t, p: events.append((t, p)))
    (repo / "existing.py").write_text("value = 2\n", encoding="utf-8")

    with pytest.raises(CapabilityApprovalRequired) as excinfo:
        registry.invoke("git.stage", {"paths": ["existing.py"]})

    assert excinfo.value.capability_id == "git.stage"
    assert "approval" in excinfo.value.reason
    assert git(repo, "diff", "--cached", "--name-only").stdout == ""
    assert events == [
        (
            EventType.COMMAND_START,
            {
                "command": "GIT_STAGE existing.py",
                "purpose": "git",
                "capability_id": "git.stage",
                "decision": {
                    "verdict": "require_approval",
                    "reason": "capability.require_approval.git_mutation_task_permission_missing",
                    "detail": "git.stage",
                },
            },
        ),
        (
            EventType.COMMAND_RESULT,
            {
                "command": "GIT_STAGE existing.py",
                "exit_code": 126,
                "duration_ms": 0,
                "stdout_tail": "",
                "stderr_tail": "git.stage requires approval",
                "capability_id": "git.stage",
                "decision": {
                    "verdict": "require_approval",
                    "reason": "capability.require_approval.git_mutation_task_permission_missing",
                    "detail": "git.stage",
                },
            },
        ),
    ]


def test_network_fetch_requires_approval_by_default(tmp_path: Path) -> None:
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(tmp_path, emit=lambda t, p: events.append((t, p)))

    with pytest.raises(CapabilityApprovalRequired) as excinfo:
        registry.invoke("network.fetch", {"url": "https://example.com/"})

    assert excinfo.value.capability_id == "network.fetch"
    assert "approval" in excinfo.value.reason
    assert events == [
        (
            EventType.COMMAND_START,
            {
                "command": "GET https://example.com/",
                "purpose": "network",
                "capability_id": "network.fetch",
                "decision": {
                    "verdict": "require_approval",
                    "reason": "capability.require_approval.network_allowlist_missing",
                    "detail": "example.com",
                },
            },
        ),
        (
            EventType.COMMAND_RESULT,
            {
                "command": "GET https://example.com/",
                "exit_code": 126,
                "duration_ms": 0,
                "stdout_tail": "",
                "stderr_tail": "network.fetch requires approval with a task network allowlist",
                "capability_id": "network.fetch",
                "decision": {
                    "verdict": "require_approval",
                    "reason": "capability.require_approval.network_allowlist_missing",
                    "detail": "example.com",
                },
            },
        ),
    ]


def test_network_fetch_allowed_by_task_network_allowlist(tmp_path: Path, origin_url: str) -> None:
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(
        tmp_path,
        emit=lambda t, p: events.append((t, p)),
        network_allowlist=("localhost",),
    )

    result = registry.invoke("network.fetch", {"url": origin_url})

    assert result.capability_id == "network.fetch"
    assert result.status == "allowed"
    assert result.exit_code == 0
    assert result.output == "ORIGIN-OK"
    assert events[0] == (
        EventType.COMMAND_START,
        {
            "command": f"GET {origin_url}",
            "purpose": "network",
            "capability_id": "network.fetch",
            "decision": {
                "verdict": "allow_with_constraints",
                "reason": "capability.allow.network_allowlist_match",
                "detail": "localhost",
            },
        },
    )
    assert events[1][0] == EventType.COMMAND_RESULT
    assert events[1][1]["command"] == f"GET {origin_url}"
    assert events[1][1]["exit_code"] == 0
    assert events[1][1]["status_code"] == 200
    assert events[1][1]["url"] == origin_url
    assert events[1][1]["host"] == "localhost"
    assert events[1][1]["output_tail"] == "ORIGIN-OK"


def test_network_fetch_denies_hosts_outside_task_network_allowlist(tmp_path: Path) -> None:
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(
        tmp_path,
        emit=lambda t, p: events.append((t, p)),
        network_allowlist=("pypi.org",),
    )

    with pytest.raises(CapabilityDenied):
        registry.invoke("network.fetch", {"url": "https://example.com/"})

    assert events == [
        (
            EventType.COMMAND_START,
            {
                "command": "GET https://example.com/",
                "purpose": "network",
                "capability_id": "network.fetch",
                "decision": {
                    "verdict": "deny",
                    "reason": "capability.deny.network_host_not_allowed",
                    "detail": "example.com",
                },
            },
        ),
        (
            EventType.COMMAND_RESULT,
            {
                "command": "GET https://example.com/",
                "exit_code": 126,
                "duration_ms": 0,
                "stdout_tail": "",
                "stderr_tail": (
                    "host 'example.com' is not in the task network allowlist — "
                    "allowed hosts: pypi.org; use one of those or work offline"
                ),
                "capability_id": "network.fetch",
                "decision": {
                    "verdict": "deny",
                    "reason": "capability.deny.network_host_not_allowed",
                    "detail": "example.com",
                },
            },
        ),
    ]


class _HtmlOriginHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"<html><body><script>x()</script><p>Read me</p></body></html>"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture()
def html_origin_url() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _HtmlOriginHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://localhost:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


def test_network_read_returns_readable_text_on_the_allowlist(
    tmp_path: Path, html_origin_url: str
) -> None:
    """v29-F2: network.read shares network.fetch's gate but strips HTML."""
    registry = CapabilityRegistry(
        tmp_path,
        emit=lambda _t, _p: None,
        network_allowlist=("localhost",),
    )
    result = registry.invoke("network.read", {"url": html_origin_url})
    assert result.capability_id == "network.read"
    assert result.status == "allowed"
    assert result.output == "Read me"  # tags gone, script content dropped
    assert "<p>" not in (result.output or "")
    assert "x()" not in (result.output or "")


def test_network_read_denies_off_allowlist_hosts_like_fetch(tmp_path: Path) -> None:
    registry = CapabilityRegistry(
        tmp_path,
        emit=lambda _t, _p: None,
        network_allowlist=("pypi.org",),
    )
    with pytest.raises(CapabilityDenied):
        registry.invoke("network.read", {"url": "https://example.com/"})


def test_network_read_requires_approval_without_an_allowlist(tmp_path: Path) -> None:
    registry = CapabilityRegistry(tmp_path, emit=lambda _t, _p: None)
    with pytest.raises(CapabilityApprovalRequired) as excinfo:
        registry.invoke("network.read", {"url": "https://example.com/"})
    assert excinfo.value.capability_id == "network.read"


def test_browse_only_run_cannot_reach_shell_or_raw_fetch(tmp_path: Path) -> None:
    """A run scoped to network.read via allowed_tools cannot escalate."""
    from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task
    from skep.workers.coding_minimal import _disallowed_requested_model_tools

    workspace = tmp_path / "ws"
    workspace.mkdir()
    task = mint_task(
        workspace=workspace,
        instructions="read a page",
        budget=DEFAULT_BUDGET,
    ).model_copy(
        update={
            "permissions": _browse_only_permissions(),
        }
    )
    disallowed = _disallowed_requested_model_tools(
        {"network.read", "network.fetch", "shell.run"}, task
    )
    assert "network.fetch" in disallowed
    assert "shell.run" in disallowed
    assert "network.read" not in disallowed


def _browse_only_permissions() -> object:
    from skep.worker_contract import Permissions

    return Permissions(
        read=["workspace"],
        write=["workspace"],
        network=["example.com"],
        env_allowlist=[],
        allowed_tools=["network.read", "filesystem.write"],
    )


def test_network_read_is_a_model_plannable_capability() -> None:
    from skep.workers.capabilities import builtin_tool_manifest

    ids = {tool["id"] for tool in builtin_tool_manifest()}
    assert "network.read" in ids


def test_plugin_tool_allows_mutating_risk_when_explicitly_allowed(tmp_path: Path) -> None:
    events: list[tuple[EventType, dict[str, object]]] = []
    script = tmp_path / "writer.py"
    script.write_text(
        (
            "import json, pathlib, sys\n"
            "payload = json.load(sys.stdin)\n"
            "path = pathlib.Path(payload['workspace']) / payload['args']['path']\n"
            "path.write_text(payload['args']['content'])\n"
            "print(json.dumps({'output': 'ok', 'exit_code': 0, "
            "'changed_files': [payload['args']['path']]}))\n"
        ),
        encoding="utf-8",
    )
    registry = CapabilityRegistry(
        tmp_path,
        emit=lambda t, p: events.append((t, p)),
        plugin_tools=(
            PluginToolSpec(
                plugin_id="writer",
                tool_id="writer.touch",
                description="Write a file via plugin.",
                risk="write",
                command=(sys.executable, str(script)),
            ),
        ),
        allowed_plugin_risks=("write",),
    )

    result = registry.invoke("writer.touch", {"path": "note.txt", "content": "hello\n"})

    assert result.capability_id == "writer.touch"
    assert result.status == "allowed"
    assert result.changed_files == ("note.txt",)
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "hello\n"
    assert events[0] == (
        EventType.COMMAND_START,
        {
            "command": "PLUGIN writer.touch",
            "purpose": "write",
            "capability_id": "writer.touch",
            "decision": {
                "verdict": "allow_with_constraints",
                "reason": "capability.allow.plugin_risk_task_permission",
                "detail": "write",
            },
        },
    )
    assert events[1] == (
        EventType.COMMAND_RESULT,
        {
            "command": "PLUGIN writer.touch",
            "exit_code": 0,
            "duration_ms": events[1][1]["duration_ms"],
            "stdout_tail": "ok",
            "stderr_tail": "",
            "capability_id": "writer.touch",
            "decision": {
                "verdict": "allow_with_constraints",
                "reason": "capability.allow.plugin_risk_task_permission",
                "detail": "write",
            },
        },
    )
    assert isinstance(events[1][1]["duration_ms"], int)
    assert events[1][1]["duration_ms"] >= 0
    assert events[2] == (
        EventType.FILE_CHANGED,
        {
            "path": "note.txt",
            "change": "modified",
            "capability_id": "writer.touch",
        },
    )


def test_plugin_resume_grant_does_not_survive_manifest_risk_widening(
    tmp_path: Path,
) -> None:
    script = tmp_path / "net.py"
    script.write_text(
        ("import json\nprint(json.dumps({'output': 'should-not-run', 'exit_code': 0}))\n"),
        encoding="utf-8",
    )
    registry = CapabilityRegistry(
        tmp_path,
        emit=lambda _t, _p: None,
        plugin_tools=(
            PluginToolSpec(
                plugin_id="writer",
                tool_id="writer.write",
                description="Manifest widened from write to network.",
                risk="network",
                command=(sys.executable, str(script)),
            ),
        ),
        network_allowlist=("example.com",),
        approved_capability_ids=("writer.write",),
    )

    with pytest.raises(CapabilityApprovalRequired) as excinfo:
        registry.invoke("writer.write", {})

    assert excinfo.value.decision is not None
    assert excinfo.value.decision.to_payload() == {
        "verdict": "require_approval",
        "reason": "capability.require_approval.plugin_resume_grant_risk_mismatch",
        "detail": "writer.write",
    }


def test_plugin_resume_grant_runs_when_manifest_risk_matches(tmp_path: Path) -> None:
    script = tmp_path / "writer.py"
    script.write_text(
        (
            "import json, pathlib, sys\n"
            "payload = json.load(sys.stdin)\n"
            "path = pathlib.Path(payload['workspace']) / 'approved.txt'\n"
            "path.write_text('approved\\n')\n"
            "print(json.dumps({'output': 'ok', 'exit_code': 0, "
            "'changed_files': ['approved.txt']}))\n"
        ),
        encoding="utf-8",
    )
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(
        tmp_path,
        emit=lambda t, p: events.append((t, p)),
        plugin_tools=(
            PluginToolSpec(
                plugin_id="writer",
                tool_id="writer.write",
                description="Write through approved plugin.",
                risk="write",
                command=(sys.executable, str(script)),
            ),
        ),
        approved_capability_ids=("writer.write",),
        approved_plugin_risks={"writer.write": "write"},
    )

    result = registry.invoke("writer.write", {})

    assert result.status == "allowed"
    assert (tmp_path / "approved.txt").read_text(encoding="utf-8") == "approved\n"
    assert events[0][1]["decision"] == {
        "verdict": "allow_with_constraints",
        "reason": "capability.allow.resume_approved.plugin_tool",
        "detail": "writer.write",
    }


def test_plugin_tool_rebuilds_child_environment_from_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WORKER_CANARY_SECRET", "leak-me-if-you-can")
    monkeypatch.setenv("ALLOWED_PROVIDER_KEY", "ok-to-pass")
    script = tmp_path / "reader.py"
    script.write_text(
        (
            "import json, os, sys\n"
            "keys = {'WORKER_CANARY_SECRET', 'ALLOWED_PROVIDER_KEY', 'PATH', 'HOME'}\n"
            "print(json.dumps({'output': json.dumps({k: v for k, v in os.environ.items() "
            "if k in keys}), 'exit_code': 0}))\n"
        ),
        encoding="utf-8",
    )
    registry = CapabilityRegistry(
        tmp_path,
        emit=lambda _t, _p: None,
        env_allowlist=("ALLOWED_PROVIDER_KEY",),
        plugin_tools=(
            PluginToolSpec(
                plugin_id="reader",
                tool_id="reader.env",
                description="Read selected environment variables.",
                risk="read",
                command=(sys.executable, str(script)),
            ),
        ),
    )

    result = registry.invoke("reader.env", {})

    child_env = json.loads(result.output or "{}")
    assert "WORKER_CANARY_SECRET" not in child_env
    assert child_env.get("ALLOWED_PROVIDER_KEY") == "ok-to-pass"
    assert "PATH" in child_env
    assert "HOME" in child_env


def test_plugin_network_risk_denied_without_task_network_allowlist(
    tmp_path: Path,
) -> None:
    script = tmp_path / "net.py"
    script.write_text(
        ("import json\nprint(json.dumps({'output': 'should-not-run', 'exit_code': 0}))\n"),
        encoding="utf-8",
    )
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(
        tmp_path,
        emit=lambda t, p: events.append((t, p)),
        plugin_tools=(
            PluginToolSpec(
                plugin_id="net",
                tool_id="net.fetch",
                description="Fetch network data via plugin.",
                risk="network",
                command=(sys.executable, str(script)),
            ),
        ),
        allowed_plugin_risks=("network",),
        network_allowlist=(),
    )

    with pytest.raises(CapabilityDenied, match="requires a task network allowlist"):
        registry.invoke("net.fetch", {"url": "https://example.com/data"})

    assert events == [
        (
            EventType.COMMAND_START,
            {
                "command": "PLUGIN net.fetch",
                "purpose": "network",
                "capability_id": "net.fetch",
                "decision": {
                    "verdict": "deny",
                    "reason": "capability.deny.plugin_network_task_allowlist_missing",
                    "detail": "net.fetch",
                },
            },
        ),
        (
            EventType.COMMAND_RESULT,
            {
                "command": "PLUGIN net.fetch",
                "exit_code": 126,
                "duration_ms": 0,
                "stdout_tail": "",
                "stderr_tail": "net.fetch requires a task network allowlist",
                "capability_id": "net.fetch",
                "decision": {
                    "verdict": "deny",
                    "reason": "capability.deny.plugin_network_task_allowlist_missing",
                    "detail": "net.fetch",
                },
            },
        ),
    ]


def test_plugin_network_risk_uses_proxy_env_when_task_network_allowlist_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9999")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    script = tmp_path / "net.py"
    script.write_text(
        (
            "import json, os\n"
            "keys = ['HTTP_PROXY', 'HTTPS_PROXY']\n"
            "print(json.dumps({'output': json.dumps({k: os.environ.get(k) for k in keys}), "
            "'exit_code': 0}))\n"
        ),
        encoding="utf-8",
    )
    registry = CapabilityRegistry(
        tmp_path,
        emit=lambda _t, _p: None,
        plugin_tools=(
            PluginToolSpec(
                plugin_id="net",
                tool_id="net.fetch",
                description="Fetch network data via plugin.",
                risk="network",
                command=(sys.executable, str(script)),
            ),
        ),
        allowed_plugin_risks=("network",),
        network_allowlist=("example.com",),
    )

    result = registry.invoke("net.fetch", {"url": "https://example.com/data"})

    child_env = json.loads(result.output or "{}")
    assert child_env == {
        "HTTP_PROXY": "http://127.0.0.1:9999",
        "HTTPS_PROXY": "http://127.0.0.1:9999",
    }


def test_plugin_git_risk_requires_git_mutation_permission_even_when_risk_is_allowed(
    tmp_path: Path,
) -> None:
    script = tmp_path / "git_tool.py"
    script.write_text(
        ("import json\nprint(json.dumps({'output': 'should-not-run', 'exit_code': 0}))\n"),
        encoding="utf-8",
    )
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(
        tmp_path,
        emit=lambda t, p: events.append((t, p)),
        plugin_tools=(
            PluginToolSpec(
                plugin_id="gittool",
                tool_id="gittool.commit",
                description="Mutate git state via plugin.",
                risk="git",
                command=(sys.executable, str(script)),
            ),
        ),
        allowed_plugin_risks=("git",),
        allow_git_mutation=False,
    )

    with pytest.raises(CapabilityApprovalRequired) as excinfo:
        registry.invoke("gittool.commit", {"message": "from plugin"})

    assert excinfo.value.capability_id == "gittool.commit"
    assert excinfo.value.decision is not None
    assert excinfo.value.decision.to_payload() == {
        "verdict": "require_approval",
        "reason": "capability.require_approval.plugin_git_task_permission_missing",
        "detail": "gittool.commit",
    }
    assert events == [
        (
            EventType.COMMAND_START,
            {
                "command": "PLUGIN gittool.commit",
                "purpose": "git",
                "capability_id": "gittool.commit",
                "decision": {
                    "verdict": "require_approval",
                    "reason": "capability.require_approval.plugin_git_task_permission_missing",
                    "detail": "gittool.commit",
                },
            },
        ),
        (
            EventType.COMMAND_RESULT,
            {
                "command": "PLUGIN gittool.commit",
                "exit_code": 126,
                "duration_ms": 0,
                "stdout_tail": "",
                "stderr_tail": "gittool.commit requires approval for risk 'git'",
                "capability_id": "gittool.commit",
                "decision": {
                    "verdict": "require_approval",
                    "reason": "capability.require_approval.plugin_git_task_permission_missing",
                    "detail": "gittool.commit",
                },
            },
        ),
    ]


def test_plugin_external_side_effect_risk_requires_manual_approval_even_when_risk_is_allowed(
    tmp_path: Path,
) -> None:
    script = tmp_path / "external.py"
    script.write_text(
        ("import json\nprint(json.dumps({'output': 'should-not-run', 'exit_code': 0}))\n"),
        encoding="utf-8",
    )
    events: list[tuple[EventType, dict[str, object]]] = []
    registry = CapabilityRegistry(
        tmp_path,
        emit=lambda t, p: events.append((t, p)),
        plugin_tools=(
            PluginToolSpec(
                plugin_id="external",
                tool_id="external.deploy",
                description="Perform an external side effect via plugin.",
                risk="external_side_effect",
                command=(sys.executable, str(script)),
            ),
        ),
        allowed_plugin_risks=("external_side_effect",),
    )

    with pytest.raises(CapabilityApprovalRequired) as excinfo:
        registry.invoke("external.deploy", {"target": "service"})

    assert excinfo.value.capability_id == "external.deploy"
    assert excinfo.value.decision is not None
    assert excinfo.value.decision.to_payload() == {
        "verdict": "require_approval",
        "reason": "capability.require_approval.plugin_external_side_effect_not_auto_allowed",
        "detail": "external.deploy",
    }
    assert events == [
        (
            EventType.COMMAND_START,
            {
                "command": "PLUGIN external.deploy",
                "purpose": "external_side_effect",
                "capability_id": "external.deploy",
                "decision": {
                    "verdict": "require_approval",
                    "reason": (
                        "capability.require_approval.plugin_external_side_effect_not_auto_allowed"
                    ),
                    "detail": "external.deploy",
                },
            },
        ),
        (
            EventType.COMMAND_RESULT,
            {
                "command": "PLUGIN external.deploy",
                "exit_code": 126,
                "duration_ms": 0,
                "stdout_tail": "",
                "stderr_tail": "external.deploy requires approval for risk 'external_side_effect'",
                "capability_id": "external.deploy",
                "decision": {
                    "verdict": "require_approval",
                    "reason": (
                        "capability.require_approval.plugin_external_side_effect_not_auto_allowed"
                    ),
                    "detail": "external.deploy",
                },
            },
        ),
    ]


def test_shell_run_strips_proxy_environment_when_task_network_is_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9999")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    registry = CapabilityRegistry(
        tmp_path,
        emit=lambda _t, _p: None,
        env_allowlist=(),
        network_allowlist=(),
    )

    result = registry.invoke(
        "shell.run",
        {
            "argv": [
                sys.executable,
                "-c",
                (
                    "import json, os; "
                    "keys = ['HTTP_PROXY', 'HTTPS_PROXY']; "
                    "print(json.dumps({k: os.environ.get(k) for k in keys}))"
                ),
            ],
            "purpose": "verify",
        },
    )

    child_env = json.loads(result.output or "{}")
    assert child_env == {"HTTP_PROXY": None, "HTTPS_PROXY": None}


def test_shell_run_preserves_proxy_environment_when_task_network_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9999")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    registry = CapabilityRegistry(
        tmp_path,
        emit=lambda _t, _p: None,
        env_allowlist=(),
        network_allowlist=("example.com",),
    )

    result = registry.invoke(
        "shell.run",
        {
            "argv": [
                sys.executable,
                "-c",
                (
                    "import json, os; "
                    "keys = ['HTTP_PROXY', 'HTTPS_PROXY']; "
                    "print(json.dumps({k: os.environ.get(k) for k in keys}))"
                ),
            ],
            "purpose": "verify",
        },
    )

    child_env = json.loads(result.output or "{}")
    assert child_env == {
        "HTTP_PROXY": "http://127.0.0.1:9999",
        "HTTPS_PROXY": "http://127.0.0.1:9999",
    }


def test_plugin_tool_requires_approval_when_mutating_risk_not_allowed(tmp_path: Path) -> None:
    events: list[tuple[EventType, dict[str, object]]] = []
    script = tmp_path / "writer.py"
    script.write_text(
        (
            "import json, pathlib, sys\n"
            "payload = json.load(sys.stdin)\n"
            "path = pathlib.Path(payload['workspace']) / payload['args']['path']\n"
            "path.write_text(payload['args']['content'])\n"
            "print(json.dumps({'output': 'ok', 'exit_code': 0, "
            "'changed_files': [payload['args']['path']]}))\n"
        ),
        encoding="utf-8",
    )
    registry = CapabilityRegistry(
        tmp_path,
        emit=lambda t, p: events.append((t, p)),
        plugin_tools=(
            PluginToolSpec(
                plugin_id="writer",
                tool_id="writer.touch",
                description="Write a file via plugin.",
                risk="write",
                command=(sys.executable, str(script)),
            ),
        ),
    )

    with pytest.raises(CapabilityApprovalRequired) as excinfo:
        registry.invoke("writer.touch", {"path": "note.txt", "content": "hello\n"})

    assert excinfo.value.capability_id == "writer.touch"
    assert excinfo.value.decision is not None
    assert excinfo.value.decision.to_payload() == {
        "verdict": "require_approval",
        "reason": "capability.require_approval.plugin_risk_not_allowed",
        "detail": "write",
    }
    assert not (tmp_path / "note.txt").exists()
    assert events == [
        (
            EventType.COMMAND_START,
            {
                "command": "PLUGIN writer.touch",
                "purpose": "write",
                "capability_id": "writer.touch",
                "decision": {
                    "verdict": "require_approval",
                    "reason": "capability.require_approval.plugin_risk_not_allowed",
                    "detail": "write",
                },
            },
        ),
        (
            EventType.COMMAND_RESULT,
            {
                "command": "PLUGIN writer.touch",
                "exit_code": 126,
                "duration_ms": 0,
                "stdout_tail": "",
                "stderr_tail": "writer.touch requires approval for risk 'write'",
                "capability_id": "writer.touch",
                "decision": {
                    "verdict": "require_approval",
                    "reason": "capability.require_approval.plugin_risk_not_allowed",
                    "detail": "write",
                },
            },
        ),
    ]
