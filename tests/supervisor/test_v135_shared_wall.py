"""v135 Wave 2a (ADR 0069 2.1): the shared wall's four optional parameters.

``wall.walled`` and ``sandbox.write_profile`` gain a supervisor-owned
``profile_dir``, a ``home`` apart from the workspace, supervisor-only
``trusted_read_roots`` and ``skep_home_roots=False``. Every existing caller
passes none of them, and its profile and environment are what they were.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.supervisor import sandbox
from skep.supervisor.wall import walled

needs_strict_wall = pytest.mark.skipif(
    sandbox.availability().backend not in sandbox.STRICT_READ_BACKENDS,
    reason="real sandbox proof (runs where bwrap or sandbox-exec is the backend)",
)

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


@pytest.fixture(autouse=True)
def clear_probe() -> Iterator[None]:
    sandbox.availability.cache_clear()
    yield
    sandbox.availability.cache_clear()


@pytest.fixture()
def skep_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from skep.supervisor import secret_floor

    monkeypatch.setattr(secret_floor, "HOME_DIRS", (f"{tmp_path}/no-homes/*",))
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    home = tmp_path / "skep"
    (home / "supervisor").mkdir(parents=True)
    (home / "profile.json").write_text("{}", encoding="utf-8")
    (home / "supervisor" / "serve-token").write_text("TOKEN", encoding="utf-8")
    return home


@needs_strict_wall
def test_an_existing_caller_gets_the_profile_and_env_it_always_got(
    skep_home: Path, tmp_path: Path
) -> None:
    """The defaults ARE the old wall: same profile file, same text, same env."""
    work = tmp_path / "work"
    work.mkdir()
    with walled(["true"], skep_home=skep_home, hosts=(), workspace=work) as (_, env):
        old_text = (work / "sandbox.profile").read_text(encoding="utf-8")
        assert set(env) <= {"PATH", "LANG", "HOME", "TMPDIR", "TMPPREFIX", "XDG_RUNTIME_DIR"}
        assert env["HOME"] == str(work) and env["TMPDIR"] == str(work)
    explicit = sandbox.write_profile(
        tmp_path / "explicit.profile",
        workspace=work,
        network=sandbox.DENY_ALL_NETWORK,
        skep_home=skep_home,
        read_allowlist=(),
        trusted_read_roots=(),
        skep_home_roots=True,
    )
    assert explicit.read_text(encoding="utf-8") == old_text


@needs_strict_wall
def test_the_profile_lives_where_the_supervisor_says_and_never_where_the_child_writes(
    skep_home: Path, tmp_path: Path
) -> None:
    work, profiles = tmp_path / "work", tmp_path / "profiles"
    work.mkdir()
    profiles.mkdir()
    with walled(["true"], skep_home=skep_home, hosts=(), workspace=work, profile_dir=profiles):
        assert (profiles / "sandbox.profile").is_file()
        assert not (work / "sandbox.profile").exists()
    with (
        pytest.raises(sandbox.SandboxUnavailableError, match="child can write"),
        walled(["true"], skep_home=skep_home, hosts=(), workspace=work, profile_dir=work),
    ):
        pass


@needs_strict_wall
def test_a_separate_home_is_writable_and_holds_temp(skep_home: Path, tmp_path: Path) -> None:
    work, home = tmp_path / "work", tmp_path / "scratch"
    work.mkdir()
    home.mkdir()
    code = "import os, tempfile; open(os.path.join(os.environ['HOME'], 'x'), 'w').write('1'); " \
        "print(tempfile.gettempdir())"  # fmt: skip
    with walled(
        [sys.executable, "-c", code], skep_home=skep_home, hosts=(), workspace=work, home=home
    ) as (argv, env):
        proc = subprocess.run(argv, cwd=work, env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert env["HOME"] == str(home) and (home / "x").read_text() == "1"
    assert Path(proc.stdout.strip()).is_relative_to(home)


@needs_strict_wall
def test_trusted_read_roots_open_a_skep_home_tree_operator_paths_cannot(
    skep_home: Path, tmp_path: Path
) -> None:
    tree = skep_home / "mcp" / "srv" / "abc"
    tree.mkdir(parents=True)
    (tree / "entry.txt").write_text("installed", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    probe = [sys.executable, "-c", _PROBE, str(tree / "entry.txt")]
    # the control: as an operator path, the same tree refuses the wall
    with (
        pytest.raises(sandbox.SandboxUnavailableError, match="skep home"),
        walled(probe, skep_home=skep_home, hosts=(), workspace=work, read_allowlist=(tree,)),
    ):
        pass
    with walled(probe, skep_home=skep_home, hosts=(), workspace=work) as (argv, env):
        unbound = json.loads(subprocess.run(argv, env=env, capture_output=True, text=True).stdout)
    assert unbound[str(tree / "entry.txt")] != "read"
    with walled(
        probe, skep_home=skep_home, hosts=(), workspace=work, trusted_read_roots=(tree,)
    ) as (argv, env):
        bound = json.loads(subprocess.run(argv, env=env, capture_output=True, text=True).stdout)
    assert bound[str(tree / "entry.txt")] == "read"


@needs_strict_wall
def test_skep_home_roots_off_drops_profile_json_and_keeps_the_masks(
    skep_home: Path, tmp_path: Path
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    targets = [str(skep_home / "profile.json"), str(skep_home / "supervisor" / "serve-token")]
    probe = [sys.executable, "-c", _PROBE, *targets]
    with walled(probe, skep_home=skep_home, hosts=(), workspace=work) as (argv, env):
        default = json.loads(subprocess.run(argv, env=env, capture_output=True, text=True).stdout)
    assert default[targets[0]] == "read"  # the control: strict mode grants it by default
    with walled(probe, skep_home=skep_home, hosts=(), workspace=work, skep_home_roots=False) as (
        argv,
        env,
    ):
        off = json.loads(subprocess.run(argv, env=env, capture_output=True, text=True).stdout)
    assert off[targets[0]] != "read" and off[targets[1]] != "read"
    with walled(
        probe, skep_home=skep_home, hosts=(), workspace=work, skep_home_roots=False,
        trusted_read_roots=(skep_home / "supervisor",),
    ) as (argv, env):  # fmt: skip
        masked = json.loads(subprocess.run(argv, env=env, capture_output=True, text=True).stdout)
    assert masked[targets[1]] != "read"  # a trusted root never lifts a secret mask


@needs_strict_wall
def test_trusted_roots_need_a_strict_wall(skep_home: Path, tmp_path: Path) -> None:
    with pytest.raises(sandbox.SandboxUnavailableError, match="strict"):
        sandbox.write_profile(
            tmp_path / "p.profile",
            workspace=tmp_path,
            skep_home=skep_home,
            trusted_read_roots=(tmp_path,),
        )
