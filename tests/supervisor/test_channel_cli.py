"""v87-F3: ``skep channel status`` — one honest line per channel."""

from __future__ import annotations

from pathlib import Path

import pytest

from skep.cli import main
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.serve.channels import ChannelConfig, store_channel_secret
from skep.supervisor.store import RunStore


def _run(home: Path, *args: str) -> int:
    return main(["--home", str(home), *args])


def test_channel_status_states_the_actual_condition(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    config = build_config(home, None)
    store = RunStore(config.db_path)
    try:
        store.upsert_channel_config(ChannelConfig(channel="telegram", enabled=True))
        store.set_setting(
            "channel_last_delivery:telegram",
            {"ts": "2026-07-23T20:00:00Z", "ok": True, "kind": "info", "note": "delivered"},
        )
    finally:
        store.close()
    store_channel_secret(config.home, "telegram", "tok-t")

    assert _run(home, "channel", "status") == 0
    out = capsys.readouterr().out
    # The field-test failure mode, named: never configured is said in words.
    assert "discord" in out and "never configured" in out
    assert "secret: MISSING" in out
    # A working channel reads as what it is.
    assert "telegram" in out and "enabled" in out
    assert "secret: present" in out
    assert "delivered (ok) at 2026-07-23T20:00:00Z" in out


def test_channel_status_without_a_store_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(tmp_path / "empty", "channel", "status") == 1
    assert "no supervisor store" in capsys.readouterr().out
