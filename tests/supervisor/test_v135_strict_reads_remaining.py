"""v135 Wave 1 (ADR 0068 follow-up, ADR 0069 §3): the last denylist-only lanes.

Before: the ``run_code`` fast lane and ``diagnose_run`` wrote denylist-only
profiles, so either could read ``~/Downloads``, ``~/Documents``, ``~/.ssh``
(past the secret floor's list) and every sibling checkout. Now the fast lane
reads the strict allowlist wherever the backend enforces one, and
``diagnose_run`` takes the wall the run's dispatch recorded — the one G10
already takes, through the same reader, with the same refusals (I2, I12).

The real-wall tests run where bubblewrap or Seatbelt is the backend (Linux
CI, the macOS job); every canary sits under a FAKE home in ``tmp_path``.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import sysconfig
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import skep
from skep.supervisor import RunStore, SupervisorConfig, sandbox
from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task
from skep.supervisor.serve.actions import diagnose_run
from skep.supervisor.serve.fastlane import run_code_fast

from .conftest import git

needs_strict_wall = pytest.mark.skipif(
    sandbox.availability().backend not in sandbox.STRICT_READ_BACKENDS,
    reason="real sandbox proof (runs where bwrap or sandbox-exec is the backend)",
)

# {path: "read" | the error's name}; a directory counts as read when listed.
_PROBE = """
import json, os, sys
seen = {}
for path in sys.argv[1:]:
    try:
        os.listdir(path) if os.path.isdir(path) else open(path).read()
        seen[path] = "read"
    except OSError as exc:
        seen[path] = type(exc).__name__
print(json.dumps(seen))
"""
_TINY_TEST = "def test_tiny():\n    assert 1 + 1 == 2\n"


@pytest.fixture(autouse=True)
def clear_sandbox_probe_cache() -> Iterator[None]:
    sandbox.availability.cache_clear()
    yield
    sandbox.availability.cache_clear()


@pytest.fixture(autouse=True)
def no_other_homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from skep.supervisor import secret_floor

    monkeypatch.setattr(secret_floor, "HOME_DIRS", (f"{tmp_path}/no-homes/*",))
    for name in (
        *sandbox._SOCKET_ENV,
        "UV_PYTHON_INSTALL_DIR",
        "UV_TOOL_DIR",
        "CARGO_HOME",
        "RUSTUP_HOME",
        "NVM_DIR",
        "PYENV_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "fakehome"
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture()
def canaries(fake_home: Path) -> list[Path]:
    """The plan's four: Downloads, Documents, .ssh and a sibling repo."""
    made = []
    for rel in (
        "Downloads/canary-downloads.txt",
        "Documents/canary-documents.txt",
        ".ssh/id_ed25519",
        "code/sibling-repo/canary-sibling.txt",
    ):
        path = fake_home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"CANARY {rel}\n", encoding="utf-8")
        made.append(path)
    git(fake_home / "code" / "sibling-repo", "init", "-q")
    return made


def _reads(seen: dict[str, str]) -> list[str]:
    return [path for path, verdict in seen.items() if verdict == "read"]


def _ordinary_canaries_open_without_the_wall(canaries: list[Path], tmp_path: Path) -> None:
    """S5, the control: under a denylist-only profile (``read_allowlist=None``)
    the ordinary-file canaries ARE readable where they sit, so an unread one
    under the strict wall is the wall's doing — not bwrap's tmpfs over /tmp
    or the secret floor (which masks ``.ssh`` either way). Skips, saying why,
    where the location is hidden anyway."""
    ordinary = [str(path) for path in canaries if ".ssh" not in path.parts]
    work = tmp_path / "control"
    work.mkdir()
    profile = sandbox.write_profile(
        work / "sandbox.profile",
        workspace=work,
        network=sandbox.DENY_ALL_NETWORK,
        skep_home=tmp_path / "skep",
        read_allowlist=None,
    )
    argv = sandbox.wrap_command([sys.executable, "-c", _PROBE, *ordinary], profile)
    proc = subprocess.run(argv, cwd=work, capture_output=True, text=True, check=True)
    if _reads(json.loads(proc.stdout)) != ordinary:
        pytest.skip(f"canaries under {tmp_path} are hidden even without the strict wall")


