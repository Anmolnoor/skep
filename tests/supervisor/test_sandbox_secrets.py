"""ES-F4 (B05 + V3 sandbox half, I1/I6/I12): workers cannot read skep's
secrets or the operator's credential dirs, and Seatbelt cannot dial serve.

Before: bwrap ``--ro-bind / /`` and Seatbelt ``(allow default)`` left every
file readable, so a sandboxed worker read ``serve-token`` and, on the Mac,
POSTed ``/api/approvals/{id}/approve`` over loopback (the profile allowed
``localhost:*``) — it approved its own card and a commit landed (mac-p1).

``llm-secret*`` stays readable on purpose: the builtin worker authenticates
to its LLM with it until V6 moves the credential into the proxy.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.supervisor import sandbox


@pytest.fixture(autouse=True)
def clear_sandbox_probe_cache() -> Iterator[None]:
    sandbox.availability.cache_clear()
    yield
    sandbox.availability.cache_clear()


@pytest.fixture()
def fake_homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A fake HOME with canary credential files, and a skep home with secrets."""
    home = tmp_path / "fakehome"
    for rel in (".ssh/id_rsa", ".aws/credentials", ".config/gh/hosts.yml", ".netrc"):
        path = home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"CANARY {rel}\n", encoding="utf-8")
    skep_home = tmp_path / "skep"
    supervisor = skep_home / "supervisor"
    supervisor.mkdir(parents=True)
    for name in ("serve-token", "mail-secret", "discord-secret", "llm-secret", "llm-secret-p1"):
        (supervisor / name).write_text(f"CANARY {name}\n", encoding="utf-8")
    (skep_home / "serve.env").write_text("CANARY=serve.env\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    return home, skep_home


def test_secret_paths_cover_the_floor_and_skip_the_worker_llm_key(
    fake_homes: tuple[Path, Path],
) -> None:
    home, skep_home = fake_homes
    sup = skep_home / "supervisor"
    assert set(sandbox.secret_paths(skep_home)) == {
        home / ".ssh",
        home / ".aws",
        home / ".config" / "gh",
        home / ".netrc",
        skep_home / "serve.env",
        sup / "serve-token",
        sup / "mail-secret",
        sup / "discord-secret",
    }


def test_seatbelt_profile_denies_secret_reads_and_the_serve_port(
    fake_homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _home, skep_home = fake_homes
    monkeypatch.setenv("SKEP_SERVE_PORT", "8771")
    monkeypatch.setenv("SKEP_SERVE_URL", "http://127.0.0.1:8772")
    for network, port in (
        (sandbox.DENY_ALL_NETWORK, None),
        (sandbox.ALLOW_ALL_NETWORK, None),
        (sandbox.NetworkPolicy(allow=("pypi.org",)), 40000),
    ):
        text = sandbox.build_profile(
            writable_roots=[Path("/w")],
            network=network,
            proxy_port=port,
            read_denied=sandbox.secret_paths(skep_home),
        )
        lines = text.splitlines()
        assert f'(deny file-read* (literal "{skep_home / "supervisor" / "serve-token"}"))' in lines
        assert f'(deny file-read* (subpath "{Path.home() / ".ssh"}"))' in lines
        assert "llm-secret" not in text
        for serve_port in ("8765", "8771", "8772"):
            deny = f'(deny network-outbound (remote ip "localhost:{serve_port}"))'
            # Last match wins in SBPL: the deny must follow every loopback allow.
            allows = [i for i, line in enumerate(lines) if "(allow network-outbound" in line]
            assert lines.index(deny) > max(allows, default=-1)


def test_bwrap_and_podman_mask_the_secret_paths(fake_homes: tuple[Path, Path]) -> None:
    home, skep_home = fake_homes
    masked = sandbox.secret_paths(skep_home)
    args = sandbox._bubblewrap_args(
        writable_roots=[Path("/srv/ws")],
        network=sandbox.DENY_ALL_NETWORK,
        proxy_port=None,
        read_denied=masked,
    )
    token = str(skep_home / "supervisor" / "serve-token")
    assert args[args.index(token) - 2 : args.index(token)] == ["--ro-bind", "/dev/null"]
    assert args[args.index(str(home / ".ssh")) - 1] == "--tmpfs"
    # After the writable binds, so a mask wins even inside a writable root.
    assert args.index(token) > args.index("/srv/ws")

    pod = sandbox._podman_args(
        writable_roots=[Path("/srv/ws")],
        network=sandbox.DENY_ALL_NETWORK,
        rootfs=Path("/r"),
        read_denied=masked,
    )
    assert f"/dev/null:{token}:ro" in pod
    assert pod[pod.index(str(home / ".ssh")) - 1] == "--tmpfs"


def test_bwrap_skips_masks_already_hidden_by_its_tmpfs() -> None:
    args = sandbox._bubblewrap_args(
        writable_roots=[],
        network=sandbox.DENY_ALL_NETWORK,
        proxy_port=None,
        read_denied=[Path("/tmp/x/serve-token"), Path("/run/user/1/y")],
    )
    assert "/tmp/x/serve-token" not in args and "/run/user/1/y" not in args


_READER = """
import sys
for path in sys.argv[1:]:
    try:
        print("READ", path, open(path).read().strip())
    except OSError:
        print("DENIED", path)
"""


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or sandbox.availability().backend != "bubblewrap",
    reason="real bubblewrap proof (runs where bwrap is the backend)",
)
def test_a_real_sandboxed_worker_cannot_read_the_canaries(
    fake_homes: tuple[Path, Path], tmp_path: Path
) -> None:
    home, skep_home = fake_homes
    workspace = tmp_path / "ws"
    workspace.mkdir()
    profile = sandbox.write_profile(
        tmp_path / "p.profile", workspace=workspace, skep_home=skep_home
    )
    sup = skep_home / "supervisor"
    targets = [
        home / ".ssh" / "id_rsa",
        home / ".aws" / "credentials",
        home / ".netrc",
        sup / "serve-token",
        sup / "mail-secret",
        skep_home / "serve.env",
    ]
    proc = subprocess.run(
        sandbox.wrap_command(
            [sys.executable, "-c", _READER, *map(str, targets), str(sup / "llm-secret")], profile
        ),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "CANARY" not in proc.stdout.replace("CANARY llm-secret", ""), proc.stdout
    assert "READ " + str(sup / "llm-secret") in proc.stdout  # the worker's own LLM key (V6)


def test_fast_lane_code_does_not_inherit_the_daemon_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_code's fast lane ran with serve's whole environment — provider keys
    loaded from serve.env included — inside the wall."""
    from skep.supervisor.serve import fastlane

    captured: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "CANARY-key")
    monkeypatch.setattr(
        fastlane,
        "availability",
        lambda: sandbox.SandboxAvailability(True, backend="bubblewrap"),
    )
    monkeypatch.setattr(fastlane, "write_profile", lambda path, **kw: path)
    monkeypatch.setattr(fastlane, "wrap_command", lambda argv, profile: argv)
    monkeypatch.setattr(subprocess, "run", fake_run)
    fastlane.run_code_fast("python", "print(1)")
    env = captured["env"]
    assert isinstance(env, dict)
    assert "ANTHROPIC_API_KEY" not in env
    assert set(env) <= {"PATH", "HOME", "TMPDIR"}
    assert env["TMPDIR"] == captured["cwd"]  # private temp inside the wall (B11)


def test_spawner_hands_the_profile_this_runs_skep_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import SupervisorConfig, spawner
    from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task

    seen: dict[str, object] = {}
    monkeypatch.setattr(
        sandbox, "require_usable", lambda backend=None: sandbox.SandboxAvailability(True)
    )
    monkeypatch.setattr(sandbox, "write_profile", lambda path, **kw: seen.update(kw) or path)
    monkeypatch.setattr(sandbox, "wrap_command", lambda argv, profile, backend=None: argv)
    monkeypatch.setattr(spawner, "git_metadata_writable_roots", lambda _ws: ())
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: type("P", (), {"pid": 1})())
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config = SupervisorConfig(home=tmp_path / "skep" / "supervisor", worker_command=("w",))
    task = mint_task(workspace=workspace, instructions="x", budget=DEFAULT_BUDGET)
    spawner.spawn_worker(
        config, task, tmp_path / "t.json", tmp_path / "o.json", log_path=tmp_path / "w.log"
    )
    assert seen["skep_home"] == tmp_path / "skep"
