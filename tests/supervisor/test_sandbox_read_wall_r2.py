"""ADR 0068, Phase 03 review round 2: the recorded read wall, held.

G1 dispatch records the RESOLVED targets of ``extra_read_paths`` (validated
first); G10 binds exactly those and refuses one that is now a link or no
longer resolves to itself, so a link retargeted before G10 never widens it.
G2 the implicit skep-home roots (``worker_plugins``, ``profile.json``, the
store, ``tools/.venvs``) are bound only where they sit, never through a link.
G3 a run whose worker had no wall records none as the worker's: G10's own
wall rides ``g10_read_allowlist``. G4 setup judges extra paths against the
daemon's own home (``--home``), not ``SKEP_HOME`` or ``~/.skep``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import RunStore, SupervisorConfig, sandbox

from .conftest import FAKE_WORKER, serve_client

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

# argv[1] must be read; any later path read is a leak (exit non-zero either way).
_MUST_AND_NEVER = """
import sys
open(sys.argv[1]).read()
for path in sys.argv[2:]:
    try:
        open(path).read()
    except OSError:
        continue
    sys.exit("LEAK " + path)
print("wall held")
"""

_TOOLCHAIN_ENV = ("UV_PYTHON_INSTALL_DIR", "UV_TOOL_DIR", "CARGO_HOME", "RUSTUP_HOME", "NVM_DIR",
                  "PYENV_ROOT")  # fmt: skip


@pytest.fixture(autouse=True)
def _clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from skep.supervisor import secret_floor

    monkeypatch.setattr(secret_floor, "HOME_DIRS", (f"{tmp_path}/no-homes/*",))
    for name in (*sandbox._SOCKET_ENV, *_TOOLCHAIN_ENV):
        monkeypatch.delenv(name, raising=False)
    clear = sandbox.availability.cache_clear
    clear()
    yield
    clear()


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "fakehome"
    canary = home / "Downloads" / "canary-downloads.txt"
    canary.parent.mkdir(parents=True)
    canary.write_text("CANARY downloads\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture()
def skep_home(tmp_path: Path) -> Path:
    home = tmp_path / "skep"
    (home / "supervisor").mkdir(parents=True)
    (home / "supervisor" / "serve-token").write_text("CANARY token\n", encoding="utf-8")
    RunStore(home / "supervisor" / "supervisor.sqlite3").close()
    return home


@pytest.fixture()
def strict_config(skep_home: Path) -> SupervisorConfig:
    """A home of its own, so a data dir under tmp_path sits outside it."""
    return SupervisorConfig(
        home=skep_home / "supervisor",
        worker_command=(sys.executable, str(FAKE_WORKER)),
        grace_seconds=0.5,
        heartbeat_seconds=2.0,
        poll_seconds=0.01,
        sandbox=True,
    )


@pytest.fixture()
def data(tmp_path: Path) -> Path:
    """The operator's dataset, reached through a link they named."""
    real = tmp_path / "datasets" / "v1"
    real.mkdir(parents=True)
    (real / "rows.csv").write_text("a,b\n", encoding="utf-8")
    (tmp_path / "current").symlink_to(real)
    return real


def _probe(profile: Path, paths: list[Path]) -> dict[str, str]:
    proc = subprocess.run(
        sandbox.wrap_command([sys.executable, "-c", _PROBE, *map(str, paths)], profile),
        capture_output=True, text=True, check=False, timeout=60,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    seen: dict[str, str] = json.loads(proc.stdout)
    return seen


def _strict_project(config: SupervisorConfig, repo: Path, extra: list[str]) -> None:
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="strict", name="strict", strategy="trusted_local_dev", phase="build",
            policy={"default_execution_mode": "sandbox", "strict_read_confinement": True,
                    "extra_read_paths": extra},
        )  # fmt: skip
        store.add_project_binding(
            project_id="strict", binding_kind="repo_path", binding_value=str(repo)
        )
    finally:
        store.close()


def _dispatch(
    config: SupervisorConfig, repo: Path, monkeypatch: pytest.MonkeyPatch, mode: str = "sandbox"
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """run_task up to the worker (spied): (task id, the dispatched record,
    what the spawner was handed)."""
    from skep.supervisor import dispatch, run_task

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
    task_id = run_task(repo, "x", config=config, execution_mode=mode).record.task_id
    store = RunStore(config.db_path)
    try:
        transitions = store.transitions_for(task_id)
    finally:
        store.close()
    detail = next((d for st, d, _ in transitions if st == "dispatched"), None)
    return task_id, json.loads(detail or "{}"), seen


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
                store=store, task_id=task_id, worker_outcome="passed", repo=repo,
                ref=None, config=replace(config, sandbox=True), verify_command="true",
            )  # fmt: skip
    finally:
        store.close()
    return seen["read_allowlist"]


