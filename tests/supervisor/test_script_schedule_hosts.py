"""Phase 09 R2 (N1): a script schedule's hosts are concrete host patterns.

``("*",)`` handed to the wall made ``NetworkPolicy.is_allow_all`` true: no
``--unshare-net``, no netshim, raw sockets straight to 127.0.0.1 and the
LAN. ``("*", "x")`` allowed every host at the proxy. The wall now refuses
any entry that is not a concrete host (``netproxy.host_entry_problem``,
the forged tools' own check), and every face that creates a script
schedule refuses it first, with the reason."""

from __future__ import annotations

from pathlib import Path

import pytest

from skep.cli import main
from skep.supervisor import RunStore, SupervisorConfig, sandbox
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.netproxy import host_entry_problem
from skep.supervisor.scheduler import make_schedule
from skep.supervisor.serve.jobs import Dispatcher
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import execute_mutation, mutation_execution_decision
from skep.supervisor.wall import walled

from .conftest import serve_client

BAD = [("*",), ("*", "api.example.com"), ("http://x.example.com",), ("a b",), ("",)]


@pytest.mark.parametrize("entry", ["api.example.com", "*.example.com", "host:8443", "10.0.0.1"])
def test_concrete_hosts_pass(entry: str) -> None:
    assert host_entry_problem(entry) is None


@pytest.mark.parametrize("hosts", BAD)
def test_the_wall_refuses_anything_but_concrete_hosts(tmp_path: Path, hosts: tuple[str]) -> None:
    with (
        pytest.raises(sandbox.SandboxUnavailableError, match="not a host name"),
        walled(["true"], skep_home=tmp_path / "skep", hosts=hosts, workspace=tmp_path),
    ):
        pass  # pragma: no cover - never entered


@pytest.mark.parametrize("hosts", BAD)
def test_make_schedule_refuses_a_script_with_a_wildcard_host(hosts: tuple[str]) -> None:
    with pytest.raises(ValueError, match="not a host name"):
        make_schedule(
            name="s", repo="", instructions="x", interval_seconds=60,
            worker_kind="script", network=hosts,
        )  # fmt: skip


def test_chat_refuses_the_wildcard_at_proposal_and_on_confirm(tmp_path: Path) -> None:
    config = build_config(tmp_path / "skephome", None)
    config.home.mkdir(parents=True)
    store = RunStore(config.db_path)
    holder = ConfigHolder(config, store)
    args = {"name": "s", "caste": "script", "every": "1h", "instructions": "x", "network": ["*"]}
    try:
        proposed = mutation_execution_decision("propose_schedule", args, store=store, holder=holder)
        assert proposed is not None and proposed.verdict == "deny", proposed
        assert "not a host name" in str(proposed.detail)
        with pytest.raises(ValueError, match="not a host name"):
            execute_mutation(
                "propose_schedule", args, store=store, holder=holder,
                runner=Dispatcher(store=store, holder=holder), actor="chat-user", chat_id=None,
            )  # fmt: skip
        assert store.list_schedules() == []
    finally:
        store.close()


def test_rest_refuses_the_wildcard(config: SupervisorConfig) -> None:
    client = serve_client(config)
    refused = client.post(
        "/api/schedules",
        json={"name": "s", "caste": "script", "every": "1d", "instructions": "x",
              "network": ["*"]},
    )  # fmt: skip
    assert refused.status_code == 400 and "not a host name" in refused.json()["detail"]
    assert client.get("/api/schedules").json()["schedules"] == []


def test_the_cli_refuses_the_wildcard(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--home", str(tmp_path), "schedule", "add", "s", str(repo), "echo hi",
                 "--caste", "script",
                 "--every", "1d", "--network", "*"])  # fmt: skip
    assert code != 0 and "not a host name" in capsys.readouterr().err
