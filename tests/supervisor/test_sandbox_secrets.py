"""ES-F4 (B05 + V3 sandbox half, I1/I6/I12): workers cannot read skep's
secrets or the operator's credential dirs, and Seatbelt cannot dial serve.

Before: bwrap ``--ro-bind / /`` and Seatbelt ``(allow default)`` left every
file readable, so a sandboxed worker read ``serve-token`` and, on the Mac,
POSTed ``/api/approvals/{id}/approve`` over loopback (the profile allowed
``localhost:*``) — it approved its own card and a commit landed (mac-p1).

``llm-secret`` and the active profile's ``llm-secret-<id>`` stay readable
for an LLM-calling worker only (B125): the builtin worker authenticates to its
LLM with them until V6 moves the credential into the proxy.
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
    (sup / "audit" / "t-other").mkdir(parents=True)
    (sup / "chat-attachments").mkdir()
    floor = {
        home / ".ssh",
        home / ".aws",
        home / ".config" / "gh",
        home / ".netrc",
        skep_home / "serve.env",
        sup / "serve-token",
        sup / "mail-secret",
        sup / "discord-secret",
        sup / "audit",
        sup / "chat-attachments",
    }
    # B125: a run that does not call the LLM (every non-worker profile, a
    # non-provider caste) sees no LLM key at all...
    assert set(sandbox.secret_paths(skep_home)) == floor | {
        sup / "llm-secret",
        sup / "llm-secret-p1",
    }
    # ...and the builtin worker keeps the legacy key; another profile's key
    # stays hidden (no active profile here).
    assert set(sandbox.secret_paths(skep_home, worker_llm_key=True)) == floor | {
        sup / "llm-secret-p1"
    }


def test_the_worker_reads_only_the_active_profiles_llm_key(
    fake_homes: tuple[Path, Path],
) -> None:
    """B125: v108-F4's worker reads llm-secret-<active id>; every other
    profile's key is masked."""
    from skep.supervisor import RunStore
    from skep.supervisor.providers import ProviderProfile

    _home, skep_home = fake_homes
    sup = skep_home / "supervisor"
    (sup / "llm-secret-p2").write_text("CANARY p2\n", encoding="utf-8")
    store = RunStore(sup / "supervisor.sqlite3")
    try:
        for pid in ("p1", "p2"):
            store.upsert_provider_profile(
                ProviderProfile(
                    provider_id=pid,
                    protocol="ollama",
                    base_url=f"http://{pid}.example",
                    model="m",
                )
            )
        store.set_active_provider("p2")
    finally:
        store.close()
    masked = set(sandbox.secret_paths(skep_home, worker_llm_key=True))
    assert sup / "llm-secret-p1" in masked
    assert sup / "llm-secret-p2" not in masked
    assert sup / "llm-secret" not in masked


@pytest.mark.parametrize(
    ("worker_kind", "needs_key"),
    [("coding", True), ("document", True), ("script", False), ("audit", False)],
)
def test_the_spawner_unmasks_the_llm_key_only_for_castes_that_call_the_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worker_kind: str, needs_key: bool
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
    monkeypatch.setattr(spawner, "git_metadata_readable_roots", lambda _ws: ())
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: type("P", (), {"pid": 1})())
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config = SupervisorConfig(home=tmp_path / "skep" / "supervisor", worker_command=("w",))
    task = mint_task(
        workspace=workspace, instructions="x", budget=DEFAULT_BUDGET, worker_kind=worker_kind
    )
    spawner.spawn_worker(
        config, task, tmp_path / "t.json", tmp_path / "o.json", log_path=tmp_path / "w.log"
    )
    assert seen["worker_llm_key"] is needs_key
    # An external engine authenticates from its env, never skep's key.
    spawner.spawn_worker(
        config,
        task,
        tmp_path / "t.json",
        tmp_path / "o.json",
        log_path=tmp_path / "w.log",
        worker_argv=("claude-adapter",),
    )
    assert seen["worker_llm_key"] is False


