"""v121-F4 (DD-4): the decision log never loses a row (I8, I13).

Rotation renames the live file to the next numbered generation and never
deletes one; size check, rotation and append run under one flock; every row
carries ``v``; the readers see only the newest generation + the live file."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from skep.supervisor import policy_v3, policy_write
from skep.supervisor.policy_v3 import decision_log_paths, log_decision
from skep.supervisor.serve.actions import policy_decisions


def _rows(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _generations(audit: Path) -> list[str]:
    return sorted(p.name for p in (audit / "policy").glob("decisions.jsonl*"))


def test_rotations_keep_every_generation_and_every_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(policy_v3, "DECISION_LOG_MAX_BYTES", 1)
    for i in range(5):
        log_decision(tmp_path, {"n": i})
    policy = tmp_path / "policy"
    # Each append found the live file past the cap and rotated it first: the
    # first row lives in .1 (the oldest), the last one in the live file.
    assert [_rows(policy / f"decisions.jsonl.{g}") for g in range(1, 5)] == [
        [{"n": g - 1, "v": 1}] for g in range(1, 5)
    ]
    assert _rows(policy / "decisions.jsonl") == [{"n": 4, "v": 1}]


def test_generation_ten_follows_nine_numerically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = tmp_path / "policy"
    policy.mkdir()
    for g in (1, 2, 9):
        (policy / f"decisions.jsonl.{g}").write_text(json.dumps({"gen": g}) + "\n")
    (policy / "decisions.jsonl").write_text('{"gen": "live"}\n')
    (policy / "decisions.jsonl.bak").write_text("not a generation\n")
    monkeypatch.setattr(policy_v3, "DECISION_LOG_MAX_BYTES", 1)
    log_decision(tmp_path, {"gen": "new"})
    assert _rows(policy / "decisions.jsonl.10") == [{"gen": "live"}]
    assert _rows(policy / "decisions.jsonl.9") == [{"gen": 9}]  # untouched
    # The newest generation is .10, not .9 (a string sort would pick .9).
    assert decision_log_paths(tmp_path) == [
        policy / "decisions.jsonl.10",
        policy / "decisions.jsonl",
    ]


def test_concurrent_writers_lose_no_row_and_tear_no_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(policy_v3, "DECISION_LOG_MAX_BYTES", 4096)
    pad = "x" * 100

    def writer(t: int) -> None:
        for i in range(200):
            log_decision(tmp_path, {"t": t, "i": i, "value": pad})

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    files = list((tmp_path / "policy").glob("decisions.jsonl*"))
    assert len(files) > 10  # the writes crossed many rotations
    seen = [(row["t"], row["i"]) for f in files for row in _rows(f)]  # a torn line raises
    assert sorted(seen) == [(t, i) for t in range(8) for i in range(200)]


def test_every_row_carries_the_version(tmp_path: Path) -> None:
    log_decision(tmp_path, {"value": "ls"})
    log_decision(tmp_path, {"value": "pwd", "v": 7})
    assert [row["v"] for row in _rows(tmp_path / "policy" / "decisions.jsonl")] == [1, 1]


def test_no_log_yet_reads_as_empty(tmp_path: Path) -> None:
    assert decision_log_paths(tmp_path) == [tmp_path / "policy" / "decisions.jsonl"]
    assert policy_write.read_log(tmp_path) == []
    assert policy_decisions(tmp_path)["rows"] == []


def test_readers_see_only_the_newest_generation_and_the_live_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(policy_v3, "DECISION_LOG_MAX_BYTES", 1)
    for i in range(4):
        log_decision(tmp_path, {"at": f"2026-11-18T0{i}:00:00Z", "actor": "t", "n": i})
    assert [r and r["n"] for r in policy_write.read_log(tmp_path)] == [2, 3]
    report = policy_decisions(tmp_path)
    assert [r["n"] for r in report["rows"]] == [3, 2] and report["total"] == 2


def test_readers_tolerate_a_generation_renamed_mid_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A rotation between listing the files and opening them leaves a listed
    # path gone; the readers skip it instead of failing the read.
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "decisions.jsonl").write_text('{"actor": "t", "n": 1}\n')
    gone = policy / "decisions.jsonl.3"
    monkeypatch.setattr(
        policy_write, "decision_log_paths", lambda audit: [gone, policy / "decisions.jsonl"]
    )
    import skep.supervisor.serve.actions as actions

    monkeypatch.setattr(
        actions, "decision_log_paths", lambda audit: [gone, policy / "decisions.jsonl"]
    )
    assert policy_write.read_log(tmp_path) == [{"actor": "t", "n": 1}]
    assert policy_decisions(tmp_path)["rows"] == [{"actor": "t", "n": 1}]
