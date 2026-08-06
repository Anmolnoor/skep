"""v110-F1: the fleet sync verb — the operator's machine-sync script gains a
skep face without moving a guard.

The pin IS the design: ``skep sync --set`` is the only place the command can
be chosen (I4 — a chat lane that picked commands would be a shadow run_shell
without the Queen's git guard). Execution reads the pin, records what ran
(I8), and every refusal names the fix (I9).
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import HTTPException

from skep.supervisor import SupervisorConfig
from skep.supervisor.serve.actions import (
    FLEET_SYNC_STATE_KEY,
    fleet_sync_status,
    set_fleet_sync_command,
    sync_fleet,
)
from skep.supervisor.store import RunStore

from .conftest import serve_client


@pytest.fixture()
def store(config: SupervisorConfig) -> Iterator[RunStore]:
    store = RunStore(config.db_path)
    yield store
    store.close()


def test_pin_set_show_clear_roundtrip(store: RunStore) -> None:
    assert fleet_sync_status(store) == {"command": None, "last": None}
    result = set_fleet_sync_command(store, "  echo hive ")
    assert result["command"] == "echo hive"  # stored stripped
    assert fleet_sync_status(store)["command"] == "echo hive"
    set_fleet_sync_command(store, None)
    assert fleet_sync_status(store)["command"] is None


def test_an_empty_pin_refuses_with_the_shape_of_a_real_one(store: RunStore) -> None:
    with pytest.raises(HTTPException) as excinfo:
        set_fleet_sync_command(store, "   ")
    assert excinfo.value.status_code == 400
    assert "skep sync --set" in excinfo.value.detail


def test_unpinned_run_refuses_naming_the_fix(store: RunStore) -> None:
    """I9: the refusal teaches — it names the CLI that pins, and states the
    rule that chat can never choose the command."""
    with pytest.raises(HTTPException) as excinfo:
        sync_fleet(store)
    assert excinfo.value.status_code == 409
    assert "skep sync --set" in excinfo.value.detail
    assert "never choose" in excinfo.value.detail


def test_pinned_run_captures_output_and_records_state(store: RunStore) -> None:
    set_fleet_sync_command(store, "echo hive-sync-ok")
    result = sync_fleet(store)
    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert "hive-sync-ok" in result["stdout"]
    assert result["command"] == "echo hive-sync-ok"
    state = store.get_setting(FLEET_SYNC_STATE_KEY)
    assert state is not None
    assert state["ok"] is True
    assert state["command"] == "echo hive-sync-ok"
    assert isinstance(state["at"], str) and state["at"]
    assert fleet_sync_status(store)["last"] == state


def test_a_failing_sync_is_recorded_as_a_failure(store: RunStore) -> None:
    """I8: the record says what actually happened — exit code and ok:false,
    never a swallowed error."""
    set_fleet_sync_command(store, "echo broken >&2; exit 7")
    result = sync_fleet(store)
    assert result["ok"] is False
    assert result["exit_code"] == 7
    assert "broken" in result["stderr"]
    state = store.get_setting(FLEET_SYNC_STATE_KEY)
    assert state is not None
    assert state["ok"] is False


def test_timeout_reads_as_exit_minus_one(store: RunStore) -> None:
    set_fleet_sync_command(store, "sleep 5")
    result = sync_fleet(store, timeout_seconds=0.2)
    assert result["ok"] is False
    assert result["exit_code"] == -1
    assert "timed out" in result["stderr"]


def test_get_api_sync_is_read_only_status(config: SupervisorConfig) -> None:
    """The REST face is a read: running goes through the carded command path
    or the operator's CLI — there is deliberately no POST /api/sync."""
    client = serve_client(config)
    assert client.get("/api/sync").json() == {"command": None, "last": None}
    store = RunStore(config.db_path)
    try:
        set_fleet_sync_command(store, "echo from-the-terminal")
    finally:
        store.close()
    body = client.get("/api/sync").json()
    assert body["command"] == "echo from-the-terminal"
    assert body["last"] is None
    # No POST face exists.
    assert client.post("/api/sync").status_code == 405


def test_sync_fleet_is_a_carded_publishing_mutation() -> None:
    """v110-F2: carded like push_branch, and web-UI-only — absent from the
    channel confirm allow-list and from the always-advertised core set."""
    from skep.supervisor.serve.cards import risk
    from skep.supervisor.serve.channels import CHANNEL_CONFIRMABLE_ACTIONS
    from skep.supervisor.serve.tools import (
        COMMAND_TOOL_NAMES,
        CORE_TOOL_NAMES,
        MUTATING_TOOL_NAMES,
        tool_description,
    )

    assert "sync_fleet" in MUTATING_TOOL_NAMES
    assert "sync_fleet" in COMMAND_TOOL_NAMES  # the deck's /sync may propose it
    assert "sync_fleet" not in CHANNEL_CONFIRMABLE_ACTIONS
    assert "sync_fleet" not in CORE_TOOL_NAMES
    description = tool_description("sync_fleet")
    assert "never choose or change the command" in description
    assert "skep sync --set" in description
    risk_text = risk("sync_fleet", {})
    assert risk_text is not None
    assert "publishes to the remote" in risk_text


def test_model_args_can_never_steer_the_sync_command(
    store: RunStore, config: SupervisorConfig
) -> None:
    """I4: the execute arm ignores every argument — a model-authored
    {"command": ...} still runs the operator's pin, verbatim."""
    from skep.supervisor.serve.settings import ConfigHolder
    from skep.supervisor.serve.tools import execute_mutation

    holder = ConfigHolder(config, store)
    set_fleet_sync_command(store, "echo pinned-wins")
    result = execute_mutation(
        "sync_fleet",
        {"command": "echo model-was-here"},
        store=store,
        holder=holder,
        runner=None,  # type: ignore[arg-type]  # sync_fleet never dispatches
        actor="test",
    )
    assert result["command"] == "echo pinned-wins"
    assert "pinned-wins" in result["stdout"]
    assert "model-was-here" not in result["stdout"]


def test_cli_sync_pin_run_show(
    config: SupervisorConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    """The terminal face end-to-end: --set, bare run, --show, --clear."""
    from skep.supervisor.cli_cmds import cmd_sync

    home = config.home.parent  # build_config appends /supervisor itself

    def _args(**overrides: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "home": Path(home),
            "set": None,
            "clear": False,
            "show": False,
            "timeout": None,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    assert cmd_sync(_args()) == 2  # unpinned: refuse, teach
    assert "skep sync --set" in capsys.readouterr().err

    assert cmd_sync(_args(set="echo cli-sync-ok")) == 0
    assert "pinned: echo cli-sync-ok" in capsys.readouterr().out

    assert cmd_sync(_args()) == 0
    assert "cli-sync-ok" in capsys.readouterr().out

    assert cmd_sync(_args(show=True)) == 0
    shown = capsys.readouterr().out
    assert "echo cli-sync-ok" in shown
    assert "ok" in shown

    assert cmd_sync(_args(clear=True)) == 0
    capsys.readouterr()
    assert cmd_sync(_args(show=True)) == 0
    assert "pin one with skep sync --set" in capsys.readouterr().out
