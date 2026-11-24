"""Phase 08 8.1: the hash-chained ``"v": 2`` decision log (I8, I11, I13).

Every row names the one before it (``seq``, ``prev_hash``, ``hash``); the
chain runs unbroken across rotated generations and past the old ``"v": 1``
rows, which stay as written and read as unchained. ``skep ledger verify``
names the first break by file and line; ``skep proof export`` writes a local
receipt that says what a local chain does and does not prove.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from skep.cli import main
from skep.supervisor import RunStore, SupervisorConfig, policy_v3
from skep.supervisor.forensics import build_run_forensics, render_forensics
from skep.supervisor.ledger_proof import build_receipt, render_receipt, verify_ledger
from skep.supervisor.policy_v3 import chain_hash, log_decision

from .test_forensics import CANARY, _row, _run


def _lines(path: Path) -> list[str]:
    return path.read_text().splitlines()


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in _lines(path)]


def _live(audit: Path) -> Path:
    return audit / "policy" / "decisions.jsonl"


def _flip(path: Path, line: int, old: str, new: str) -> None:
    lines = _lines(path)
    assert old in lines[line - 1]
    lines[line - 1] = lines[line - 1].replace(old, new, 1)
    path.write_text("".join(f"{each}\n" for each in lines))


def test_rows_chain_from_genesis(tmp_path: Path) -> None:
    for n in range(3):
        log_decision(tmp_path, {"n": n, "value": "ls"})
    rows = _rows(_live(tmp_path))
    assert [(r["v"], r["seq"]) for r in rows] == [(2, 1), (2, 2), (2, 3)]
    assert rows[0]["prev_hash"] == "GENESIS"
    assert [r["prev_hash"] for r in rows[1:]] == [r["hash"] for r in rows[:-1]]
    for row in rows:
        body = {k: v for k, v in row.items() if k != "hash"}
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        assert row["hash"] == chain_hash(body) == hashlib.sha256(canonical).hexdigest()
    result = verify_ledger(tmp_path)
    assert result["status"] == "VALID"
    assert result["head"] == {
        "seq": 3,
        "hash": rows[-1]["hash"],
        "file": str(_live(tmp_path)),
        "line": 3,
    }
    assert result["chained"] == 3 and result["unchained"] == 0


def test_a_caller_hash_or_version_never_bends_the_chain(tmp_path: Path) -> None:
    log_decision(tmp_path, {"value": "pwd", "v": 7, "hash": "mine", "seq": 99})
    (row,) = _rows(_live(tmp_path))
    assert (row["v"], row["seq"], row["prev_hash"]) == (2, 1, "GENESIS")
    assert verify_ledger(tmp_path)["status"] == "VALID"


def test_v1_rows_stay_as_written_and_read_unchained(tmp_path: Path) -> None:
    policy = tmp_path / "policy"
    policy.mkdir()
    old = '{"value": "ls", "v": 1}\n{"value": "pwd"}\nnot json at all\n'
    (policy / "decisions.jsonl.1").write_text('{"value": "older", "v": 1}\n')
    _live(tmp_path).write_text(old)
    log_decision(tmp_path, {"value": "new"})
    log_decision(tmp_path, {"value": "newer"})
    text = _live(tmp_path).read_text()
    assert text.startswith(old)  # never rewritten
    first, second = (json.loads(line) for line in text.splitlines()[3:])
    assert (first["seq"], first["prev_hash"]) == (1, "GENESIS")
    assert second["prev_hash"] == first["hash"]
    result = verify_ledger(tmp_path)
    assert result["status"] == "VALID"
    assert (result["unchained"], result["chained"]) == (4, 2)
    assert result["head"]["line"] == 5


def test_rotation_carries_the_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(policy_v3, "DECISION_LOG_MAX_BYTES", 1)
    for n in range(5):
        log_decision(tmp_path, {"n": n})
    policy = tmp_path / "policy"
    files = [policy / f"decisions.jsonl.{g}" for g in range(1, 5)] + [_live(tmp_path)]
    rows = [row for path in files for row in _rows(path)]
    assert [r["seq"] for r in rows] == [1, 2, 3, 4, 5]
    assert [r["prev_hash"] for r in rows[1:]] == [r["hash"] for r in rows[:-1]]
    result = verify_ledger(tmp_path)
    assert result["status"] == "VALID" and result["head"]["seq"] == 5


def test_a_rotated_generation_with_an_empty_live_file_still_chains(tmp_path: Path) -> None:
    log_decision(tmp_path, {"n": 0})
    _live(tmp_path).rename(tmp_path / "policy" / "decisions.jsonl.1")
    _live(tmp_path).write_text("")
    log_decision(tmp_path, {"n": 1})
    assert _rows(_live(tmp_path))[0]["seq"] == 2
    assert verify_ledger(tmp_path)["status"] == "VALID"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('"value": "row 1"', '"value": "row 7"'),  # the content
        ('"seq": 2', '"seq": 3'),  # the sequence
        ('", "', '",  "'),  # whitespace: the parse is the same, the bytes are not
        ('"prev_hash": "', '"prev_hash": "0'),  # the link
    ],
)
def test_one_flipped_character_is_caught_with_file_and_line(
    tmp_path: Path, capsys: Any, old: str, new: str
) -> None:
    for n in range(4):
        log_decision(tmp_path, {"value": f"row {n}"})
    copy = tmp_path / "ledger-tamper.jsonl"
    shutil.copy(_live(tmp_path), copy)
    assert main(["--home", str(tmp_path), "ledger", "verify", "--path", str(copy)]) == 0
    assert "VALID" in capsys.readouterr().out
    _flip(copy, 2, old, new)
    assert main(["--home", str(tmp_path), "ledger", "verify", "--path", str(copy)]) == 1
    out = capsys.readouterr().out
    assert "BROKEN" in out
    assert f"{copy}:2" in out


def test_a_tampered_rotated_generation_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(policy_v3, "DECISION_LOG_MAX_BYTES", 200)
    for n in range(6):
        log_decision(tmp_path, {"value": f"row {n}"})
    generation = tmp_path / "policy" / "decisions.jsonl.1"
    _flip(generation, 1, "row 0", "row 9")
    result = verify_ledger(tmp_path)
    assert result["status"] == "BROKEN"
    assert result["break"]["file"] == str(generation)
    assert result["break"]["line"] == 1


def test_a_deleted_or_reordered_row_breaks_the_chain(tmp_path: Path) -> None:
    for n in range(4):
        log_decision(tmp_path, {"n": n})
    lines = _lines(_live(tmp_path))
    _live(tmp_path).write_text("".join(f"{x}\n" for x in [lines[0], lines[2], lines[3]]))
    broken = verify_ledger(tmp_path)["break"]
    assert broken["line"] == 2 and "seq" in broken["reason"]
    _live(tmp_path).write_text("".join(f"{x}\n" for x in [lines[1], lines[0], *lines[2:]]))
    assert verify_ledger(tmp_path)["break"]["line"] == 1


def test_a_v1_row_inside_the_chain_is_a_break(tmp_path: Path) -> None:
    log_decision(tmp_path, {"n": 0})
    with _live(tmp_path).open("a") as handle:
        handle.write('{"value": "slipped in", "v": 1}\n')
    log_decision(tmp_path, {"n": 1})
    broken = verify_ledger(tmp_path)["break"]
    assert broken["line"] == 2 and "unchained" in broken["reason"]


def test_a_missing_oldest_generation_is_a_break_but_a_lone_copy_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setattr(policy_v3, "DECISION_LOG_MAX_BYTES", 1)
    for n in range(3):
        log_decision(tmp_path, {"n": n})
    copy = tmp_path / "copy.jsonl"
    shutil.copy(_live(tmp_path), copy)
    (tmp_path / "policy" / "decisions.jsonl.1").unlink()
    result = verify_ledger(tmp_path)
    assert result["status"] == "BROKEN"
    assert result["break"]["file"].endswith("decisions.jsonl.2")
    assert "starts at seq 2" in result["break"]["reason"]
    # --path reads one offline copy: its first row is its anchor, said so.
    offline = verify_ledger(path=copy)
    assert offline["status"] == "VALID" and offline["starts_at"] == 3
    assert main(["--home", str(tmp_path), "ledger", "verify", "--path", str(copy)]) == 0
    assert "starts at seq 3" in capsys.readouterr().out


def test_checkpoints_catch_a_truncated_tail_and_a_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(policy_v3, "CHECKPOINT_EVERY", 2)
    for n in range(5):
        log_decision(tmp_path, {"n": n})
    with sqlite3.connect(tmp_path / "policy" / "chain.sqlite3") as conn:
        assert [s for (s,) in conn.execute("SELECT seq FROM checkpoints ORDER BY seq")] == [2, 4]
    lines = _lines(_live(tmp_path))
    # Tail truncation: the chain alone still links, the checkpoint at seq 4 does not.
    _live(tmp_path).write_text("".join(f"{x}\n" for x in lines[:3]))
    result = verify_ledger(tmp_path)
    assert result["status"] == "BROKEN"
    assert "checkpoint" in result["break"]["reason"] and "seq 4" in result["break"]["reason"]
    # A full rewrite: every hash recomputed, so the chain links — the anchor does not.
    _live(tmp_path).unlink()
    (tmp_path / "policy" / "chain.sqlite3").rename(tmp_path / "kept.sqlite3")
    for n in range(5):
        log_decision(tmp_path, {"n": n, "rewritten": True})
    (tmp_path / "kept.sqlite3").replace(tmp_path / "policy" / "chain.sqlite3")
    result = verify_ledger(tmp_path)
    assert result["status"] == "BROKEN"
    assert result["break"]["line"] == 2 and "checkpoint" in result["break"]["reason"]


def test_concurrent_writers_never_fork_the_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(policy_v3, "DECISION_LOG_MAX_BYTES", 4096)
    monkeypatch.setattr(policy_v3, "CHECKPOINT_EVERY", 7)

    def writer(t: int) -> None:
        for i in range(60):
            log_decision(tmp_path, {"t": t, "i": i, "value": "x" * 50})

    code = (
        "import sys; from pathlib import Path; from skep.supervisor.policy_v3 import log_decision\n"
        "for i in range(60): log_decision(Path(sys.argv[1]), {'p': sys.argv[2], 'i': i})"
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", code, str(tmp_path), str(p)]) for p in range(3)
    ]
    threads = [threading.Thread(target=writer, args=(t,)) for t in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert [proc.wait(timeout=120) for proc in procs] == [0, 0, 0]
    result = verify_ledger(tmp_path)
    assert result["status"] == "VALID", result["break"]
    assert result["head"]["seq"] == result["chained"] == 4 * 60 + 3 * 60


def test_verify_reads_a_snapshot_while_writers_rotate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(policy_v3, "DECISION_LOG_MAX_BYTES", 2048)
    monkeypatch.setattr(policy_v3, "CHECKPOINT_EVERY", 3)
    stop = threading.Event()

    def writer() -> None:
        for _ in range(1500):
            if stop.is_set():
                return
            log_decision(tmp_path, {"value": "y" * 40})

    log_decision(tmp_path, {"value": "first"})
    thread = threading.Thread(target=writer)
    thread.start()
    try:
        results = [verify_ledger(tmp_path) for _ in range(20)]
    finally:
        stop.set()
        thread.join()
    assert all(r["status"] == "VALID" for r in results), [r["break"] for r in results]


def test_verify_on_an_empty_home_and_a_missing_path(tmp_path: Path, capsys: Any) -> None:
    result = verify_ledger(tmp_path / "audit")
    assert result["status"] == "VALID" and result["head"] is None
    assert main(["--home", str(tmp_path), "ledger", "verify"]) == 0
    assert "no chained rows" in capsys.readouterr().out
    missing = tmp_path / "nope.jsonl"
    assert main(["--home", str(tmp_path), "ledger", "verify", "--path", str(missing)]) == 2
    assert "nope.jsonl" in capsys.readouterr().err


def test_verify_json_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setattr(policy_v3, "CHECKPOINT_EVERY", 2)
    home = tmp_path / "home"
    audit = home / "supervisor" / "audit"
    for n in range(3):
        log_decision(audit, {"n": n})
    before = sorted((p, p.stat().st_mtime_ns) for p in home.rglob("*"))
    assert main(["--home", str(home), "ledger", "verify", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["head"]["seq"] == 3
    assert sorted((p, p.stat().st_mtime_ns) for p in home.rglob("*")) == before


# -- the receipt and forensics -------------------------------------------------


@pytest.fixture()
def config(config: SupervisorConfig, tmp_path: Path) -> SupervisorConfig:
    return replace(config, home=tmp_path / "home" / "supervisor")


@pytest.fixture()
def landed(config: SupervisorConfig, tmp_path: Path) -> str:
    task_id = _run(config, tmp_path, state="completed")
    patch = config.audit_dir / task_id / "patch.diff"
    patch.write_text("diff --git a/a.py b/a.py\n")
    store = RunStore(config.db_path)
    try:
        store.transition(
            task_id,
            "dispatched",
            json.dumps({"provider_key_relay": "on", "read_allowlist": ["/repo"]}),
        )
        store.add_artifact(
            task_id,
            kind="patch",
            audit_path=patch,
            sha256=hashlib.sha256(patch.read_bytes()).hexdigest(),
        )
        store.record_reverification(
            task_id,
            outcome="passed",
            worker_outcome="passed",
            confirmed=True,
            commands=["uv run pytest"],
            exit_codes=[0],
            detail="ok [command from the project's pinned verify_command]",
            base_commit="abc123",
            test_counts={
                "status": "ok",
                "runner": "pytest",
                "baseline": {"passed": 4, "total_executed": 4},
                "patched": {"passed": 5, "total_executed": 5},
            },
        )
    finally:
        store.close()
    log_decision(config.audit_dir, _row(task_id, note=f"SKEP_TEST_TOKEN={CANARY}"))
    log_decision(config.audit_dir, _row(task_id, value="git status"))
    log_decision(config.audit_dir, _row("someone-else"))
    return task_id


def _receipt(config: SupervisorConfig, task_id: str) -> dict[str, Any]:
    store = RunStore(config.db_path, readonly=True)
    try:
        return build_receipt(store, config.home, config.audit_dir, task_id)
    finally:
        store.close()


def test_the_receipt_holds_the_run_and_the_ledger_head(
    config: SupervisorConfig, landed: str
) -> None:
    receipt = _receipt(config, landed)
    head = verify_ledger(config.audit_dir)["head"]
    patch = config.audit_dir / landed / "patch.diff"
    assert receipt["task_id"] == landed
    assert receipt["baseline_sha"] == "abc123"
    assert receipt["patch"] == {
        "sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
        "on_disk": "matches",
    }
    assert receipt["skep_version"]
    assert receipt["sandbox"]["read_confinement"] == "strict"
    assert receipt["key_relay"] == "on"
    assert receipt["policy_baseline"]["state"] == "none"
    assert receipt["g10"]["v1"] == "clean"
    assert receipt["g10"]["executed_tests"] == {"baseline": 4, "patched": 5}
    assert receipt["g10"]["commands_source"] == "project"
    assert receipt["decisions"] == 2
    assert receipt["ledger"]["status"] == "VALID"
    assert receipt["ledger"]["head_hash"] == head["hash"]
    assert receipt["ledger"]["head_seq"] == head["seq"] == 3
    assert "not tamper-proof" in receipt["scope"]


def test_receipt_md_and_json_on_the_cli_carry_no_secret(
    config: SupervisorConfig, landed: str, capsys: Any
) -> None:
    home = str(config.home.parent)
    assert main(["--home", home, "proof", "export", landed]) == 0
    markdown = capsys.readouterr().out
    assert main(["--home", home, "proof", "export", landed, "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data == _receipt(config, landed) | {"exported_at": data["exported_at"]}
    for out in (markdown, json.dumps(data)):
        assert CANARY not in out
    assert "<details>" in markdown and data["ledger"]["head_hash"] in markdown
    assert data["scope"] in markdown
    lowered = markdown.lower().replace("not tamper-proof", "")
    assert "tamper-proof" not in lowered and "tamperproof" not in lowered


def test_a_tampered_v1_run_and_a_broken_ledger_read_as_such(
    config: SupervisorConfig, landed: str
) -> None:
    store = RunStore(config.db_path)
    try:
        store.record_reverification(
            landed,
            outcome="tampered",
            worker_outcome="passed",
            confirmed=False,
            commands=["uv run pytest"],
            exit_codes=[0],
            detail="tampered",
            tamper_flags=("test_file_deleted:tests/test_a.py",),
        )
    finally:
        store.close()
    _flip(_live(config.audit_dir), 2, '"git status"', '"git stash"')
    (config.audit_dir / landed / "patch.diff").write_text("changed\n")
    receipt = _receipt(config, landed)
    assert receipt["g10"]["v1"] == "tampered: test_file_deleted:tests/test_a.py"
    assert receipt["g10"]["executed_tests"] == {"baseline": "unknown", "patched": "unknown"}
    assert receipt["ledger"]["status"] == "BROKEN"
    assert receipt["patch"]["on_disk"].startswith("differs")
    markdown = render_receipt(receipt, "md")
    assert "BROKEN" in markdown and "decisions.jsonl:2" in markdown


def test_proof_export_unknown_task_is_a_clean_error(config: SupervisorConfig, capsys: Any) -> None:
    RunStore(config.db_path).close()
    assert main(["--home", str(config.home.parent), "proof", "export", "nope-123"]) == 2
    assert "no run" in capsys.readouterr().err


def test_forensics_shows_the_chain_head_and_v1(config: SupervisorConfig, landed: str) -> None:
    store = RunStore(config.db_path, readonly=True)
    try:
        report = build_run_forensics(store, config.audit_dir, landed)
    finally:
        store.close()
    head = verify_ledger(config.audit_dir)["head"]
    assert report["ledger"] == {
        "status": "VALID",
        "head_seq": head["seq"],
        "head_hash": head["hash"],
        "unchained_rows": 0,
        "break": None,
    }
    assert report["outcome"]["g10"]["v1"] == "clean"
    text = render_forensics(report, markdown=False)
    assert f"chain head seq {head['seq']} sha256 {head['hash']} (VALID)" in text
    assert "V1: clean" in text


def test_a_row_longer_than_the_tail_window_still_chains(tmp_path: Path) -> None:
    log_decision(tmp_path, {"value": "z" * 200_000})  # the tail read steps back 64 KB at a time
    log_decision(tmp_path, {"value": "after"})
    rows = _rows(_live(tmp_path))
    assert rows[1]["prev_hash"] == rows[0]["hash"]
    assert verify_ledger(tmp_path)["status"] == "VALID"


def test_an_unreadable_checkpoint_store_is_a_break(tmp_path: Path, capsys: Any) -> None:
    log_decision(tmp_path / "supervisor" / "audit", {"n": 0})
    (tmp_path / "supervisor" / "audit" / "policy" / "chain.sqlite3").write_text("not sqlite")
    assert main(["--home", str(tmp_path), "ledger", "verify", "--json"]) == 1
    broken = json.loads(capsys.readouterr().out)["break"]
    assert broken["file"] is None and "chain.sqlite3 could not be read" in broken["reason"]


def test_a_run_with_no_patch_and_no_g10_says_so(config: SupervisorConfig, tmp_path: Path) -> None:
    task_id = _run(config, tmp_path, state="failed")
    receipt = _receipt(config, task_id)
    assert receipt["patch"] == {"sha256": "none recorded", "on_disk": "none"}
    assert receipt["g10"] is None and receipt["baseline_sha"] == "unknown"
    assert receipt["sandbox"]["read_confinement"] == "none recorded"
    assert receipt["key_relay"] == "off"
    assert receipt["ledger"]["head_hash"] is None
    markdown = render_receipt(receipt, "md")
    assert "G10 not run" in markdown and "no chained rows yet" in markdown
