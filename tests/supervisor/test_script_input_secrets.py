"""Phase 09 R3 (N2, S10): a script input under a secret mask refuses — at
creation on every face, and again at the run — and a refused run says why
in the schedule's health.

Neon's repro: ``cat $HOME/.ssh/id_rsa`` named the masked key as an input;
bubblewrap copied it to a new path the masks did not cover, and the script
printed it. The canary here is not credential-shaped (CI runs gitleaks)."""

from __future__ import annotations

from pathlib import Path

import pytest

from skep.cli import main
from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.scheduler import (
    make_schedule,
    run_due,
    run_schedule_script,
    script_input_problem,
)
from skep.supervisor.serve.jobs import Dispatcher
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import execute_mutation, mutation_execution_decision

from .conftest import serve_client

CANARY = "CANARY-INPUT-NOT-A-CREDENTIAL"


@pytest.fixture()
def masked(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A canary at ~/.ssh/id_rsa of a temp HOME — never the real one, and
    outside every test's skep home (a tick's is the config's parent)."""
    fake = tmp_path_factory.mktemp("user")
    (fake / ".ssh").mkdir()
    key = fake / ".ssh" / "id_rsa"
    key.write_text(CANARY)
    monkeypatch.setenv("HOME", str(fake))
    return key


def test_the_check_names_the_mask(tmp_path: Path, masked: Path) -> None:
    problem = script_input_problem(f"cat {masked}", tmp_path / "skep")
    assert problem is not None and "secret mask" in problem and CANARY not in problem
    assert script_input_problem("echo hi", tmp_path / "skep") is None


def test_the_run_refuses_before_the_wall(tmp_path: Path, masked: Path) -> None:
    output, ok = run_schedule_script(f"cat {masked}", skep_home=tmp_path / "skep")
    assert ok is False and output.startswith("script refused:") and "secret mask" in output
    assert CANARY not in output


def test_make_schedule_refuses_it(tmp_path: Path, masked: Path) -> None:
    with pytest.raises(ValueError, match="secret mask"):
        make_schedule(
            name="s", repo="", instructions=f"cat {masked}", interval_seconds=60,
            worker_kind="script", skep_home=tmp_path / "skep",
        )  # fmt: skip


def test_chat_refuses_it_at_proposal_and_on_confirm(tmp_path: Path, masked: Path) -> None:
    config = build_config(tmp_path / "skephome", None)
    config.home.mkdir(parents=True)
    store = RunStore(config.db_path)
    holder = ConfigHolder(config, store)
    args = {"name": "s", "caste": "script", "every": "1h", "instructions": f"cat {masked}"}
    try:
        proposed = mutation_execution_decision("propose_schedule", args, store=store, holder=holder)
        assert proposed is not None and proposed.verdict == "deny", proposed
        assert proposed.reason == "schedule.deny.inputs" and "secret mask" in str(proposed.detail)
        with pytest.raises(ValueError, match="secret mask"):
            execute_mutation(
                "propose_schedule", args, store=store, holder=holder,
                runner=Dispatcher(store=store, holder=holder), actor="chat-user", chat_id=None,
            )  # fmt: skip
        assert store.list_schedules() == []
    finally:
        store.close()


def test_rest_refuses_it(config: SupervisorConfig, masked: Path) -> None:
    client = serve_client(config)
    refused = client.post(
        "/api/schedules",
        json={"name": "s", "caste": "script", "every": "1d", "instructions": f"cat {masked}"},
    )
    assert refused.status_code == 400 and "secret mask" in refused.json()["detail"]
    assert client.get("/api/schedules").json()["schedules"] == []


def test_the_cli_refuses_it(
    tmp_path: Path, masked: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--home", str(tmp_path / "h"), "schedule", "add", "s", f"cat {masked}",
                 "--caste", "script", "--every", "1d"])  # fmt: skip
    assert code != 0 and "secret mask" in capsys.readouterr().err


def test_a_refused_run_says_why_in_the_schedule_health(
    config: SupervisorConfig, masked: Path
) -> None:
    """S10: the tick's state carries the refusal, not a bare script_failed.
    The schedule predates the check (stored directly), so the run refuses."""
    store = RunStore(config.db_path)
    try:
        schedule = make_schedule(
            name="brief", repo="", instructions=f"cat {masked}", interval_seconds=300,
            worker_kind="script", start_at="2026-06-11T00:00:00Z",
        )  # fmt: skip
        store.add_schedule(schedule)
        (result,) = run_due(store=store, config=config, now="2026-06-11T09:00:00Z")
        assert result.state.startswith("script_failed: script refused:")
        assert "secret mask" in result.state and CANARY not in result.state
        stored = store.get_schedule("brief")
        assert stored is not None and stored.last_state == result.state
    finally:
        store.close()
