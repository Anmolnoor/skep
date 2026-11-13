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

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.supervisor import sandbox

# W33: the engine login stores, masked like every other credential since the
# per-run config redirect and the NO AUTH ENV refusal made them dead weight.
LOGINS = (
    "Library/Keychains/login.keychain-db",
    ".claude/.credentials.json",
    ".codex/auth.json",
)


@pytest.fixture(autouse=True)
def clear_sandbox_probe_cache() -> Iterator[None]:
    sandbox.availability.cache_clear()
    yield
    sandbox.availability.cache_clear()


@pytest.fixture(autouse=True)
def no_other_homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """W40: the mask list also covers every home directory on the machine;
    these tests compare exact sets, so they see only the homes they make."""
    from skep.supervisor import secret_floor

    monkeypatch.setattr(secret_floor, "HOME_DIRS", (f"{tmp_path}/no-homes/*",))
    for name in sandbox._SOCKET_ENV:  # R5: nor the sockets this shell's env names
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def fake_homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A fake HOME with canary credential files, and a skep home with secrets."""
    home = tmp_path / "fakehome"
    for rel in (".ssh/id_rsa", ".aws/credentials", ".config/gh/hosts.yml", ".netrc", *LOGINS):
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
        home / "Library" / "Keychains",
        home / ".claude" / ".credentials.json",
        home / ".codex" / "auth.json",
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
    # R5: notmpcopyup, or podman copies the secrets into the mask
    assert pod[pod.index(f"{home / '.ssh'}:notmpcopyup") - 1] == "--tmpfs"


def test_bwrap_skips_masks_already_hidden_by_its_tmpfs() -> None:
    args = sandbox._bubblewrap_args(
        writable_roots=[],
        network=sandbox.DENY_ALL_NETWORK,
        proxy_port=None,
        read_denied=[Path("/tmp/x/serve-token"), Path("/run/user/1/y")],
    )
    assert "/tmp/x/serve-token" not in args and "/run/user/1/y" not in args


def test_bwrap_masks_a_socket_inside_a_root_it_binds_back_into_tmp() -> None:
    """R5: /tmp is a fresh tmpfs except where a root is bound back in (a repo
    under /tmp): a socket in that git dir stayed connectable, its mask skipped."""
    sock = "/tmp/repo/.git/fsmonitor--daemon.ipc"
    args = sandbox._bubblewrap_args(
        writable_roots=[],
        network=sandbox.DENY_ALL_NETWORK,
        proxy_port=None,
        read_denied=[Path(sock), Path("/tmp/x/serve-token")],
        readable_roots=[Path("/tmp/repo/.git")],
    )
    assert args[args.index(sock) - 2 : args.index(sock)] == ["--ro-bind", "/dev/null"]
    assert "/tmp/x/serve-token" not in args


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
        *(home / rel for rel in LOGINS),
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


_SEATBELT_PROBE = """
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


