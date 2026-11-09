"""ADR 0056 D2 (O5, was ADR 0054 A6/D8): a home that never existed starts with
every scope on engine v2 and a ``fresh_home_seeded_at`` stamp; an existing
home keeps whatever its switch says and gets no stamp."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import skep.supervisor.store as store_module
from skep.supervisor import RunStore
from skep.supervisor.policy_schema import SCOPES
from skep.supervisor.serve.settings import (
    FRESH_HOME_SEEDED_AT,
    POLICY_ENGINE_V2_SCOPES,
    engine_v2_scopes_from,
)


def test_the_store_literal_is_the_settings_key() -> None:
    source = Path(store_module.__file__).read_text()
    assert f'"{POLICY_ENGINE_V2_SCOPES}"' in source
    assert f'"{FRESH_HOME_SEEDED_AT}"' in source


def test_a_fresh_home_is_seeded_with_every_scope_and_a_stamp(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "skep.db")
    try:
        raw = store.get_setting(POLICY_ENGINE_V2_SCOPES)
        stamp = store.get_setting(FRESH_HOME_SEEDED_AT)
    finally:
        store.close()
    assert raw == list(SCOPES)
    assert engine_v2_scopes_from(raw) == sorted(SCOPES)
    assert isinstance(stamp, str) and stamp.endswith("Z")


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
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (FRESH_HOME_SEEDED_AT,))
    store = RunStore(db)
    try:
        assert store.get_setting(POLICY_ENGINE_V2_SCOPES) is None
        assert store.get_setting(FRESH_HOME_SEEDED_AT) is None
    finally:
        store.close()
