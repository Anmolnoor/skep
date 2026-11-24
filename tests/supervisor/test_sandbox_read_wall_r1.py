"""ADR 0068, Phase 03 review round 1: the read wall's holes, closed.

W1/W10 the wall comes from the project the run already resolved and is
recorded on its ``dispatched`` line; G10 (live and after a restart) reads that
record, never the policy again. W2/W11/W13 roots are validated before any walk
or bind (a detected toolchain linked elsewhere, an extra path at/above the
home, in the skep home, on /etc's reserved entries). W3 a moved $CARGO_HOME's
credentials are masked. W4 the git common dir loses its symlinks and the main
checkout's private state. W5 a podman host's strict run takes native bwrap.
W6 the doctor reports effect, not capability. W7 a bare name is refused before
any cwd resolution. W8 the operator's worker script is bound back. W12 a
socket root under /tmp no longer crashes bwrap.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import RunStore, SupervisorConfig, sandbox
from skep.supervisor.worktree import (
    git_metadata_masked,
    git_metadata_readable_roots,
    git_metadata_writable_roots,
)

from .conftest import git

needs_bwrap = pytest.mark.skipif(
    not sys.platform.startswith("linux") or sandbox.availability().backend != "bubblewrap",
    reason="real bubblewrap proof (runs where bwrap is the backend)",
)

# {path: "read" | the error's name}; a directory counts as read when listable.
_PROBE = """
import json, os, sys
seen = {}
for path in sys.argv[1:]:
    try:
        os.listdir(path) if os.path.isdir(path) else open(path, 'rb').read()
        seen[path] = "read"
    except OSError as exc:
        seen[path] = type(exc).__name__