@pytest.mark.skipif(
    sys.platform != "darwin" or not sandbox.availability().usable,
    reason="real Seatbelt proof (runs where sandbox-exec can apply a profile)",
)
@pytest.mark.parametrize("worker_llm_key", [True, False])
def test_a_real_seatbelt_worker_cannot_read_the_canaries(
    fake_homes: tuple[Path, Path], tmp_path: Path, worker_llm_key: bool
) -> None:
    """The macOS twin of the bubblewrap proof: secret_paths() resolves under
    the fake HOME the fixture exports, never the operator's."""
    from skep.supervisor import RunStore
    from skep.supervisor.providers import ProviderProfile

    home, skep_home = fake_homes
    sup = skep_home / "supervisor"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    for path in (
        *(home / rel for rel in (".zshrc", ".bashrc", ".profile", ".zsh_history")),
        home / ".bash_history",
        home / ".cargo" / "credentials.toml",
        home / ".claude.json",
        sup / "llm-secret-p2",
        sup / "audit" / "t-other" / "worker.log",
        sup / "chat-attachments" / "a.txt",
        workspace / "plain.txt",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"CANARY {path.name}\n", encoding="utf-8")
    (workspace / "link").symlink_to(home / ".ssh" / "id_rsa")
    store = RunStore(sup / "supervisor.sqlite3")
    try:
        for pid in ("p1", "p2"):
            store.upsert_provider_profile(
                ProviderProfile(
                    provider_id=pid, protocol="ollama", base_url=f"http://{pid}.example", model="m"
                )
            )
        store.set_active_provider("p1")
    finally:
        store.close()
    profile = sandbox.write_profile(
        tmp_path / "p.sb", workspace=workspace, skep_home=skep_home, worker_llm_key=worker_llm_key
    )
    refused = [
        *(home / rel for rel in (".zshrc", ".bashrc", ".profile", ".zsh_history")),
        home / ".bash_history",
        home / ".ssh" / "id_rsa",
        home / ".aws" / "credentials",
        home / ".cargo" / "credentials.toml",
        home / ".claude.json",
        home / ".netrc",
        *(home / rel for rel in LOGINS),
        sup / "serve-token",
        skep_home / "serve.env",
        sup / "mail-secret",
        sup / "llm-secret-p2",  # an inactive profile's key
        sup / "audit",
        sup / "audit" / "t-other" / "worker.log",
        sup / "chat-attachments",
        workspace / "link",  # a symlink to ~/.ssh/id_rsa
    ]
    if (home / ".SSH").exists():  # the fold only exists on a case-insensitive volume
        refused.append(home / ".SSH" / "id_rsa")
    readable = [workspace / "plain.txt"]
    own_keys = [sup / "llm-secret", sup / "llm-secret-p1"]  # the active profile's key
    (readable if worker_llm_key else refused).extend(own_keys)
    # Not pinned either way: .npmrc and supervisor.sqlite3 stay readable on
    # purpose today (SANDBOX_READABLE_SECRETS) and are an open operator decision.
    proc = subprocess.run(
        sandbox.wrap_command(
            [sys.executable, "-c", _SEATBELT_PROBE, *map(str, refused + readable)], profile
        ),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    # PermissionError, not FileNotFoundError: the wall refused a path that exists.
    expected = {str(p): "PermissionError" for p in refused} | {str(p): "read" for p in readable}
    assert json.loads(proc.stdout) == expected


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
    declared exemption — ~/.npmrc for `npm install` — which stays on the
    policy floor. W33: the engine logins (keychain, claude_code's credential
    file, codex's auth.json) are masked: an engine authenticates from its env.
    A new policy entry is masked unless exempted on purpose."""
    from skep.supervisor.secret_floor import HOME_SECRETS

    home = tmp_path / "fakehome"
    for rel in HOME_SECRETS:
        (home / rel).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    masked = set(sandbox.secret_paths(tmp_path / "skep"))
    readable = {".npmrc"}
    assert set(sandbox.SANDBOX_READABLE_SECRETS) == readable
    assert readable < set(HOME_SECRETS)
    assert masked == {(home / rel).resolve() for rel in HOME_SECRETS if rel not in readable}
    assert (home / ".secrets").resolve() in masked


def test_the_mask_list_covers_every_home_and_spares_only_the_workers_own_npmrc(
    fake_homes: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W40: another home's credential files are masked too — all of them, its
    engine logins and its ``.npmrc`` included. The serve user's own engine
    logins are masked as well (W33); only its own ``.npmrc`` stays readable
    (SANDBOX_READABLE_SECRETS)."""
    from skep.supervisor import secret_floor

    home, skep_home = fake_homes
    other = tmp_path / "Users" / "bob"
    for rel in (".ssh/id_rsa", ".claude/.credentials.json", ".npmrc", "code/x.py"):
        (other / rel).parent.mkdir(parents=True, exist_ok=True)
        (other / rel).write_text("CANARY\n", encoding="utf-8")
    (home / ".npmrc").write_text("CANARY\n", encoding="utf-8")
    monkeypatch.setattr(secret_floor, "HOME_DIRS", (f"{tmp_path}/Users/*",))
    masked = set(sandbox.secret_paths(skep_home))
    assert {other / ".ssh", other / ".claude" / ".credentials.json", other / ".npmrc"} <= masked
    assert other / "code" / "x.py" not in masked
    assert home / ".claude" / ".credentials.json" in masked
    assert home / ".npmrc" not in masked


def test_known_agent_socket_places_are_masked_under_every_home(
    fake_homes: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5 (D19): a read-only bind does not stop connect(), so bwrap hides the
    places an agent socket lives (1Password, Docker Desktop, colima, ...)
    under this home and every other one, like the secret floor."""
    from skep.supervisor import secret_floor

    home, skep_home = fake_homes
    other = tmp_path / "Users" / "bob"
    for base in (home, other):
        for rel in sandbox.AGENT_SOCKETS:
            (base / rel).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(secret_floor, "HOME_DIRS", (f"{tmp_path}/Users/*",))
    masked = set(sandbox.secret_paths(skep_home))
    assert {base / rel for base in (home, other) for rel in sandbox.AGENT_SOCKETS} <= masked


def test_the_sockets_the_operators_env_names_are_masked_and_nothing_else(
    fake_homes: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5: $SSH_AUTH_SOCK and a unix:// DOCKER_HOST point at the operator's
    agents wherever they listen; a value that is no socket masks nothing (a
    stray SSH_AUTH_SOCK=$HOME must never lay a tmpfs over the home)."""
    import os
    import socket

    home, skep_home = fake_homes
    monkeypatch.chdir(home)
    srv = socket.socket(socket.AF_UNIX)
    srv.bind("d.sock")  # relative: AF_UNIX caps a path at 108 bytes
    try:
        monkeypatch.setenv("DOCKER_HOST", f"unix://{home / 'd.sock'}")
        monkeypatch.setenv("SSH_AUTH_SOCK", str(home))
        monkeypatch.setenv("CONTAINER_HOST", "ssh://core@localhost:22/run/podman.sock")
        masked = set(sandbox.secret_paths(skep_home))
    finally:
        srv.close()
    assert home / "d.sock" in masked
    assert home not in masked and Path(os.sep) not in masked


def test_rancher_desktops_socket_is_masked_but_its_tools_stay_on_path(
    fake_homes: tuple[Path, Path],
) -> None:
    """R5: ~/.rd holds Rancher Desktop's docker.sock AND its bin/ (kubectl,
    helm, nerdctl) that a worker's PATH may name; only the socket is hidden."""
    home, skep_home = fake_homes
    (home / ".rd" / "bin").mkdir(parents=True)
    (home / ".rd" / "bin" / "kubectl").write_text("#!/bin/sh\n", encoding="utf-8")
    (home / ".rd" / "docker.sock").write_text("", encoding="utf-8")
    masked = set(sandbox.secret_paths(skep_home))
    assert home / ".rd" / "docker.sock" in masked
    assert not {home / ".rd", home / ".rd" / "bin"} & masked


def test_a_runs_own_secret_is_bound_back_after_every_mask(fake_homes: tuple[Path, Path]) -> None:
    """ES-F122 follow-up: a file inside a masked dir (a codex run's login
    copy under the audit dir) is readable by its own run only — bound back
    AFTER the masks on every backend, so the mask still hides its siblings."""
    _home, skep_home = fake_homes
    sup = skep_home / "supervisor"
    (sup / "audit" / "t-own").mkdir(parents=True)
    own = sup / "audit" / "t-own" / "codex-login.json"
    masked = sandbox.secret_paths(skep_home)
    audit = str(sup / "audit")
    args = sandbox._bubblewrap_args(
        writable_roots=[Path("/srv/ws")],
        network=sandbox.NetworkPolicy(allow=("chatgpt.com:443",)),
        proxy_port=40000,
        unix_socket_path="/tmp/p.sock",
        read_denied=masked,
        unmasked=[own],
    )
    at = args.index(str(own))
    assert args[at - 1 : at + 2] == ["--ro-bind", str(own), str(own)]
    assert at > args.index(audit) and at < args.index("--")  # before the netshim argv
    pod = sandbox._podman_args(
        writable_roots=[Path("/srv/ws")],
        network=sandbox.DENY_ALL_NETWORK,
        rootfs=Path("/r"),
        read_denied=masked,
        unmasked=[own],
    )
    assert pod.index(f"{own}:{own}:ro") > pod.index(f"{audit}:notmpcopyup")
    lines = sandbox.build_profile(
        writable_roots=[Path("/w")], read_denied=masked, skep_home=skep_home, unmasked=[own]
    ).splitlines()
    allow = f'(allow file-read* (literal "{own}"))'
    assert lines.index(allow) > lines.index(f'(deny file-read* (subpath "{audit}"))')


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or sandbox.availability().backend != "bubblewrap",
    reason="real bubblewrap proof (runs where bwrap is the backend)",
)
def test_a_real_worker_reads_its_own_login_copy_and_never_another_runs(
    fake_homes: tuple[Path, Path], tmp_path: Path
) -> None:
    """ES-F122 follow-up: before, the copy sat in the run's worktree and any
    other sandboxed run (any engine, any project) could read it."""
    _home, skep_home = fake_homes
    workspace = tmp_path / "ws"
    workspace.mkdir()
    audit = skep_home / "supervisor" / "audit"
    copies = {}
    for task in ("t-own", "t-other"):
        (audit / task).mkdir(parents=True)
        copies[task] = audit / task / "codex-login.json"
        copies[task].write_text(f"CANARY {task}\n", encoding="utf-8")
        copies[task].chmod(0o400)
    profile = sandbox.write_profile(
        tmp_path / "p.profile",
        workspace=workspace,
        skep_home=skep_home,
        unmasked=[copies["t-own"]],
    )
    proc = subprocess.run(
        sandbox.wrap_command(
            [sys.executable, "-c", _READER, str(copies["t-own"]), str(copies["t-other"])], profile
        ),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "CANARY t-own" in proc.stdout
    assert "CANARY t-other" not in proc.stdout
