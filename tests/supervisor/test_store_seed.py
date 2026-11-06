"""ADR 0054 A6/D8: a home that never existed starts with shell+filesystem on
engine v2; an existing home keeps whatever its switch says."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import skep.supervisor.store as store_module
from skep.supervisor import RunStore
from skep.supervisor.serve.settings import POLICY_ENGINE_V2_SCOPES, engine_v2_scopes_from


def test_the_store_literal_is_the_settings_key() -> None:
    source = Path(store_module.__file__).read_text()
    assert f'"{POLICY_ENGINE_V2_SCOPES}"' in source


def test_a_fresh_home_is_seeded_with_filesystem_and_shell(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "skep.db")
    try:
        raw = store.get_setting(POLICY_ENGINE_V2_SCOPES)
    finally:
        store.close()
    assert engine_v2_scopes_from(raw) == ["filesystem", "shell"]


def test_an_existing_home_keeps_an_empty_switch(tmp_path: Path) -> None:
    db = tmp_path / "skep.db"
    store = RunStore(db)
    store.set_setting(POLICY_ENGINE_V2_SCOPES, [])
    store.close()
    store = RunStore(db)
    try:
        assert store.get_setting(POLICY_ENGINE_V2_SCOPES) == []
    finally:
        store.close()


def test_an_existing_home_with_no_setting_is_not_seeded(tmp_path: Path) -> None:
    db = tmp_path / "skep.db"
    RunStore(db).close()
    with sqlite3.connect(db) as conn:  # a pre-1.2.0 home: data, no switch row
        conn.execute("DELETE FROM settings WHERE key = ?", (POLICY_ENGINE_V2_SCOPES,))
    store = RunStore(db)
    try:
        assert store.get_setting(POLICY_ENGINE_V2_SCOPES) is None
    finally:
        store.close()
