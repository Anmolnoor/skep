"""first-run O2 (ADR 0056 D1/D2): the locked baseline — canonical form, lock,
approvals ledger, verify — and the tamper / zero-state behaviour end to end.
Always a temp home: never ``~/.skep``."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from skep.supervisor import policy_baseline as pb
from skep.supervisor.policy_templates import load_policy_template


def _edit(home: Path, mutate: Any) -> None:
    """A hand edit: the file is 0444, which stops nobody who means it."""
    path = pb.baseline_path(home)
    os.chmod(path, 0o644)
    document = json.loads(path.read_text())
    mutate(document)
    path.write_text(json.dumps(document, indent=2) + "\n")


def _approvals(home: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in pb.ledger_path(home).read_text().splitlines()]


def test_canonical_form_ignores_formatting_and_tracks_content() -> None:
    builder = load_policy_template("builder")
    again = pb._load_layer(json.dumps(builder.model_dump(mode="json"), indent=7))
    assert pb.canonical_bytes(again) == pb.canonical_bytes(builder)
    document = json.loads(pb.canonical_bytes(builder))
    assert document["schema_version"] == 1 and list(document) == sorted(document)
    changed = builder.model_copy(update={"settings": {**builder.settings, "default_network": []}})
    assert pb.sha256_of(changed) != pb.sha256_of(builder)
    assert pb.sha256_of(builder, schema_version=2) != pb.sha256_of(builder)


def test_approve_writes_layer_lock_and_ledger_row_and_verifies_ok(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert isinstance(pb.verify(home), pb.NoBaseline)
    row = pb.write_lock(home, load_policy_template("builder"), approved_by="op")
    assert set(row) == {
        "kind",
        "sha256",
        "template",
        "approved_by",
        "approved_at",
        "skep_version",
        "schema_version",
    }
    assert row["kind"] == "baseline_approved" and row["template"] == "builder"
    assert _approvals(home) == [row]
    assert pb.ledger_path(home).parent == home / "audit" / "policy"
    assert stat.S_IMODE(pb.baseline_path(home).stat().st_mode) == 0o444
    lock = json.loads(pb.lock_path(home).read_text())
    assert {k: lock[k] for k in row} == row
    state = pb.verify(home)
    assert isinstance(state, pb.Ok) and state.schema_from is None
    assert all(s.enforced for s in state.enforced_layer().settings)
    assert state.enforced_layer().name == "template"
    assert pb.baseline_status(home)["state"] == "ok"


def test_a_reformat_is_not_tampering(tmp_path: Path) -> None:
    home = tmp_path / "home"
    pb.write_lock(home, load_policy_template("builder"), approved_by="op")
    path = pb.baseline_path(home)
    document = json.loads(path.read_text())
    os.chmod(path, 0o644)
    path.write_text(json.dumps(dict(reversed(document.items())), indent=1, sort_keys=False))
    assert isinstance(pb.verify(home), pb.Ok)


def test_a_setting_edit_is_tampered_with_the_diff(tmp_path: Path) -> None:
    home = tmp_path / "home"
    pb.write_lock(home, load_policy_template("builder"), approved_by="op")
    _edit(home, lambda d: d["settings"]["allowed_shell_commands"].append(["make"]))
    state = pb.verify(home)
    assert isinstance(state, pb.Tampered)
    (line,) = state.diff
    assert line.startswith("~ setting allowed_shell_commands:") and '["make"]]' in line
    assert pb.fail_closed_reason(state) == pb.BASELINE_TAMPERED
    _edit(home, lambda d: d["rules"].pop(0))
    assert any(
        line.startswith("- rule coding-workspace")
        for line in pb.verify(home).diff  # type: ignore[union-attr]
    )


def test_a_missing_lock_is_missing_and_fails_closed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    pb.write_lock(home, load_policy_template("builder"), approved_by="op")
    pb.lock_path(home).unlink()
    state = pb.verify(home)
    assert isinstance(state, pb.Missing)
    assert pb.fail_closed_reason(state) == pb.BASELINE_TAMPERED
    assert pb.baseline_status(home)["state"] == "tampered"


def test_the_lock_must_hash_and_match_the_ledger(tmp_path: Path) -> None:
    home = tmp_path / "home"
    builder = load_policy_template("builder")
    pb.write_lock(home, builder, approved_by="op")
    # Rewrite file AND lock to a wider layer, recomputing the lock's sha: the
    # ledger's last approval still names the old hash.
    wider = builder.model_copy(update={"settings": {**builder.settings, "default_network": ["*"]}})
    os.chmod(pb.baseline_path(home), 0o644)
    pb.baseline_path(home).write_text(json.dumps(wider.model_dump(mode="json")))
    lock = json.loads(pb.lock_path(home).read_text())
    lock.update(sha256=pb.sha256_of(wider), layer=wider.model_dump(mode="json"))
    pb.lock_path(home).write_text(json.dumps(lock))
    state = pb.verify(home)
    assert isinstance(state, pb.Tampered) and "approvals ledger" in state.diff[0]
    # A lock whose layer no longer hashes to its own sha is caught first.
    lock["sha256"] = "0" * 64
    pb.lock_path(home).write_text(json.dumps(lock))
    state = pb.verify(home)
    assert isinstance(state, pb.Tampered) and "was edited" in state.diff[0]


def test_deleting_both_files_is_not_a_way_back_to_the_zero_state(tmp_path: Path) -> None:
    home = tmp_path / "home"
    pb.write_lock(home, load_policy_template("assistant"), approved_by="op")
    pb.baseline_path(home).unlink()
    assert isinstance(pb.verify(home), pb.Tampered)
    pb.lock_path(home).unlink()
    assert isinstance(pb.verify(home), pb.Tampered)


def test_the_zero_state_is_explicit_and_answers_nothing_today(tmp_path: Path) -> None:
    state = pb.verify(tmp_path / "fresh")
    assert isinstance(state, pb.NoBaseline)
    assert pb.NO_BASELINE_REASON is None and pb.fail_closed_reason(state) is None
    assert pb.baseline_status(tmp_path / "fresh")["state"] == "none"


def test_a_schema_bump_asks_for_reapproval_instead_of_tripping(tmp_path: Path) -> None:
    home = tmp_path / "home"
    builder = load_policy_template("builder")
    # An older skep approved this exact content under schema 0.
    pb.write_lock(home, builder, approved_by="op", schema_version=0)
    state = pb.verify(home)
    assert isinstance(state, pb.Ok) and state.schema_from == 0
    assert pb.fail_closed_reason(state) is None
    assert pb.baseline_status(home)["state"] == "reapprove"
    row = pb.note_event(home, state)
    assert row is not None and row["reason"] == "schema_changed"
    assert pb.note_event(home, state) is None  # one card per event
    assert pb.tampered_since(home) is None  # a schema move is not tampering
    pb.write_lock(home, builder, approved_by="op")
    state = pb.verify(home)
    assert isinstance(state, pb.Ok) and state.schema_from is None


def test_tamper_events_are_recorded_once_and_reset_by_an_approval(tmp_path: Path) -> None:
    home = tmp_path / "home"
    builder = load_policy_template("builder")
    pb.write_lock(home, builder, approved_by="op")
    assert pb.note_event(home, pb.verify(home)) is None  # Ok: nothing to record
    _edit(home, lambda d: d["settings"].update(default_network=["*"]))
    first = pb.note_event(home, pb.verify(home))
    assert first is not None and first["reason"] == pb.BASELINE_TAMPERED
    assert pb.note_event(home, pb.verify(home)) is None
    assert pb.tampered_since(home) == first["at"]
    _edit(home, lambda d: d["settings"].update(default_network=["evil.example"]))
    assert pb.note_event(home, pb.verify(home)) is not None  # a new edit is a new event
    assert pb.tampered_since(home) == first["at"]  # "since" is the first of the run
    assert pb.status_line(home) == f"baseline: TAMPERED since {first['at']}"
    pb.write_lock(home, builder, approved_by="op")
    assert pb.tampered_since(home) is None
    assert pb.status_line(home).startswith("baseline: builder approved ")
