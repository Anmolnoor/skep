"""Phase 09 R1 (S7): a script schedule runs inside the strict wall.

It used to run supervisor-side with the operator's whole filesystem and
unrestricted egress (a scrubbed env leaves both). It now runs behind the
same wall as a forged tool (``wall.walled``): ADR 0068 strict reads plus
the files its own command names (its declared inputs), writes only in a
throwaway workspace, and egress only to the schedule's ``network`` hosts
through a FilteringProxy — none by default. No backend refuses the run.

The proofs run on the real sandbox (Seatbelt here, bubblewrap on Linux);
the "allowed host" is a local fake server the proxy is pointed at.
"""

from __future__ import annotations

import http.server
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import RunStore, SupervisorConfig, netproxy, sandbox, scheduler
from skep.supervisor.scheduler import make_schedule, run_due, run_schedule_script

needs_wall = pytest.mark.skipif(
    sandbox.availability().backend not in ("bubblewrap", "seatbelt"),
    reason="real sandbox proof (runs where bwrap or sandbox-exec is the backend)",
)


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    """A skep home with nothing else under tmp_path's own dir, so a file
    beside it is outside every root the wall opens."""
    (tmp_path / "skep" / "supervisor").mkdir(parents=True)
    return tmp_path / "skep"


@pytest.fixture()
def store(config: SupervisorConfig) -> Iterator[RunStore]:
    config.home.mkdir(parents=True, exist_ok=True)
    opened = RunStore(config.db_path)
    yield opened
    opened.close()


@pytest.fixture()
def fake_upstream() -> Iterator[tuple[int, list[str]]]:
    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            body = b"hello"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1], hits
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def fake_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    real = netproxy.resolve_public

    def resolve(host: str, port: int, resolver: Any = None) -> tuple[list[str], str]:
        if host.endswith(".test"):
            return ["127.0.0.1"], ""
        return real(host, port)

    monkeypatch.setattr(netproxy, "resolve_public", resolve)


_FETCH = (
    "import sys, urllib.request\n"
    "for url in sys.argv[1:]:\n"
    "    try:\n"
    "        print(url, urllib.request.urlopen(url, timeout=5).read().decode())\n"
    "    except Exception as exc:\n"
    "        print(url, 'blocked', type(exc).__name__)\n"
)


def test_declared_inputs_are_the_absolute_files_the_command_names(tmp_path: Path) -> None:
    script, ics, folder = tmp_path / "brief.py", tmp_path / "cal.ics", tmp_path / "dir"
    for path in (script, ics):
        path.write_text("x")
    folder.mkdir()
    (tmp_path / "link.py").symlink_to(script)
    command = (
        f"A=1 SKEP_CALENDAR_ICS={ics} python3 {script} --x {folder} "
        f"{tmp_path / 'link.py'} {tmp_path / 'missing'} rel/path"
    )
    assert scheduler.script_inputs(command) == (ics, script)
    assert scheduler.script_inputs("echo 'unterminated") == ()


@needs_wall
def test_a_script_reads_only_its_declared_inputs(home: Path) -> None:
    declared = home.parent / "declared.txt"
    declared.write_text("DECLARED-OK")
    (home.parent / "canary.txt").write_text("CANARY-SECRET")
    # The canary's path is computed in the shell, so the command never names it.
    output, _ok = run_schedule_script(
        f'cat {declared}; echo; cat "$(dirname {declared})/canary.txt"; echo rc=$?',
        skep_home=home,
    )
    assert "DECLARED-OK" in output, output
    assert "CANARY-SECRET" not in output and "rc=1" in output


@needs_wall
def test_a_script_writes_only_its_workspace(home: Path) -> None:
    target = home.parent / "planted.txt"
    output, ok = run_schedule_script(f'echo x > "$HOME/ok" && echo x > {target}', skep_home=home)
    assert ok is False and not target.exists(), output


@needs_wall
def test_a_script_without_hosts_has_no_network(
    home: Path, fake_upstream: tuple[int, list[str]]
) -> None:
    port, hits = fake_upstream
    output, _ok = run_schedule_script(
        f'{sys.executable} -c "{_FETCH}" http://127.0.0.1:{port}/direct', skep_home=home
    )
    assert "blocked" in output and hits == [], output
    assert "PROXY" not in run_schedule_script("env", skep_home=home)[0].upper()


@needs_wall
@pytest.mark.usefixtures("fake_dns")
def test_a_script_reaches_its_declared_hosts_and_nothing_else(
    home: Path, fake_upstream: tuple[int, list[str]]
) -> None:
    port, hits = fake_upstream
    output, ok = run_schedule_script(
        f'{sys.executable} -c "{_FETCH}" http://allowed.test:{port}/ok http://other.test:{port}/no',
        skep_home=home,
        hosts=("allowed.test",),
    )
    assert ok is True, output
    assert f"http://allowed.test:{port}/ok hello" in output
    assert f"http://other.test:{port}/no blocked" in output
    assert hits == ["/ok"]


def test_no_wall_refuses_the_script(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sandbox,
        "availability",
        lambda backend=None: sandbox.SandboxAvailability(False, reason="no backend here"),
    )
    output, ok = run_schedule_script("echo ran", skep_home=home)
    assert ok is False and "ran" not in output.split(":", 1)[0]
    assert output.startswith("script refused: no usable sandbox backend (no backend here)")


def test_a_declared_input_inside_the_skep_home_refuses_the_script(home: Path) -> None:
    secret = home / "supervisor" / "mail-secret"
    secret.write_text("pw")
    output, ok = run_schedule_script(f"cat {secret}", skep_home=home)
    assert ok is False and output.startswith("script refused:") and "skep home" in output


def test_the_tick_walls_a_script_with_the_schedules_hosts(
    store: RunStore, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake(command: str, **kwargs: Any) -> tuple[str, bool]:
        seen.update(kwargs, command=command)
        return ("brief", True)

    monkeypatch.setattr(scheduler, "run_schedule_script", fake)
    store.add_schedule(
        make_schedule(
            name="brief", repo="", instructions="python3 brief.py", interval_seconds=86400,
            worker_kind="script", network=["api.open-meteo.com"],
            start_at="2026-06-11T00:00:00Z",
        )
    )  # fmt: skip
    run_due(store=store, config=config, now="2026-06-11T09:00:00Z")
    assert seen["command"] == "python3 brief.py"
    assert seen["hosts"] == ("api.open-meteo.com",)
    assert seen["skep_home"] == config.home.parent