# --- the fast lane -----------------------------------------------------------


def _probe_script(paths: list[Path]) -> str:
    return f"import sys\nsys.argv[1:] = {[str(p) for p in paths]!r}\n{_PROBE}"


@needs_strict_wall
def test_the_fast_lane_reads_no_home_canary(canaries: list[Path], tmp_path: Path) -> None:
    _ordinary_canaries_open_without_the_wall(canaries, tmp_path)
    result = run_code_fast("python", _probe_script(canaries), skep_home=tmp_path / "skep")
    assert result is not None and result["exit_code"] == 0, result
    seen = json.loads(result["output"])
    assert len(seen) == 4 and _reads(seen) == [], seen


@needs_strict_wall
def test_the_fast_lane_still_runs_python_and_pytest(fake_home: Path, tmp_path: Path) -> None:
    code = (
        "import pathlib, subprocess, sys\n"
        f"pathlib.Path('test_tiny.py').write_text({_TINY_TEST!r})\n"
        "proc = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', "
        "'no:cacheprovider', 'test_tiny.py'], capture_output=True, text=True)\n"
        "print(proc.stdout, proc.stderr)\n"
        "raise SystemExit(proc.returncode)\n"
    )
    result = run_code_fast("python", code, skep_home=tmp_path / "skep")
    assert result is not None and result["exit_code"] == 0, result
    assert "1 passed" in result["output"]