def _real_g10(config: SupervisorConfig, repo: Path, task_id: str, command: str) -> Any:
    from skep.supervisor.reverify import reverify_run

    patch = config.audit_dir / task_id / "marker.patch"
    patch.parent.mkdir(parents=True, exist_ok=True)
    patch.write_text(
        "diff --git a/patched_marker.txt b/patched_marker.txt\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/patched_marker.txt\n@@ -0,0 +1 @@\n+patched\n",
        encoding="utf-8",
    )
    store = RunStore(config.db_path)
    try:
        store.add_artifact(
            task_id, kind="patch", audit_path=patch,
            sha256=hashlib.sha256(patch.read_bytes()).hexdigest(),
        )  # fmt: skip
        return reverify_run(
            store=store, task_id=task_id, worker_outcome="passed", repo=repo, ref=None,
            config=config, verify_command=command,
        )  # fmt: skip
    finally:
        store.close()


# --- G1: dispatch records resolved targets; G10 binds exactly those ---------


def test_dispatch_records_the_resolved_target_never_the_link(
    repo: Path, strict_config: SupervisorConfig, data: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    _strict_project(strict_config, repo, [str(tmp_path / "current")])
    task_id, record, handed = _dispatch(strict_config, repo, monkeypatch)
    assert record["read_allowlist"] == [str(data.resolve())]
    assert handed["read_allowlist"] == (data.resolve(),)
    # The operator retargets the link after the run ...
    (tmp_path / "current").unlink()
    (tmp_path / "current").symlink_to(tmp_path / "fakehome-elsewhere", target_is_directory=True)
    # ... and G10 is handed the target the worker had, not the link.
    assert _g10_wall(strict_config, repo, task_id, monkeypatch) == (data.resolve(),)


def test_dispatch_refuses_an_extra_path_that_now_reopens_the_wall(
    repo: Path, strict_config: SupervisorConfig, fake_home: Path, data: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """Setup accepted the link; by dispatch it points at the home itself."""
    _strict_project(strict_config, repo, [str(tmp_path / "current")])
    (tmp_path / "current").unlink()
    (tmp_path / "current").symlink_to(fake_home)
    task_id, record, handed = _dispatch(strict_config, repo, monkeypatch)
    assert handed == {}  # no worker
    assert record == {}  # never dispatched
    store = RunStore(strict_config.db_path)
    try:
        run = store.get_run(task_id)
    finally:
        store.close()
    assert run is not None and run.state == "worker_crashed"
    log = (strict_config.audit_dir / task_id / "worker.log").read_text(encoding="utf-8")
    assert log.startswith("sandbox_unavailable: extra_read_paths:")
    assert "at or above the home" in log


def _swap(recorded: Path, swap: str, fake_home: Path) -> None:
    """The recorded target itself, or its parent, becomes a link home."""
    moved = recorded if swap == "target" else recorded.parent
    moved.rename(moved.with_name(moved.name + "-old"))
    moved.symlink_to(fake_home / "Downloads" if swap == "target" else fake_home)


@pytest.mark.parametrize("swap", ["target", "parent"])
def test_g10_refuses_a_recorded_root_that_no_longer_resolves_to_itself(
    swap: str, repo: Path, strict_config: SupervisorConfig, data: Path, fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """Every backend: refused before anything is built, and recorded so."""
    import importlib

    reverify_module = importlib.import_module("skep.supervisor.reverify")
    built: list[Any] = []
    monkeypatch.setattr(reverify_module, "reverify", lambda **kw: built.append(kw))
    monkeypatch.setattr(reverify_module.sandbox, "available", lambda: True)
    recorded = data.resolve()
    store = RunStore(strict_config.db_path)
    try:
        store.transition("t-moved", "dispatched", json.dumps({"read_allowlist": [str(recorded)]}))
        _swap(recorded, swap, fake_home)
        outcome = reverify_module.reverify_run(
            store=store, task_id="t-moved", worker_outcome="passed", repo=repo, ref=None,
            config=strict_config, verify_command="true",
        )  # fmt: skip
        record = store.reverification_for("t-moved")
    finally:
        store.close()
    assert built == []
    assert outcome.outcome == "unavailable" and outcome.exit_codes == []
    assert str(recorded) in outcome.detail and "no longer resolves" in outcome.detail
    assert record is not None and record.outcome == "unavailable" and not record.confirmed


@needs_bwrap
def test_g10_never_reads_through_a_link_retargeted_after_dispatch(
    repo: Path, strict_config: SupervisorConfig, data: Path, fake_home: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """The real wall: the link moves to ~/Downloads between the worker and
    G10, and G10 still reads the dataset and never the downloads."""
    _strict_project(strict_config, repo, [str(tmp_path / "current")])
    task_id, _record, _handed = _dispatch(strict_config, repo, monkeypatch)
    (tmp_path / "current").unlink()
    (tmp_path / "current").symlink_to(fake_home / "Downloads")
    canary = fake_home / "Downloads" / "canary-downloads.txt"
    command = " ".join(
        shlex.quote(str(arg))
        for arg in (sys.executable, "-c", _MUST_AND_NEVER, data / "rows.csv",
                    tmp_path / "current" / canary.name, canary)
    )  # fmt: skip
    outcome = _real_g10(strict_config, repo, task_id, command)
    assert outcome.outcome == "passed", outcome.detail
    assert outcome.exit_codes == [0]


@needs_bwrap
@pytest.mark.parametrize("swap", ["target", "parent"])
def test_g10_refuses_a_recorded_target_swapped_for_a_link_and_says_why(
    swap: str, repo: Path, strict_config: SupervisorConfig, data: Path, fake_home: Path,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    _strict_project(strict_config, repo, [str(tmp_path / "current")])
    task_id, record, _handed = _dispatch(strict_config, repo, monkeypatch)
    recorded = Path(record["read_allowlist"][0])
    assert recorded == data.resolve()  # G1 first: never swap the test's own tmp dir
    _swap(recorded, swap, fake_home)
    canary = fake_home / "Downloads" / "canary-downloads.txt"
    outcome = _real_g10(strict_config, repo, task_id, f"cat {shlex.quote(str(canary))}")
    assert outcome.outcome == "unavailable", outcome.detail
    assert outcome.exit_codes == []  # nothing ran
    assert str(recorded) in outcome.detail and "no longer resolves" in outcome.detail


# --- G2: the implicit skep-home roots are bound where they sit -------------


@pytest.mark.parametrize("entry", ["worker_plugins", "profile.json", "tools"])
def test_a_linked_skep_home_root_is_never_bound(
    entry: str, skep_home: Path, fake_home: Path
) -> None:
    downloads = fake_home / "Downloads"
    if entry == "tools":  # a linked parent: tools/.venvs resolves into the downloads
        (downloads / ".venvs").mkdir()
    (skep_home / entry).symlink_to(downloads)
    roots = sandbox.strict_read_roots(skep_home)
    assert not any(root.is_relative_to(downloads.resolve()) for root in roots), roots


def test_a_skep_home_root_in_place_is_still_bound(skep_home: Path, fake_home: Path) -> None:
    (skep_home / "worker_plugins").mkdir()
    (skep_home / "profile.json").write_text("{}\n", encoding="utf-8")
    (skep_home / "tools" / ".venvs").mkdir(parents=True)
    roots = sandbox.strict_read_roots(skep_home, worker_llm_key=True)
    real = skep_home.resolve()
    for root in (real / "worker_plugins", real / "profile.json", real / "tools" / ".venvs",
                 real / "supervisor" / "supervisor.sqlite3"):  # fmt: skip
        assert root in roots, root


def test_a_relocated_shelf_is_followed_only_by_name(
    skep_home: Path, fake_home: Path, tmp_path: Path
) -> None:
    shelf = tmp_path / "shelf"
    shelf.mkdir()
    (skep_home / "worker_plugins").symlink_to(shelf)
    assert shelf.resolve() not in sandbox.strict_read_roots(skep_home)
    assert shelf.resolve() in sandbox.strict_read_roots(skep_home, extra=(shelf,))


@needs_bwrap
def test_a_plugin_shelf_linked_to_downloads_stays_unreadable_for_real(
    skep_home: Path, fake_home: Path, tmp_path: Path
) -> None:
    (skep_home / "worker_plugins").symlink_to(fake_home / "Downloads")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    profile = sandbox.write_profile(
        tmp_path / "p.profile", workspace=workspace, skep_home=skep_home, read_allowlist=()
    )
    canary = fake_home / "Downloads" / "canary-downloads.txt"
    seen = _probe(profile, [canary, skep_home / "worker_plugins" / canary.name])
    assert "read" not in seen.values(), seen


# --- G3: the worker's wall is recorded only when the worker had one --------


@pytest.mark.parametrize(
    ("mode", "sandboxed", "field"),
    [("sandbox", True, "read_allowlist"), ("workspace", True, "g10_read_allowlist"),
     ("sandbox", False, "g10_read_allowlist")],
)  # fmt: skip
def test_only_a_walled_worker_records_its_read_wall(
    mode: str, sandboxed: bool, field: str, repo: Path, strict_config: SupervisorConfig,
    data: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """A workspace run (or the sandbox off) read the whole disk: its record
    says nothing about a worker wall, and G10 still keeps the project's."""
    _strict_project(strict_config, repo, [str(data)])
    config = replace(strict_config, sandbox=sandboxed)
    task_id, record, _handed = _dispatch(config, repo, monkeypatch, mode=mode)
    other = ({"read_allowlist", "g10_read_allowlist"} - {field}).pop()
    assert record[field] == [str(data.resolve())]
    assert other not in record
    assert _g10_wall(strict_config, repo, task_id, monkeypatch) == (data.resolve(),)


# --- G4: setup judges extra paths against the daemon's own home ------------


@pytest.mark.parametrize("route", ["setup", "preview"])
def test_setup_refuses_a_path_inside_the_daemon_s_own_home(
    route: str, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`skep serve --home X` exports no SKEP_HOME (the suite points it at a
    different temp home): X's own repos must still be refused at setup."""
    import skep.supervisor.serve.registry as registry

    monkeypatch.setattr(
        registry, "new_project_sandbox_probe",
        lambda backend=None: sandbox.SandboxAvailability(True, backend="bubblewrap"),
    )  # fmt: skip
    home = tmp_path / "custom-home"
    config = SupervisorConfig(home=home / "supervisor", worker_command=("w",))
    inside = home / "repos"
    inside.mkdir(parents=True)
    assert Path(os.environ["SKEP_HOME"]).resolve() != home.resolve()
    response = serve_client(config).post(
        f"/api/projects/{route}",
        json={"project_id": "p", "name": "p", "strategy": "trusted_local_dev",
              "repo_path": str(repo),
              "policy_overrides": {"strict_read_confinement": True,
                                   "extra_read_paths": [str(inside)]}},
    )  # fmt: skip
    assert response.status_code == 400, response.text
    assert "skep home" in response.text


def test_the_store_judges_extra_paths_against_its_own_home(tmp_path: Path) -> None:
    home = tmp_path / "custom-home"
    (home / "repos").mkdir(parents=True)
    store = RunStore(home / "supervisor" / "supervisor.sqlite3")
    try:
        with pytest.raises(ValueError, match="skep home"):
            store.add_project_policy(
                project_id="p", name="p", strategy="trusted_local_dev", phase="build",
                policy={"extra_read_paths": [str(home / "repos")]},
            )  # fmt: skip
    finally:
        store.close()


# --- review round 3 (3I): G10 replays the worker's effective wall, or nothing -


def test_dispatch_records_only_the_extra_roots_the_worker_s_wall_binds(
    repo: Path, strict_config: SupervisorConfig, data: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """Sol R3-2: a missing extra path was recorded, though the worker's
    profile skips it; made before G10, G10 would have read it."""
    _strict_project(strict_config, repo, [str(tmp_path / "late"), str(data)])
    _task_id, record, handed = _dispatch(strict_config, repo, monkeypatch)
    assert record["read_allowlist"] == [str(data.resolve())]
    assert handed["read_allowlist"] == (data.resolve(),)


@needs_bwrap
def test_g10_never_reads_an_extra_root_made_after_dispatch(
    repo: Path, strict_config: SupervisorConfig, fake_home: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    late = tmp_path / "late"
    _strict_project(strict_config, repo, [str(late)])
    task_id, _record, _handed = _dispatch(strict_config, repo, monkeypatch)
    late.mkdir()  # after the worker's wall was built without it
    (late / "canary.txt").write_text("CANARY late\n", encoding="utf-8")
    command = " ".join(
        shlex.quote(str(arg))
        for arg in (sys.executable, "-c", _MUST_AND_NEVER, "existing.py", late / "canary.txt")
    )
    outcome = _real_g10(strict_config, repo, task_id, command)
    assert outcome.outcome == "passed", outcome.detail
    assert outcome.exit_codes == [0]


@pytest.mark.parametrize("field", ["read_allowlist", "g10_read_allowlist"])
def test_g10_never_runs_a_recorded_wall_on_the_host_with_the_sandbox_off(
    field: str, repo: Path, strict_config: SupervisorConfig, data: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """Sol R3-3: with the sandbox off, G10 ignored the recorded wall and ran
    the patch's verify on the host."""
    import importlib

    reverify_module = importlib.import_module("skep.supervisor.reverify")
    ran: list[Any] = []
    monkeypatch.setattr(reverify_module, "reverify", lambda **kw: ran.append(kw))
    store = RunStore(strict_config.db_path)
    try:
        store.transition("t-off", "dispatched", json.dumps({field: [str(data.resolve())]}))
        outcome = reverify_module.reverify_run(
            store=store, task_id="t-off", worker_outcome="passed", repo=repo, ref=None,
            config=replace(strict_config, sandbox=False), verify_command="true",
        )  # fmt: skip
        record = store.reverification_for("t-off")
    finally:
        store.close()
    assert ran == []
    assert outcome.outcome == "unavailable" and outcome.exit_codes == []
    assert "strict read wall" in outcome.detail and "not run on the host" in outcome.detail
    assert record is not None and record.outcome == "unavailable" and not record.confirmed
