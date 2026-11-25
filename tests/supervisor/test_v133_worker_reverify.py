"""v133 B2: the worker and re-verify fixes from the Alfin live test.

F7 (A2): re-verify primes Node deps in the directory the pin runs in.
F8 (F-I5): a failed install is not a verify that ran; offline failures say so.
F9 (A4): the malformed-reply counter is a streak, not a per-run total.
F10 (A8): a shell.run step with `cwd` (or any key it does not take) is refused.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.contracts_io import DEFAULT_BUDGET
from skep.supervisor.dispatch import run_task
from skep.supervisor.reverify import ReverifyOutcome, reverify

from .conftest import git
from .fake_openai import FakeOpenAI
from .test_coding_minimal_worker import (
    _RECOVERY_DENIED_PLAN,
    _react_done,
    _react_verify_action,
    _react_write_action,
    _recovery_good_plan,
    _repair_test_setup,
    _verify_steps_are_the_pin,  # noqa: F401 -- autouse: verify steps are the pin
)
from .test_reverify import _npm_lock, _RecordingWall

reverify_mod = importlib.import_module("skep.supervisor.reverify")

# ---------- F7 (A2): the pin's own directory is primed ----------


def _pwd_stubs(tmp_path: Path) -> tuple[Path, Path]:
    """Fake `npm`/`pnpm`/`yarn`/`node` logging argv and the directory they ran in."""
    bin_dir = tmp_path / "nodebin"
    bin_dir.mkdir()
    log = tmp_path / "node.log"
    for tool in ("npm", "pnpm", "yarn", "node"):
        stub = bin_dir / tool
        stub.write_text(
            f'#!/bin/sh\necho "{tool} $* pwd=$PWD" >> "$NODE_LOG"\nexit 0\n', encoding="utf-8"
        )
        stub.chmod(0o755)
    return bin_dir, log


def _commit(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", message)


def _web_project(
    repo: Path, lockfile: str = "package-lock.json", *, host: str = "registry.npmjs.org"
) -> None:
    """The Alfin shape: the app lives in web/, the repo root has no package.json."""
    web = repo / "web"
    web.mkdir()
    (web / "package.json").write_text(
        '{"name": "web", "devDependencies": {"jest": "1.0.0"}}\n', encoding="utf-8"
    )
    (web / lockfile).write_text(_npm_lock("1.0.0", host=host) + "\n", encoding="utf-8")
    _commit(repo, "web app")


def _marker_patch(tmp_path: Path, extra: str = "") -> Path:
    patch = tmp_path / "marker.patch"
    patch.write_text(
        "diff --git a/patched_marker.txt b/patched_marker.txt\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/patched_marker.txt\n"
        "@@ -0,0 +1 @@\n"
        "+patched\n" + extra,
        encoding="utf-8",
    )
    return patch


def _web_lock_bump(old_host: str = "registry.npmjs.org") -> str:
    return (
        "diff --git a/web/package-lock.json b/web/package-lock.json\n"
        "--- a/web/package-lock.json\n"
        "+++ b/web/package-lock.json\n"
        "@@ -1 +1 @@\n"
        f"-{_npm_lock('1.0.0', host=old_host)}\n"
        f"+{_npm_lock('2.0.0', host=old_host)}\n"
    )


def _run(
    repo: Path,
    config: SupervisorConfig,
    patch: Path,
    bin_dir: Path,
    log: Path,
    pin: str,
    profile_path: Path | None = None,
) -> ReverifyOutcome:
    return reverify(
        repo=repo,
        ref=None,
        patch_path=patch,
        commands=[pin],
        config=config,
        profile_path=profile_path,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "NODE_LOG": str(log)},
    )


def _log(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


@pytest.mark.parametrize(
    ("pin", "lockfile", "install"),
    [
        ("cd web && npm test", "package-lock.json", "npm ci --ignore-scripts"),
        ("cd web && node --test", "package-lock.json", "npm ci --ignore-scripts"),
        ("CI=1 npm --prefix web test", "package-lock.json", "npm ci --ignore-scripts"),
        ("npm --prefix=web test", "package-lock.json", "npm ci --ignore-scripts"),
        (
            "pnpm -C web test",
            "pnpm-lock.yaml",
            "pnpm install --frozen-lockfile --ignore-scripts --ignore-pnpmfile",
        ),
    ],
)
def test_f7_the_baseline_prime_installs_where_the_pin_runs(
    repo: Path, config: SupervisorConfig, tmp_path: Path, pin: str, lockfile: str, install: str
) -> None:
    """A2: the app in web/, no root package.json — G10 never installed, so the
    worker's pass became DISAGREED with `Cannot find module`."""
    _web_project(repo, lockfile)
    bin_dir, log = _pwd_stubs(tmp_path)
    outcome = _run(repo, config, _marker_patch(tmp_path), bin_dir, log, pin)
    assert outcome.outcome == "passed", outcome.detail
    lines = _log(log)
    assert lines[0].startswith(f"{install} "), lines
    assert lines[0].endswith("/web"), lines
    assert "node deps primed from the baseline" in outcome.detail
    assert len(lines) == 2  # one install, then the pin