@needs_strict_wall
def test_the_fast_lane_starts_an_interpreter_that_lives_in_a_venv_under_home(
    canaries: list[Path], fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `uv tool install` or pipx skep: its python is a venv under $HOME.
    The wall binds that venv (never the home) and the script still runs. The
    venv sits where no other rule allowlists it (S7), so only venv detection
    can bind it. (Reached through a linked parent dir, bwrap cannot start it:
    ADR 0068 states that limit.)"""
    _ordinary_canaries_open_without_the_wall(canaries, tmp_path)
    venv = fake_home / "venvs" / "skep"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    # skep importable from it, as installed (the wall's Landlock step runs
    # `python -m skep.landlock`): this checkout's packages, chained by a .pth
    purelib = Path(sysconfig.get_path("purelib", vars={"base": venv, "platbase": venv}))
    chained = [p for p in sys.path if Path(p).name in ("site-packages", "dist-packages")]
    (purelib / "skep-under-test.pth").write_text(
        "\n".join([*chained, str(Path(skep.__file__).parents[1])]) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(sys, "executable", str(venv / "bin" / "python"))
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(sys, "exec_prefix", str(venv))
    code = f"import sys\nprint(sys.prefix)\n{_probe_script(canaries)}"
    result = run_code_fast("python", code, skep_home=tmp_path / "skep")
    assert result is not None and result["exit_code"] == 0, result
    prefix, probe = result["output"].splitlines()
    assert Path(prefix).resolve() == venv.resolve()
    assert _reads(json.loads(probe)) == []


# --- diagnose_run ------------------------------------------------------------


def _kept_run(store: RunStore, repo: Path, tmp_path: Path, recorded: dict[str, Any] | None) -> str:
    """A kept worktree of ``repo`` with a dispatched line recording ``recorded``."""
    worktree = tmp_path / "kept"
    git(repo, "worktree", "add", "-q", "--detach", str(worktree))
    task = mint_task(workspace=worktree, instructions="x", budget=DEFAULT_BUDGET)
    store.create_run(task, repo=repo, ref=None, execution_mode="sandbox")
    if recorded is not None:
        store.transition(task.task_id, "dispatched", json.dumps(recorded))
    return task.task_id


@needs_strict_wall
def test_diagnose_reads_no_home_canary_and_still_runs_python_and_pytest(
    repo: Path, config: SupervisorConfig, canaries: list[Path], tmp_path: Path
) -> None:
    _ordinary_canaries_open_without_the_wall(canaries, tmp_path)
    store = RunStore(config.db_path)
    try:
        task_id = _kept_run(store, repo, tmp_path, {"read_allowlist": []})
        (tmp_path / "kept" / "test_tiny.py").write_text(_TINY_TEST, encoding="utf-8")
        probe = tmp_path / "kept" / "probe.py"
        probe.write_text(_PROBE, encoding="utf-8")
        python = sys.executable
        command = (
            f"{python} probe.py {' '.join(map(str, canaries))} {repo / 'existing.py'} "
            f"&& {python} -m pytest -q -p no:cacheprovider test_tiny.py"
        )
        result = diagnose_run(store, config, task_id, command=command)
    finally:
        store.close()
    assert result["exit_code"] == 0, result
    probe_line, _, pytest_out = result["stdout"].partition("\n")
    seen = json.loads(probe_line)
    # the operator's own checkout, outside the kept tree, is a fifth canary
    assert len(seen) == 5 and _reads(seen) == [], seen
    assert "1 passed" in pytest_out
    assert result["sandboxed"] is True


@pytest.mark.parametrize(
    ("recorded", "expected"),
    [
        ({"read_allowlist": []}, ()),
        ({"read_allowlist": ["/srv/data"]}, (Path("/srv/data"),)),
        ({"g10_read_allowlist": []}, ()),  # G3: an engine with no wall of its own
        ({}, None),  # the run read denylist-only, so does its diagnosis
        (None, None),  # no dispatched line at all
    ],
)
def test_diagnose_takes_the_wall_the_run_recorded(
    repo: Path, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    recorded: dict[str, Any] | None, expected: Any,
) -> None:  # fmt: skip
    seen: dict[str, Any] = {}
    monkeypatch.setattr(sandbox, "write_profile", lambda path, **kw: seen.update(kw) or path)
    monkeypatch.setattr(sandbox, "wrap_command", lambda argv, profile: ["true"])
    # /srv/data need not exist here (the package exports a function under
    # the module's name, so the module comes through importlib)
    reverify_module = importlib.import_module("skep.supervisor.reverify")
    monkeypatch.setattr(reverify_module, "_moved_read_roots", lambda paths: [])
    store = RunStore(config.db_path)
    try:
        task_id = _kept_run(store, repo, tmp_path, recorded)
        diagnose_run(store, config, task_id, command="true")
    finally:
        store.close()
    assert seen["read_allowlist"] == expected


def test_diagnose_refuses_a_recorded_wall_that_moved(
    repo: Path, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G1, as G10 has it: a recorded root that is a link now would read
    whatever it points at today, past the worker's wall — refuse."""
    real, link = tmp_path / "data", tmp_path / "data-link"
    real.mkdir()
    link.symlink_to(real)
    called: list[object] = []
    monkeypatch.setattr(sandbox, "write_profile", lambda *a, **kw: called.append(kw))
    store = RunStore(config.db_path)
    try:
        task_id = _kept_run(store, repo, tmp_path, {"read_allowlist": [str(link)]})
        with pytest.raises(ValueError, match="recorded read wall"):
            diagnose_run(store, config, task_id, command="true")
    finally:
        store.close()
    assert called == []


def test_diagnose_refuses_a_strict_wall_the_backend_cannot_hold(
    repo: Path, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A strict run on a host now on podman: refused, never run with open reads."""
    podman = sandbox.SandboxAvailability(True, backend="podman")
    monkeypatch.setattr(sandbox, "require_usable", lambda backend=None: podman)
    store = RunStore(config.db_path)
    try:
        task_id = _kept_run(store, repo, tmp_path, {"read_allowlist": []})
        with pytest.raises(ValueError, match="strict read confinement"):
            diagnose_run(store, replace(config, sandbox=True), task_id, command="true")
    finally:
        store.close()


def _forge_pointer_to_foreign_repo(kept: Path, foreign: Path, repo: Path) -> None:
    (kept / ".git").write_text(f"gitdir: {foreign / '.git'}\n", encoding="utf-8")


def _forge_commondir(kept: Path, foreign: Path, repo: Path) -> None:
    # the run's own git dir is writable in its wall: a `commondir` there
    # moves git's common dir wherever the worker likes
    git_dir = Path((kept / ".git").read_text(encoding="utf-8").split(":", 1)[1].strip())
    (git_dir / "commondir").write_text(f"{foreign / '.git'}\n", encoding="utf-8")


def _forge_pointer_to_a_sibling_run(kept: Path, foreign: Path, repo: Path) -> None:
    sibling = kept.parent / "sibling-run"
    git(repo, "worktree", "add", "-q", "--detach", str(sibling))
    (kept / ".git").write_text((sibling / ".git").read_text(), encoding="utf-8")


@pytest.mark.parametrize(
    "forge",
    [_forge_pointer_to_foreign_repo, _forge_commondir, _forge_pointer_to_a_sibling_run],
)
def test_diagnose_refuses_git_metadata_the_kept_tree_points_elsewhere(
    repo: Path, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    forge: Any,
) -> None:  # fmt: skip
    """S1 (I2): the kept tree is the worker's output. Its ``.git`` pointer (or
    a ``commondir`` in its own git dir) resolving outside the run's repo would
    bind that directory readable and its ``objects/`` writable — refuse."""
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    git(foreign, "init", "-q")
    called: list[dict[str, Any]] = []
    monkeypatch.setattr(sandbox, "write_profile", lambda *a, **kw: called.append(kw))
    store = RunStore(config.db_path)
    try:
        task_id = _kept_run(store, repo, tmp_path, {"read_allowlist": []})
        forge(tmp_path / "kept", foreign, repo)
        with pytest.raises(ValueError, match="the command was not run"):
            diagnose_run(store, config, task_id, command="true")
    finally:
        store.close()
    assert called == [], [(kw["extra_readable"], kw["extra_writable"]) for kw in called]


def test_diagnose_binds_the_run_repos_git_metadata(
    repo: Path, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(sandbox, "write_profile", lambda path, **kw: seen.update(kw) or path)
    monkeypatch.setattr(sandbox, "wrap_command", lambda argv, profile: ["true"])
    store = RunStore(config.db_path)
    try:
        task_id = _kept_run(store, repo, tmp_path, {"read_allowlist": []})
        diagnose_run(store, config, task_id, command="true")
    finally:
        store.close()
    common = (repo / ".git").resolve()
    assert tuple(seen["extra_readable"]) == (common,)
    assert tuple(seen["extra_writable"]) == (common / "worktrees" / "kept", common / "objects")


# --- skep doctor says what is true (I8) --------------------------------------


@pytest.mark.parametrize("backend", ["bubblewrap", "seatbelt"])
def test_doctor_names_the_fast_lane_and_diagnose_walls_as_they_now_are(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    from skep import status

    monkeypatch.setattr(
        status, "sandbox_availability", lambda: sandbox.SandboxAvailability(True, backend=backend)
    )
    detail = status._sandbox_check(tmp_path)["detail"]
    assert "denylist-only;" not in detail  # no lane is named denylist-only any more
    assert "forged tools, script schedules and the fast lane always strict" in detail
    assert (
        "diagnose re-runs take their run's recorded strict setting and extra read paths, "
        "plus today's system and toolchain dirs" in detail
    )
    # Wave 2's job, still true today: never claimed confined
    assert "MCP servers you register run on the host, unsandboxed" in detail
