"""W41: a read-only command must not write the store. A rw open writes even
when nothing changes — the last connection to close checkpoints a leftover
WAL into the database file, and a missing store is created — so the field
saw ``skep doctor`` and ``skep provider list`` move the real store's mtime."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from skep.cli import main
from skep.supervisor import RunStore

READ_ONLY = [
    ["status"],
    ["doctor"],
    ["provider", "list"],
    ["provider", "health"],
    ["project", "list"],
    ["repo", "list"],
    ["agents"],
    ["schedule", "list"],
    ["schedule", "health"],
    ["template", "list"],
    ["skill", "list"],
    ["memory", "list"],
    ["memory", "proposals"],
    ["role", "list"],
    ["channel", "status"],
    ["node", "list"],
    ["policy", "grants"],
]


def _snapshot(db: Path) -> tuple[int, bytes]:
    return db.stat().st_mtime_ns, db.read_bytes()


def _leave_a_wal(db: Path) -> None:
    """A writer that died without closing: its commit sits in the WAL, so the
    next connection to close cleanly would checkpoint it into ``db``."""
    script = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "from skep.supervisor import RunStore\n"
        "RunStore(Path(sys.argv[1])).set_setting('w41', 'CANARY-w41')\n"
        "os._exit(0)\n"
    )
    subprocess.run([sys.executable, "-c", script, str(db)], check=True)
    assert Path(f"{db}-wal").stat().st_size > 0


@pytest.mark.parametrize("argv", READ_ONLY, ids=lambda argv: " ".join(argv))
def test_a_read_only_command_leaves_the_store_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SKEP_HOME", str(home))
    db = home / "supervisor" / "supervisor.sqlite3"
    db.parent.mkdir(parents=True)
    RunStore(db).close()
    _leave_a_wal(db)
    before = _snapshot(db)
    main(["--home", str(home), *argv])
    assert _snapshot(db) == before


def test_a_read_of_a_home_with_no_store_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(tmp_path))
    assert main(["--home", str(home), "agents"]) == 0
    assert main(["--home", str(home), "node", "list"]) == 0
    assert not (home / "supervisor").exists()


def test_a_readonly_store_refuses_a_write(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    RunStore(db).close()
    store = RunStore(db, readonly=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            store.set_setting("k", "v")
    finally:
        store.close()


def test_a_readonly_open_still_migrates_a_stale_store(tmp_path: Path) -> None:
    """The one write a read may make is an actual change: a store an older
    skep wrote gets its missing columns, once."""
    db = tmp_path / "s.db"
    RunStore(db).close()
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE chats DROP COLUMN taint")
    store = RunStore(db, readonly=True)
    store.close()
    with sqlite3.connect(db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(chats)")}
    assert "taint" in columns
    before = _snapshot(db)
    RunStore(db, readonly=True).close()
    assert _snapshot(db) == before
    assert os.path.exists(db)


def test_provider_list_still_seeds_the_registry_from_legacy_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from skep.supervisor.serve.llm import LLM_BASE_URL, LLM_DEFAULT_MODEL, LLM_PROTOCOL

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(tmp_path))
    db = home / "supervisor" / "supervisor.sqlite3"
    store = RunStore(db)
    store.set_setting(LLM_BASE_URL, "http://localhost:11434")
    store.set_setting(LLM_DEFAULT_MODEL, "qwen3")
    store.set_setting(LLM_PROTOCOL, "ollama")
    store.close()
    assert main(["--home", str(home), "provider", "list"]) == 0
    assert "qwen3" in capsys.readouterr().out
    store = RunStore(db, readonly=True)
    try:
        assert [p.provider_id for p in store.list_provider_profiles()] == ["default"]
    finally:
        store.close()