print(json.dumps(seen))
"""

_TOOLCHAIN_ENV = ("UV_PYTHON_INSTALL_DIR", "UV_TOOL_DIR", "CARGO_HOME", "RUSTUP_HOME", "NVM_DIR",
                  "PYENV_ROOT")  # fmt: skip


@pytest.fixture(autouse=True)
def _clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from skep.supervisor import secret_floor

    monkeypatch.setattr(secret_floor, "HOME_DIRS", (f"{tmp_path}/no-homes/*",))
    for name in (*sandbox._SOCKET_ENV, *_TOOLCHAIN_ENV):
        monkeypatch.delenv(name, raising=False)
    clear = sandbox.availability.cache_clear  # a test may patch availability itself
    clear()
    yield
    clear()


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "fakehome"
    for rel in ("Downloads/canary-downloads.txt", ".ssh/id_rsa"):
        path = home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"CANARY {rel}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture()
def skep_home(tmp_path: Path) -> Path:
    home = tmp_path / "skep"
    (home / "supervisor").mkdir(parents=True)
    (home / "supervisor" / "serve-token").write_text("CANARY token\n", encoding="utf-8")
    RunStore(home / "supervisor" / "supervisor.sqlite3").close()
    return home


def _usable(monkeypatch: pytest.MonkeyPatch, backend: str) -> None:
    probe = sandbox.SandboxAvailability(True, backend=backend)
    monkeypatch.setattr(sandbox, "require_usable", lambda backend=None: probe)


def _profile_argv(path: Path) -> list[str]:
    argv: list[str] = json.loads(path.read_text(encoding="utf-8").partition("\n")[2])
    return argv


def _probe(profile: Path, paths: list[Path], cwd: Path | None = None) -> dict[str, str]:
    proc = subprocess.run(
        sandbox.wrap_command([sys.executable, "-c", _PROBE, *map(str, paths)], profile),
        capture_output=True, text=True, check=False, timeout=60, cwd=cwd,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    seen: dict[str, str] = json.loads(proc.stdout)
    return seen


# --- W1/W10: the wall is the resolved project's, recorded, and G10 keeps it --


def _template_project(config: SupervisorConfig, policy: dict[str, Any]) -> Any:
    from skep.worker_contract.task import ProjectContextPayload

    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="tpl", name="tpl", strategy="trusted_local_dev", phase="build",
            policy={"default_execution_mode": "sandbox", **policy},
        )  # fmt: skip
        store.add_project_binding(
            project_id="tpl", binding_kind="template_name", binding_value="nightly"
        )
    finally:
        store.close()
    return ProjectContextPayload(
        project_id="tpl", name="tpl", strategy="trusted_local_dev", phase="build",
        binding_kind="template_name", binding_value="nightly",
    )  # fmt: skip


class _Stop(Exception):
    pass


def _g10_wall(config: SupervisorConfig, repo: Path, task_id: str, monkeypatch: Any) -> Any:
    import importlib

    reverify_module = importlib.import_module("skep.supervisor.reverify")
    seen: dict[str, Any] = {}

    def spy(**kwargs: Any) -> Any:
        seen.update(kwargs)
        raise _Stop

    monkeypatch.setattr(reverify_module, "reverify", spy)
    monkeypatch.setattr(reverify_module.sandbox, "available", lambda: True)
    store = RunStore(config.db_path)
    try:
        with pytest.raises(_Stop):
            reverify_module.reverify_run(
                store=store, task_id=task_id, worker_outcome="completed", repo=repo,
                ref=None, config=replace(config, sandbox=True), verify_command="true",
            )  # fmt: skip
    finally:
        store.close()
    return seen["read_allowlist"]


@pytest.mark.parametrize(
    ("policy", "wall"),
    [
        # an existing dir: a missing one is never in the wall (3I)
        ({"strict_read_confinement": True, "extra_read_paths": ["/usr/share"]},
         (Path("/usr/share").resolve(),)),
        ({"strict_read_confinement": False}, None),
    ],
)  # fmt: skip
def test_a_template_bound_project_s_wall_reaches_the_worker_the_record_and_g10(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch,
    policy: dict[str, Any], wall: Any,
) -> None:  # fmt: skip
    """Bound by template name only (no repo_path): the repo lookup found no
    project and the run read the whole disk while the project said strict."""
    from skep.supervisor import dispatch, run_task

    context = _template_project(config, policy)
    seen: dict[str, Any] = {}

    def spy(*_args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise OSError("spy: no worker")

    monkeypatch.setattr(dispatch, "spawn_worker", spy)
    monkeypatch.setattr(
        dispatch,
        "sandbox_availability",
        lambda backend=None: sandbox.SandboxAvailability(True, backend="bubblewrap"),
    )
    outcome = run_task(
        repo, "x", config=replace(config, sandbox=True), execution_mode="sandbox",
        project_context=context,
    )  # fmt: skip
    assert seen["read_allowlist"] == wall
    task_id = outcome.record.task_id
    store = RunStore(config.db_path)
    try:
        detail = next(d for st, d, _ in store.transitions_for(task_id) if st == "dispatched")
        recorded = json.loads(detail or "{}").get("read_allowlist")
        assert recorded == (None if wall is None else [str(path) for path in wall])
        # The operator widens the project between the run and its G10 ...
        store.add_project_policy(
            project_id="tpl", name="tpl", strategy="trusted_local_dev", phase="build",
            policy={"default_execution_mode": "sandbox", "strict_read_confinement": False},
        )  # fmt: skip
    finally:
        store.close()
    # ... and G10 still re-runs the patch inside the wall its worker had.
    assert _g10_wall(config, repo, task_id, monkeypatch) == wall


def test_the_restart_g10_reads_the_recorded_wall_too(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task
    from skep.supervisor.dispatch import _reverify_after_restart

    reverify_module = importlib.import_module("skep.supervisor.reverify")
    task = mint_task(workspace=repo, instructions="x", budget=DEFAULT_BUDGET)
    store = RunStore(config.db_path)
    try:
        store.create_run(task, repo=repo, ref=None, execution_mode="sandbox")
        store.transition(task.task_id, "dispatched", json.dumps({"read_allowlist": []}))
        record = store.get_run(task.task_id)
        assert record is not None
        seen: dict[str, Any] = {}

        def spy(**kwargs: Any) -> Any:
            seen.update(kwargs)
            raise _Stop

        monkeypatch.setattr(reverify_module, "reverify", spy)
        monkeypatch.setattr(reverify_module.sandbox, "available", lambda: True)
        with pytest.raises(_Stop):
            _reverify_after_restart(
                store, replace(config, sandbox=True), record, task, "completed", None
            )
    finally:
        store.close()
    assert seen["read_allowlist"] == ()  # strict, though no project policy says so


# --- W2/W11/W13: roots validated before any walk or bind ---------------------


def test_a_detected_toolchain_linked_elsewhere_is_never_bound(
    fake_home: Path, skep_home: Path
) -> None:
    (fake_home / ".local").mkdir()
    (fake_home / ".local" / "bin").symlink_to(fake_home / "Downloads")
    (fake_home / ".cargo").mkdir()
    roots = sandbox.strict_read_roots(skep_home)
    assert (fake_home / "Downloads").resolve() not in roots
    assert (fake_home / ".cargo").resolve() in roots  # a real one still is
    # the operator can still follow such a link on purpose
    on_purpose = sandbox.strict_read_roots(skep_home, extra=(fake_home / ".local" / "bin",))
    assert (fake_home / "Downloads").resolve() in on_purpose


@needs_bwrap
def test_a_symlinked_local_bin_does_not_open_downloads_for_real(
    fake_home: Path, skep_home: Path, tmp_path: Path
) -> None:
    (fake_home / ".local").mkdir()
    (fake_home / ".local" / "bin").symlink_to(fake_home / "Downloads")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    profile = sandbox.write_profile(
        tmp_path / "p.profile", workspace=workspace, skep_home=skep_home, read_allowlist=()
    )
    canary = fake_home / "Downloads" / "canary-downloads.txt"
    seen = _probe(profile, [canary, fake_home / ".local" / "bin" / canary.name])
    assert "read" not in seen.values(), seen


@pytest.mark.parametrize(
    "where",
    ["home", "home-parent", "skep-home", "skep-home-child", "etc", "etc-ssh-link", "dotdot-root"],
)
def test_an_extra_read_path_over_the_home_the_skep_home_or_etc_refuses_the_run(
    where: str, fake_home: Path, skep_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused at run time, BEFORE the socket walk (W13: a link to / used
    to walk the whole host first)."""
    link = tmp_path / "link-to-etc-ssh"
    link.symlink_to("/etc/ssh")
    extra = {
        "home": fake_home,
        "home-parent": fake_home.parent,
        "skep-home": skep_home,
        "skep-home-child": skep_home / "supervisor",
        "etc": Path("/etc"),
        "etc-ssh-link": link,
        "dotdot-root": Path("/tmp/.."),
    }[where]
    walked: list[Any] = []

    def walk(roots: Any) -> list[Path]:
        walked.append(roots)
        return []

    monkeypatch.setattr(sandbox, "_sockets_under", walk)
    _usable(monkeypatch, "bubblewrap")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(sandbox.SandboxUnavailableError, match="extra_read_paths"):
        sandbox.write_profile(
            tmp_path / "p.profile", workspace=workspace, skep_home=skep_home,
            read_allowlist=(extra,),
        )  # fmt: skip
    assert walked == []
    assert not (tmp_path / "p.profile").exists()