def test_seatbelt_profile_denies_secret_reads_and_the_serve_port(
    fake_homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _home, skep_home = fake_homes
    # Mac finding F: a CLI `skep run` has no serve env; the port serve
    # recorded under its home is what the profile denies.
    (skep_home / "supervisor" / sandbox.SERVE_PORT_FILE).write_text("8771\n", encoding="utf-8")
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
            skep_home=skep_home,
        )
        lines = text.splitlines()
        assert f'(deny file-read* (literal "{skep_home / "supervisor" / "serve-token"}"))' in lines
        assert f'(deny file-read* (subpath "{Path.home() / ".ssh"}"))' in lines
        # B125: a profile that is not an LLM worker's hides the LLM key too.
        assert f'(deny file-read* (literal "{skep_home / "supervisor" / "llm-secret"}"))' in lines
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
    sup = skep_home / "supervisor"
    (home / ".zsh_history").write_text("CANARY history\n", encoding="utf-8")
    (home / ".claude.json").write_text("CANARY claude.json\n", encoding="utf-8")
    (sup / "audit" / "t-other").mkdir(parents=True)
    (sup / "audit" / "t-other" / "worker.log").write_text("CANARY audit\n", encoding="utf-8")
    profile = sandbox.write_profile(
        tmp_path / "p.profile", workspace=workspace, skep_home=skep_home, worker_llm_key=True
    )
    targets = [
        home / ".zsh_history",
        home / ".claude.json",
        sup / "audit" / "t-other" / "worker.log",
        sup / "llm-secret-p1",
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
    fastlane.run_code_fast("python", "print(1)", skep_home=Path("/nonexistent/skep-home"))
    env = captured["env"]
    assert isinstance(env, dict)
    assert "ANTHROPIC_API_KEY" not in env
    assert set(env) <= {"PATH", "HOME", "TMPDIR", "CLANG_MODULE_CACHE_PATH"}
    assert env["TMPDIR"] == captured["cwd"]  # private temp inside the wall (B11)
    # Mac finding B: clang/swiftc ignore TMPDIR for their module cache.
    assert env["CLANG_MODULE_CACHE_PATH"].startswith(str(captured["cwd"]))


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
    monkeypatch.setattr(spawner, "git_metadata_readable_roots", lambda _ws: ())
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: type("P", (), {"pid": 1})())
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config = SupervisorConfig(home=tmp_path / "skep" / "supervisor", worker_command=("w",))
    task = mint_task(workspace=workspace, instructions="x", budget=DEFAULT_BUDGET)
    spawner.spawn_worker(
        config, task, tmp_path / "t.json", tmp_path / "o.json", log_path=tmp_path / "w.log"
    )
    assert seen["skep_home"] == tmp_path / "skep"


def test_serve_records_its_port_for_cli_runs_on_the_same_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mac finding F: a CLI-dispatched worker reached serve because only
    serve's own process knew the port. serve now records it under its home,
    and a profile built for that home (no serve env at all) denies it."""
    import argparse

    import uvicorn

    from skep.supervisor.serve import serve_cmds

    monkeypatch.delenv("SKEP_SERVE_URL", raising=False)
    # cmd_serve runs uvicorn.Server via make_server (ES B119): stub the
    # Server, or a real one binds the port and the test never returns.
    monkeypatch.setattr(
        uvicorn.Server, "run", lambda self, sockets=None: setattr(self, "started", True)
    )
    home = tmp_path / "skep"
    args = argparse.Namespace(
        home=home, worker_cmd=None, auto_approve=False, host="127.0.0.1", port=9187
    )
    assert serve_cmds.cmd_serve(args) == 0
    text = sandbox.build_profile(writable_roots=[tmp_path / "ws"], skep_home=home)
    assert '(deny network-outbound (remote ip "localhost:9187"))' in text


def test_the_sandbox_masks_the_policy_floor_minus_what_engines_and_installs_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR #64 review: the sandbox masks every HOME_SECRETS path except the
    declared exemptions — engine logins (claude_code's keychain/credential
    file, codex's auth.json) and ~/.npmrc for `npm install` — which stay on
    the policy floor. A new policy entry is masked unless exempted on purpose."""
    from skep.supervisor.secret_floor import HOME_SECRETS

    home = tmp_path / "fakehome"
    for rel in HOME_SECRETS:
        (home / rel).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    masked = set(sandbox.secret_paths(tmp_path / "skep"))
    readable = {".npmrc", ".claude/.credentials.json", ".codex/auth.json", "Library/Keychains"}
    assert set(sandbox.SANDBOX_READABLE_SECRETS) == readable
    assert readable < set(HOME_SECRETS)
    assert masked == {(home / rel).resolve() for rel in HOME_SECRETS if rel not in readable}
    assert (home / ".secrets").resolve() in masked
