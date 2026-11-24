"""Phase 05 review R1 D3 (I8, I9): `skep provider set-key`, `use` and
`add --activate` stage the candidate and probe it (model list + a 1-token
completion) BEFORE writing the key or switching the active provider. A
refused probe leaves everything byte-for-byte as it was, exits non-zero,
and never echoes the key."""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.cli import main
from skep.supervisor.serve.llm import provider_secret_path
from skep.supervisor.store import RunStore

from .fake_ollama import FakeOllama


@pytest.fixture
def fake() -> Iterator[FakeOllama]:
    # ollama.com's shape: the model list answers anyone, the completion 401s
    server = FakeOllama(api_key="sk-good", public_listing=True).start()
    yield server
    server.stop()


def _key(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    stdin = io.StringIO(value + "\n")
    stdin.isatty = lambda: False  # type: ignore[method-assign]
    monkeypatch.setattr("sys.stdin", stdin)


def _snapshot(home: Path) -> tuple[object, ...]:
    supervisor = home / "supervisor"
    store = RunStore(supervisor / "supervisor.sqlite3", readonly=True)
    try:
        rows = store.list_provider_profiles()
        settings = [store.get_setting(k) for k in ("llm_base_url", "llm_default_model")]
    finally:
        store.close()
    files = [
        p.read_bytes() if p.exists() else None
        for p in (home / "profile.json", provider_secret_path(supervisor, "good"))
    ]
    return rows, settings, files


def _add(home: Path, fake: FakeOllama, provider_id: str, *extra: str) -> int:
    return main(["--home", str(home), "provider", "add", provider_id, "--protocol", "ollama",
                 "--base-url", fake.base_url, "--model", "llama3.2", *extra])  # fmt: skip


@pytest.fixture
def live(tmp_path: Path, fake: FakeOllama, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home whose active provider `good` works."""
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    home = tmp_path / "home"
    assert _add(home, fake, "good") == 0
    _key(monkeypatch, "sk-good")
    assert main(["--home", str(home), "provider", "set-key", "good"]) == 0
    assert main(["--home", str(home), "provider", "use", "good"]) == 0
    return home


def test_set_key_refuses_a_dead_key_and_keeps_the_working_one(
    live: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _snapshot(live)
    capsys.readouterr()
    _key(monkeypatch, "sk-dead-candidate")
    assert main(["--home", str(live), "provider", "set-key", "good"]) != 0
    out, err = capsys.readouterr()
    assert "401" in err and "nothing changed" in err
    assert "sk-dead-candidate" not in out + err
    assert _snapshot(live) == before


def test_use_refuses_a_provider_whose_completion_fails(
    live: Path, fake: FakeOllama, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    monkeypatch.setenv("SKEP_R1_DEAD", "sk-dead-env")
    assert _add(live, fake, "dead", "--api-key-env", "SKEP_R1_DEAD") == 0
    before = _snapshot(live)
    capsys.readouterr()
    assert main(["--home", str(live), "provider", "use", "dead"]) != 0
    out, err = capsys.readouterr()
    assert "401" in err and "'good' is still active" in err
    assert "sk-dead-env" not in out + err
    assert _snapshot(live) == before


def test_add_activate_registers_nothing_when_the_probe_fails(
    live: Path, fake: FakeOllama, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    monkeypatch.setenv("SKEP_R1_DEAD", "sk-dead-env")
    before = _snapshot(live)
    capsys.readouterr()
    assert _add(live, fake, "dead", "--api-key-env", "SKEP_R1_DEAD", "--activate") != 0
    out, err = capsys.readouterr()
    assert "401" in err and "nothing changed" in err
    assert "sk-dead-env" not in out + err
    assert _snapshot(live) == before
    # a working candidate activates
    monkeypatch.setenv("SKEP_R1_DEAD", "sk-good")
    assert _add(live, fake, "dead", "--api-key-env", "SKEP_R1_DEAD", "--activate") == 0
    store = RunStore(live / "supervisor" / "supervisor.sqlite3", readonly=True)
    try:
        active = store.active_provider_profile()
    finally:
        store.close()
    assert active is not None and active.provider_id == "dead"