def test_f7_a_patch_that_bumps_the_web_lockfile_reprimes_in_web(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    _web_project(repo)
    bin_dir, log = _pwd_stubs(tmp_path)
    patch = _marker_patch(tmp_path, _web_lock_bump())
    outcome = _run(repo, config, patch, bin_dir, log, "cd web && npm test")
    assert outcome.outcome == "passed", outcome.detail
    lines = _log(log)
    assert [line.split()[:2] for line in lines] == [["npm", "ci"], ["npm", "ci"], ["npm", "test"]]
    assert all(line.endswith("/web") for line in lines), lines
    assert "re-primed against the patched lockfile" in outcome.detail


def test_f7_the_prime_reaches_only_the_web_lockfiles_hosts(
    repo: Path, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No new network reach (I12): the hosts come from the lockfile beside
    the manifest that is installed, plus the default registries."""
    _web_project(repo, host="npm.example.com")
    wall = _RecordingWall(monkeypatch)
    bin_dir, log = _pwd_stubs(tmp_path)
    outcome = _run(
        repo,
        config,
        _marker_patch(tmp_path),
        bin_dir,
        log,
        "cd web && npm test",
        profile_path=tmp_path / "reverify.profile.sb",
    )
    assert outcome.outcome == "passed", outcome.detail
    hosts = ("npm.example.com", "registry.npmjs.org", "registry.yarnpkg.com")
    assert wall.proxies and all(proxy == hosts for proxy in wall.proxies[:1]), wall.proxies


@pytest.mark.parametrize(
    "pin", ["cd .. && npm test", "npm --prefix /abs/web test", "cd $APP && npm test"]
)
def test_f7_a_pin_directory_outside_the_worktree_is_not_primed(
    repo: Path, config: SupervisorConfig, tmp_path: Path, pin: str
) -> None:
    """A root project would have been primed and the pin run elsewhere: a
    directory that is not literally inside the worktree primes nothing."""
    (repo / "package.json").write_text(
        '{"name": "x", "devDependencies": {"jest": "1.0.0"}}\n', encoding="utf-8"
    )
    (repo / "package-lock.json").write_text(_npm_lock("1.0.0") + "\n", encoding="utf-8")
    _commit(repo, "root app")
    bin_dir, log = _pwd_stubs(tmp_path)
    outcome = _run(repo, config, _marker_patch(tmp_path), bin_dir, log, pin)
    assert outcome.outcome == "unavailable", outcome.detail
    assert "not a literal directory inside the worktree" in outcome.detail
    assert _log(log) == []  # no install, and the pin was not run


def test_f7_a_symlinked_directory_that_leaves_the_worktree_is_not_primed(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "package.json").write_text(
        '{"name": "o", "devDependencies": {"jest": "1.0.0"}}\n', encoding="utf-8"
    )
    (outside / "package-lock.json").write_text(_npm_lock("1.0.0") + "\n", encoding="utf-8")
    (repo / "web").symlink_to(outside)
    _commit(repo, "web is a symlink out")
    bin_dir, log = _pwd_stubs(tmp_path)
    outcome = _run(repo, config, _marker_patch(tmp_path), bin_dir, log, "cd web && node check.js")
    # A bare node pin runs unprimed, and the record says why.
    assert outcome.outcome == "passed", outcome.detail
    assert "ran unprimed" in outcome.detail
    assert "not a literal directory inside the worktree" in outcome.detail
    assert [line.split()[0] for line in _log(log)] == ["node"]


def test_f7_a_web_lockfile_patch_that_changes_web_package_manager_is_refused(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    """The install refusal reads the manifest that is installed."""
    _web_project(repo)
    bin_dir, log = _pwd_stubs(tmp_path)
    manifest = (
        "diff --git a/web/package.json b/web/package.json\n"
        "--- a/web/package.json\n"
        "+++ b/web/package.json\n"
        "@@ -1 +1 @@\n"
        '-{"name": "web", "devDependencies": {"jest": "1.0.0"}}\n'
        '+{"name": "web", "packageManager": "npm@99.0.0", "devDependencies": {"jest": "2.0.0"}}\n'
    )
    patch = _marker_patch(tmp_path, _web_lock_bump() + manifest)
    outcome = _run(repo, config, patch, bin_dir, log, "cd web && npm test")
    assert outcome.outcome == "unavailable", outcome.detail
    assert "packageManager" in outcome.detail
    assert [line.split()[:2] for line in _log(log)] == [["npm", "ci"]]  # baseline only


def test_f7_the_baseline_clean_keeps_the_primed_web_node_modules(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    git(wt, "init", "-q")
    for path in ("web/node_modules/jest/index.js", "node_modules/a.js", "web/state.txt"):
        (wt / path).parent.mkdir(parents=True, exist_ok=True)
        (wt / path).write_text("x", encoding="utf-8")
    keep = (*reverify_mod._BASELINE_KEEP, "web/node_modules")
    assert reverify_mod._untracked(wt, keep) == {"web/state.txt"}
    assert "web/node_modules/jest/index.js" in reverify_mod._untracked(wt)


# ---------- F8 (F-I5): an install failure is not a verify that ran ----------


def _failing_tool(bin_dir: Path, tool: str, body: str) -> None:
    (bin_dir / tool).write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    (bin_dir / tool).chmod(0o755)


def _assert_install_failure(outcome: ReverifyOutcome, install: str) -> None:
    """F-I5 / F8: the record names the install; no exit code reads as the pin's."""
    assert outcome.outcome == "unavailable", outcome.detail
    assert outcome.exit_codes == []  # the verification command never ran
    first = outcome.detail.splitlines()[0]
    assert first.startswith(reverify_mod.INSTALL_FAILED), first
    assert install in first and "was not run" in first, first


def test_f8_a_failed_baseline_node_install_is_reported_as_the_install(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    _web_project(repo)
    bin_dir, log = _pwd_stubs(tmp_path)
    _failing_tool(bin_dir, "npm", "echo 'npm ERR! code E500' >&2; exit 9")
    outcome = _run(repo, config, _marker_patch(tmp_path), bin_dir, log, "cd web && npm test")
    _assert_install_failure(outcome, "npm ci --ignore-scripts exited 9")
    assert "npm ERR! code E500" in outcome.detail


def test_f8_a_failed_node_reinstall_after_the_patch_is_reported_as_the_install(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    _web_project(repo)
    bin_dir, log = _pwd_stubs(tmp_path)
    _failing_tool(
        bin_dir, "npm", '[ -e ../patched_marker.txt ] && { echo "no tarball" >&2; exit 7; }; exit 0'
    )
    patch = _marker_patch(tmp_path, _web_lock_bump())
    outcome = _run(repo, config, patch, bin_dir, log, "cd web && npm test")
    _assert_install_failure(outcome, "npm ci --ignore-scripts exited 7")


def _uv_repo(repo: Path) -> None:
    (repo / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n", encoding="utf-8")
    (repo / "uv.lock").write_text('version = 1\nrequires-python = ">=3.11"\n', encoding="utf-8")
    _commit(repo, "uv project")


def _uv_lock_bump() -> str:
    return (
        "diff --git a/uv.lock b/uv.lock\n"
        "--- a/uv.lock\n"
        "+++ b/uv.lock\n"
        "@@ -1,2 +1,2 @@\n"
        "-version = 1\n"
        "+version = 2\n"
        ' requires-python = ">=3.11"\n'
    )


@pytest.mark.parametrize("patched_only", [False, True])
def test_f8_a_failed_uv_sync_is_reported_as_the_install(
    repo: Path, config: SupervisorConfig, tmp_path: Path, patched_only: bool
) -> None:
    _uv_repo(repo)
    bin_dir = tmp_path / "uvbin"
    bin_dir.mkdir()
    guard = "[ -e patched_marker.txt ] || exit 0; " if patched_only else ""
    _failing_tool(bin_dir, "uv", f'{guard}[ "$1" = sync ] && exit 3; exit 0')
    outcome = reverify(
        repo=repo,
        ref=None,
        patch_path=_marker_patch(tmp_path, _uv_lock_bump()),
        commands=["uv run pytest"],
        config=config,
        profile_path=None,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
    )
    _assert_install_failure(outcome, "uv sync exited 3")


@pytest.mark.parametrize(
    "lookup_error",
    [
        "npm ERR! request to https://registry.npmjs.org/slugify failed, reason: "
        "getaddrinfo EAI_AGAIN registry.npmjs.org",
        "Error: getaddrinfo ENOTFOUND registry.npmjs.org",
        "pip: Temporary failure in name resolution",
    ],
)
def test_f8_a_verify_that_needs_the_network_says_the_replay_is_offline(
    repo: Path, config: SupervisorConfig, tmp_path: Path, lookup_error: str
) -> None:
    """Alfin 01a211e5: the worker's script installed its own deps; G10's
    replay has no network by design and failed EAI_AGAIN with no word why."""
    bin_dir, log = _pwd_stubs(tmp_path)
    _failing_tool(bin_dir, "node", f"echo '{lookup_error}' >&2; exit 1")
    outcome = _run(
        repo, config, _marker_patch(tmp_path), bin_dir, log, "node scripts/verify-node-farm.cjs"
    )
    assert outcome.outcome == "failed", outcome.detail
    assert outcome.exit_codes == [1]
    first = outcome.detail.splitlines()[0]
    assert reverify_mod.OFFLINE_NOTE in first, first


def test_f8_a_plain_failure_carries_no_offline_note(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    bin_dir, log = _pwd_stubs(tmp_path)
    _failing_tool(bin_dir, "node", "echo 'AssertionError: 1 !== 2' >&2; exit 1")
    outcome = _run(repo, config, _marker_patch(tmp_path), bin_dir, log, "node check.js")
    assert outcome.outcome == "failed"
    assert reverify_mod.OFFLINE_NOTE not in outcome.detail


# ---------- F9 (A4): the malformed-reply counter is a streak ----------

_BAD_REPLY = "I will now write the file, here is my plan: not JSON at all"


def _worker_run(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replies: list[str],
    *,
    protocol: str | None = None,
    max_provider_calls: int | None = None,
) -> tuple[Any, dict[str, Any]]:
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    extra: dict[str, Any] = {}
    if protocol is not None:
        extra["planning_protocol"] = protocol
    if max_provider_calls is not None:
        extra["budget"] = DEFAULT_BUDGET.model_copy(
            update={"max_provider_calls": max_provider_calls}
        )
    try:
        permissions = _repair_test_setup(config, server, monkeypatch)
        for reply in replies:
            server.script_reply(reply)
        outcome = run_task(
            repo, "Create generated.py that prints ok.", config=config, permissions=permissions,
            **extra,
        )  # fmt: skip
    finally:
        server.stop()
    result = json.loads(
        (config.audit_dir / outcome.record.task_id / "result.json").read_text(encoding="utf-8")
    )
    return outcome, result


def test_f9_react_survives_bad_replies_between_good_steps(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alfin 01a211bf-c6ed: the fourth bad reply of a whole run killed it,
    however many good steps came between."""
    replies = [_BAD_REPLY, _BAD_REPLY, _react_write_action()]
    replies += [_BAD_REPLY, _BAD_REPLY, _react_verify_action()]
    replies += [_BAD_REPLY, _BAD_REPLY, _react_done()]
    outcome, result = _worker_run(repo, tmp_path, monkeypatch, replies, protocol="react")
    assert outcome.record.state == "completed", outcome.record.summary
    assert result["usage"]["provider_calls"] == 9


def test_f9_react_still_fails_on_four_bad_replies_in_a_row(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replies = [_react_write_action(), *[_BAD_REPLY] * 4]
    outcome, result = _worker_run(repo, tmp_path, monkeypatch, replies, protocol="react")
    assert outcome.record.state == "failed"
    summary = outcome.record.summary or ""
    assert summary.startswith("react run failed: LlmPlanError: "), summary
    assert result["usage"]["provider_calls"] == 5


def test_f9_react_bad_and_good_replies_still_stop_at_the_call_budget(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The streak resets; the run's provider-call budget still bounds the total."""
    replies = [_BAD_REPLY, _react_write_action()] * 6
    outcome, result = _worker_run(
        repo, tmp_path, monkeypatch, replies, protocol="react", max_provider_calls=5
    )
    assert outcome.record.state == "failed"
    assert result["usage"]["provider_calls"] == 5


def test_f9_the_plan_loop_resets_the_streak_after_a_valid_plan(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replies = [_BAD_REPLY, _BAD_REPLY, _RECOVERY_DENIED_PLAN]
    replies += [_BAD_REPLY, _BAD_REPLY, _recovery_good_plan()]
    outcome, result = _worker_run(repo, tmp_path, monkeypatch, replies)
    assert outcome.record.state == "completed", outcome.record.summary
    assert result["usage"]["provider_calls"] == 6


def test_f9_the_plan_loop_still_fails_on_four_bad_replies_in_a_row(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome, result = _worker_run(repo, tmp_path, monkeypatch, [_BAD_REPLY] * 4)
    assert outcome.record.state == "failed"
    assert result["usage"]["provider_calls"] == 4


# ---------- F10 (A8): shell.run refuses a key it does not take ----------

_CWD_STEP = {"argv": ["npm", "ci"], "cwd": "1-node-farm/final", "purpose": "run"}


def _assert_teaches(message: str) -> None:
    assert "'cwd'" in message, message
    assert "argv, command, purpose, verify, timeout_seconds" in message, message
    assert "npm --prefix" in message and "git -C" in message, message
    assert "repo-relative path" in message, message


def test_f10_a_shell_step_with_cwd_is_refused_naming_the_alternatives() -> None:
    from skep.workers.llm_plan import LlmPlanError, validate_shell_run_arguments

    with pytest.raises(LlmPlanError) as caught:
        validate_shell_run_arguments(_CWD_STEP)
    _assert_teaches(str(caught.value))


@pytest.mark.parametrize(
    "args",
    [
        {"argv": ["npm", "--prefix", "web", "ci"], "purpose": "run"},
        {"argv": ["node", "web/check.js"], "purpose": "verify", "timeout_seconds": 30},
        {"command": "git -C web status", "verify": True},
    ],
)
def test_f10_a_step_without_an_unsupported_key_is_unchanged(args: dict[str, Any]) -> None:
    from skep.workers.llm_plan import validate_shell_run_arguments

    validate_shell_run_arguments(args)


def _cwd_plan() -> dict[str, Any]:
    return {
        "summary": "install in the app folder",
        "required_tools": ["shell.run"],
        "steps": [
            {"tool": "shell.run", "args": _CWD_STEP},
            {"tool": "shell.run", "args": {"argv": ["node", "check.js"], "purpose": "verify"}},
        ],
        "verify": {},
    }


def test_f10_a_stored_plan_with_cwd_is_refused_on_load() -> None:
    """The replay path: a checkpoint written before v133 that carries `cwd`
    fails to load with the same teaching error — it never replays a step
    that would run at the root while the plan said a subfolder."""
    from skep.worker_contract.worker_state import RESUME_CHECKPOINT_STATE_KEY
    from skep.workers.llm_plan import LlmPlanError
    from skep.workers.runtime_plugins import RESUME_CHECKPOINT_PLUGIN

    state = {RESUME_CHECKPOINT_STATE_KEY: {"version": 1, "plan": _cwd_plan()}}
    with pytest.raises(LlmPlanError) as caught:
        RESUME_CHECKPOINT_PLUGIN.checkpoint_from_state(state)
    _assert_teaches(str(caught.value))


def test_f10_the_capability_refuses_cwd_before_anything_runs(tmp_path: Path) -> None:
    """The react path calls the capability directly: the refusal is a
    teaching observation, and the command never starts."""
    from skep.workers.capabilities import CapabilityDenied, CapabilityRegistry

    events: list[tuple[Any, Any]] = []
    registry = CapabilityRegistry(tmp_path, emit=lambda t, p: events.append((t, p)))
    with pytest.raises(CapabilityDenied) as caught:
        registry.invoke("shell.run", {"argv": ["touch", "made.txt"], "cwd": "web"})
    _assert_teaches(str(caught.value))
    assert not (tmp_path / "made.txt").exists()
    assert events == []


def test_f10_a_plan_with_cwd_is_repaired_before_any_step_runs(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    try:
        permissions = _repair_test_setup(config, server, monkeypatch)
        server.script_reply(json.dumps(_cwd_plan()))
        server.script_reply(_recovery_good_plan())
        outcome = run_task(repo, "Create generated.py.", config=config, permissions=permissions)
        bodies = server.chat_bodies()
    finally:
        server.stop()
    assert outcome.record.state == "completed", outcome.record.summary
    repair = json.dumps(bodies[1])
    assert "cwd" in repair and "npm --prefix" in repair
    store = RunStore(config.db_path)
    try:
        commands = [
            e.payload.get("command")
            for e in store.events_for(outcome.record.task_id)
            if e.type.value == "command.start"
        ]
    finally:
        store.close()
    assert not any("npm" in str(command) for command in commands), commands


# ---------- R1 (Sol S2): installer files on the whole path to the install ----


def _apps_web_project(repo: Path) -> None:
    """The install directory two levels down, with yarn config in between."""
    web = repo / "apps" / "web"
    web.mkdir(parents=True)
    (web / "package.json").write_text(
        '{"name": "web", "devDependencies": {"jest": "1.0.0"}}\n', encoding="utf-8"
    )
    (web / "package-lock.json").write_text(_npm_lock("1.0.0") + "\n", encoding="utf-8")
    (repo / "apps" / ".yarnrc.yml").write_text("yarnPath: tools/yarn.cjs\n", encoding="utf-8")
    (repo / "apps" / "tools").mkdir()
    (repo / "apps" / "tools" / "yarn.cjs").write_text("// release\n", encoding="utf-8")
    _commit(repo, "apps/web with yarn config in apps/")


def _apps_web_lock_bump() -> str:
    return _web_lock_bump().replace("web/package-lock.json", "apps/web/package-lock.json")


def _new_file(path: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
        "@@ -0,0 +1 @@\n+// patched installer\n"
    )


@pytest.mark.parametrize(
    "installer",
    [
        # a yarn release directory between the root and the install
        _new_file("apps/.yarn/releases/yarn-9.cjs"),
        _new_file("apps/web/.yarn/releases/yarn-9.cjs"),
        # the yarnPath an intermediate .yarnrc.yml resolves to
        "diff --git a/apps/tools/yarn.cjs b/apps/tools/yarn.cjs\n"
        "--- a/apps/tools/yarn.cjs\n+++ b/apps/tools/yarn.cjs\n"
        "@@ -1 +1 @@\n-// release\n+// patched installer\n",
    ],
    ids=["intermediate-.yarn", "install-dir-.yarn", "intermediate-yarnPath"],
)
def test_r1_s2_a_patched_installer_on_the_path_is_never_installed_with(
    repo: Path, config: SupervisorConfig, tmp_path: Path, installer: str
) -> None:
    _apps_web_project(repo)
    bin_dir, log = _pwd_stubs(tmp_path)
    patch = _marker_patch(tmp_path, _apps_web_lock_bump() + installer)
    outcome = _run(repo, config, patch, bin_dir, log, "cd apps/web && npm test")
    assert outcome.outcome == "unavailable", outcome.detail
    assert "package manager's config" in outcome.detail, outcome.detail
    assert [line.split()[:2] for line in _log(log)] == [["npm", "ci"]]  # baseline only


# ---------- R1 (Sol S10): only the package manager's own directory flags ----


@pytest.mark.parametrize(
    ("pin", "lockfile"),
    [
        ("npm test -- --prefix fixtures", "package-lock.json"),  # the script's argument
        ("npx jest --prefix fixtures", "package-lock.json"),  # jest's, after the command
        ("pnpm test -C fixtures", "pnpm-lock.yaml"),  # pnpm hands it to the script
        ("node -C development test.js", "package-lock.json"),  # node's --conditions
    ],
)
def test_r1_s10_a_script_argument_is_not_the_install_directory(
    repo: Path, config: SupervisorConfig, tmp_path: Path, pin: str, lockfile: str
) -> None:
    (repo / "package.json").write_text(
        '{"name": "x", "devDependencies": {"jest": "1.0.0"}}\n', encoding="utf-8"
    )
    (repo / lockfile).write_text(_npm_lock("1.0.0") + "\n", encoding="utf-8")
    _commit(repo, "root app")
    bin_dir, log = _pwd_stubs(tmp_path)
    npx = bin_dir / "npx"
    npx.write_text((bin_dir / "npm").read_text().replace('"npm ', '"npx '), encoding="utf-8")
    npx.chmod(0o755)
    outcome = _run(repo, config, _marker_patch(tmp_path), bin_dir, log, pin)
    assert outcome.outcome == "passed", outcome.detail
    lines = _log(log)
    assert lines[0].split()[1] in ("ci", "install"), lines  # the package that owns the test
    # installed where the pin runs (the worktree root), not in the argument
    assert lines[0].rsplit("pwd=", 1)[1] == lines[1].rsplit("pwd=", 1)[1], lines