def test_detected_roots_on_etc_s_reserved_entries_are_dropped(
    fake_home: Path, skep_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NVM_DIR", "/etc")
    if os.path.isdir("/etc/ssh"):
        monkeypatch.setenv("PYENV_ROOT", "/etc/ssh")
    roots = sandbox.strict_read_roots(skep_home)
    assert Path("/etc") not in roots and Path("/etc/ssh") not in roots


@pytest.mark.parametrize(
    "value", [["/a/.."], ["//"], ["/etc"], ["/etc/ssh/sshd_config"], ["HOME"], ["HOME/.."]]
)
def test_setup_refuses_an_extra_read_path_that_reopens_the_wall(
    value: list[str], fake_home: Path, skep_home: Path
) -> None:
    from skep.supervisor.projects import validate_project_policy

    paths = [path.replace("HOME", str(fake_home)) for path in value]
    with pytest.raises(ValueError, match="extra_read_paths") as err:
        validate_project_policy({"extra_read_paths": paths}, skep_home=skep_home)
    if value[0].startswith("HOME"):
        assert "subdirectory" in str(err.value)
    ok = {"extra_read_paths": [str(fake_home / "datasets")]}
    assert validate_project_policy(ok, skep_home=skep_home)


@needs_bwrap
def test_a_legal_extra_path_under_etc_keeps_its_reserved_entries_out(
    fake_home: Path, skep_home: Path, tmp_path: Path
) -> None:
    keep = Path("/etc/passwd")  # world-readable, and not a reserved entry
    if not keep.is_file():
        pytest.skip("no /etc/passwd")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    profile = sandbox.write_profile(
        tmp_path / "p.profile", workspace=workspace, skep_home=skep_home, read_allowlist=(keep,)
    )
    reserved = [Path("/etc", n) for n in ("ssh", "shadow", "gshadow", "sudoers", "sudoers.d")]
    seen = _probe(profile, [keep, *(p for p in reserved if os.path.lexists(p))])
    assert seen.pop(str(keep)) == "read"
    assert "read" not in seen.values(), seen


# --- W3: a moved $CARGO_HOME keeps its credentials masked --------------------


def test_a_moved_cargo_home_s_credentials_are_masked(
    fake_home: Path, skep_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cargo = tmp_path / "cargo-elsewhere"
    (cargo / "bin").mkdir(parents=True)
    for name in ("credentials", "credentials.toml"):
        (cargo / name).write_text("CANARY token\n", encoding="utf-8")
    monkeypatch.setenv("CARGO_HOME", str(cargo))
    masked = sandbox.secret_paths(skep_home)
    assert (cargo / "credentials").resolve() in masked
    assert (cargo / "credentials.toml").resolve() in masked
    assert (cargo / "credentials").resolve() in sandbox.secret_paths(skep_home, worker_llm_key=True)


@needs_bwrap
def test_a_moved_cargo_home_s_credentials_stay_unreadable_for_real(
    fake_home: Path, skep_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cargo = tmp_path / "cargo-elsewhere"
    (cargo / "bin").mkdir(parents=True)
    for name in ("credentials", "credentials.toml"):
        (cargo / name).write_text("CANARY token\n", encoding="utf-8")
    monkeypatch.setenv("CARGO_HOME", str(cargo))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    profile = sandbox.write_profile(
        tmp_path / "p.profile", workspace=workspace, skep_home=skep_home, read_allowlist=()
    )
    seen = _probe(profile, [cargo / "bin", cargo / "credentials", cargo / "credentials.toml"])
    assert seen.pop(str(cargo / "bin")) == "read"
    assert "read" not in seen.values(), seen


# --- W4: the common dir without symlinks or the main checkout's private state -


def _repo_with_private_state(base: Path) -> tuple[Path, Path, Path]:
    repo = base / "r"
    repo.mkdir()
    git(repo, "init", "-q")
    for key, value in (("user.email", "t@t"), ("user.name", "t"), ("gc.auto", "0")):
        git(repo, "config", key, value)
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-qm", "a")
    (repo / "a.txt").write_text("stashed secret\n", encoding="utf-8")
    git(repo, "stash", "-q")
    (repo / "b.txt").write_text("staged secret\n", encoding="utf-8")
    git(repo, "add", "b.txt")
    common = repo / ".git"
    (common / "MERGE_MSG").write_text("CANARY\n", encoding="utf-8")
    # R4: ORIG_HEAD is visible (unmasked); a real ref keeps in-wall fsck clean
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    (common / "ORIG_HEAD").write_text(head + "\n", encoding="utf-8")
    private = base / "private"
    private.mkdir()
    (private / "canary.txt").write_text("CANARY private\n", encoding="utf-8")
    (common / "leak").symlink_to(private)
    worktree = base / "w"
    git(repo, "worktree", "add", "-q", "--detach", str(worktree))
    return repo, common.resolve(), worktree


def test_the_common_dir_is_bound_whole_and_the_main_checkout_s_private_state_masked(
    tmp_path: Path,
) -> None:
    _repo, common, worktree = _repo_with_private_state(tmp_path)
    # 3I: the dir itself, so a rename git makes in it (index, packed-refs,
    # HEAD) stays live; a symlinked entry ("leak") resolves inside the wall
    assert git_metadata_readable_roots(worktree) == (common,)
    # R4 (Neon): ORIG_HEAD is not masked (a /dev/null there breaks git fsck)
    assert git_metadata_masked(worktree) == tuple(
        common / name for name in ("COMMIT_EDITMSG", "MERGE_MSG")
    )


@needs_bwrap
def test_git_status_and_diff_still_work_inside_the_narrowed_metadata(
    skep_home: Path, tmp_path: Path
) -> None:
    _repo, common, worktree = _repo_with_private_state(tmp_path)
    (worktree / "a.txt").write_text("changed\n", encoding="utf-8")
    profile = sandbox.write_profile(
        tmp_path / "p.profile",
        workspace=worktree,
        extra_writable=git_metadata_writable_roots(worktree),
        extra_readable=git_metadata_readable_roots(worktree),
        skep_home=skep_home,
        read_allowlist=(),
    )
    proc = subprocess.run(
        sandbox.wrap_command(
            ["/bin/sh", "-c",
             "git status --short && git diff --stat && git log --oneline -1"
             " && git fsck --no-progress --no-dangling"],
            profile,
        ),
        cwd=worktree, capture_output=True, text=True, check=False, timeout=60,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "TMPDIR": str(worktree)},
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    assert "a.txt" in proc.stdout and "1 file changed" in proc.stdout
    private = [common / "MERGE_MSG", common / "COMMIT_EDITMSG",
               common / "leak" / "canary.txt", tmp_path / "private" / "canary.txt"]  # fmt: skip
    seen = _probe(profile, private, cwd=worktree)
    assert "read" not in seen.values(), seen


# --- W5: a podman host's strict run falls back to native bwrap ---------------


def test_a_strict_run_on_a_podman_host_takes_the_native_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor.spawner import resolve_sandbox_backend

    monkeypatch.setattr(
        sandbox,
        "availability",
        lambda backend=None: sandbox.SandboxAvailability(True, backend=backend),
    )
    config = SupervisorConfig(home=tmp_path / "h", worker_command=("w",), sandbox_backend="podman")
    assert resolve_sandbox_backend(config, sandbox.DENY_ALL_NETWORK) == "podman"
    assert resolve_sandbox_backend(config, sandbox.DENY_ALL_NETWORK, strict_reads=True) is None


def test_the_spawner_asks_for_the_native_backend_on_a_strict_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import spawner
    from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task

    seen: dict[str, object] = {}
    _usable(monkeypatch, "bubblewrap")
    monkeypatch.setattr(sandbox, "write_profile", lambda path, **kw: seen.update(kw) or path)
    monkeypatch.setattr(sandbox, "wrap_command", lambda argv, profile, backend=None: argv)
    monkeypatch.setattr(spawner, "git_metadata_writable_roots", lambda _ws: ())
    monkeypatch.setattr(spawner, "git_metadata_readable_roots", lambda _ws: ())
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: type("P", (), {"pid": 1})())
    monkeypatch.setattr(
        sandbox,
        "availability",
        lambda backend=None: sandbox.SandboxAvailability(True, backend=backend),
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config = SupervisorConfig(
        home=tmp_path / "skep" / "supervisor", worker_command=("w",), sandbox_backend="podman"
    )
    task = mint_task(workspace=workspace, instructions="x", budget=DEFAULT_BUDGET)
    spawner.spawn_worker(
        config, task, tmp_path / "t.json", tmp_path / "o.json", log_path=tmp_path / "w.log",
        read_allowlist=(),
    )  # fmt: skip
    assert seen["backend"] is None  # native: the wall podman cannot raise


# --- W6: the doctor reports what the wall covers, not what it could ---------


def test_the_doctor_counts_strict_projects_and_names_the_denylist_lanes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep import status

    monkeypatch.setattr(
        status,
        "sandbox_availability",
        lambda: sandbox.SandboxAvailability(True, backend="bubblewrap"),
    )
    (tmp_path / "supervisor").mkdir()
    store = RunStore(tmp_path / "supervisor" / "supervisor.sqlite3")
    try:
        for project_id, policy in (
            ("old", {"default_execution_mode": "sandbox"}),
            ("new", {"default_execution_mode": "sandbox", "strict_read_confinement": True}),
            ("loose", {"default_execution_mode": "workspace"}),
        ):
            store.add_project_policy(
                project_id=project_id, name=project_id, strategy="trusted_local_dev",
                phase="build", policy=policy,
            )  # fmt: skip
    finally:
        store.close()
    check = status._sandbox_check(tmp_path)
    assert "strict-allowlist available" in check["detail"]
    assert "1 of 2 sandboxed projects strict" in check["detail"]
    for lane in ("forged tools", "script schedules", "MCP servers", "fast lane", "diagnose"):
        assert lane in check["detail"], lane
    assert check["strict_projects"] == 1 and check["sandboxed_projects"] == 2


# --- W7: a bare name is refused before anything resolves it against the cwd --


def test_a_bare_name_is_refused_before_the_cwd_resolves_it(
    config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor.serve.fileio import queen_filesystem_decision
    from skep.supervisor.serve.settings import ConfigHolder

    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "loop").symlink_to("loop")  # resolving it raised RuntimeError (a 500)
    monkeypatch.chdir(cwd)
    store = RunStore(config.db_path)
    try:
        decision = queen_filesystem_decision(
            store, ConfigHolder(config, store), action="read", path="loop"
        )
        # an absolute loop is refused as unresolvable, no longer a crash
        absolute = queen_filesystem_decision(
            store, ConfigHolder(config, store), action="read", path=str(cwd / "loop")
        )
    finally:
        store.close()
    assert decision.reason == "filesystem.deny.outside_root_lookup"
    assert absolute.reason == "filesystem.deny.unresolvable_path"


# --- W8: the operator's worker command is readable inside the wall ----------


@needs_bwrap
def test_a_strict_run_reads_its_operator_chosen_worker_script(
    fake_home: Path, skep_home: Path, tmp_path: Path
) -> None:
    """The orchestrator's direct proof died "can't open file …fake_worker.py"."""
    from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task
    from skep.supervisor.spawner import spawn_worker

    elsewhere = tmp_path / "unrelated"
    elsewhere.mkdir()
    (elsewhere / "sibling-canary.txt").write_text("CANARY sibling\n", encoding="utf-8")
    script = elsewhere / "fake_worker.py"
    script.write_text(
        "import os, sys\n"
        "here = os.path.dirname(os.path.abspath(__file__))\n"
        "try:\n    open(os.path.join(here, 'sibling-canary.txt')).read(); print('LEAK')\n"
        "except OSError as exc:\n    print('sibling', type(exc).__name__)\n"
        "print('worker ran')\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config = SupervisorConfig(
        home=skep_home / "supervisor", worker_command=(sys.executable, str(script)), sandbox=True
    )
    task = mint_task(workspace=workspace, instructions="x", budget=DEFAULT_BUDGET)
    log = tmp_path / "audit" / "worker.log"
    (tmp_path / "out").mkdir()  # dispatch makes the results dir
    process = spawn_worker(
        config, task, tmp_path / "t.json", tmp_path / "out" / "o.json", log_path=log,
        read_allowlist=(),
    )  # fmt: skip
    assert process.wait(timeout=60) == 0, log.read_text(encoding="utf-8")
    text = log.read_text(encoding="utf-8")
    assert "worker ran" in text and "LEAK" not in text
    assert "sibling FileNotFoundError" in text


# --- W12: a socket root under /tmp (git's fsmonitor) no longer crashes bwrap --


@needs_bwrap
@pytest.mark.parametrize("strict", [True, False])
def test_a_repo_under_tmp_with_an_fsmonitor_socket_still_runs(
    strict: bool, skep_home: Path
) -> None:
    base = Path(tempfile.mkdtemp(prefix="skr-", dir="/tmp"))  # the host /tmp, on purpose
    try:
        repo = base / "r"
        repo.mkdir()
        git(repo, "init", "-q")
        git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty",
            "-m", "a")  # fmt: skip
        worktree = base / "w"
        git(repo, "worktree", "add", "-q", "--detach", str(worktree))
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(str(repo / ".git" / "fsm.ipc"))
        sock.close()
        profile = sandbox.write_profile(
            base / "p.profile",
            workspace=worktree,
            extra_writable=git_metadata_writable_roots(worktree),
            extra_readable=git_metadata_readable_roots(worktree),
            skep_home=skep_home,
            read_allowlist=() if strict else None,
        )
        proc = subprocess.run(
            sandbox.wrap_command(["git", "status", "--short"], profile),
            cwd=worktree, capture_output=True, text=True, check=False, timeout=60,
            env={"PATH": os.environ["PATH"], "HOME": str(base)},
        )  # fmt: skip
        assert proc.returncode == 0, proc.stderr
    finally:
        shutil.rmtree(base, ignore_errors=True)
