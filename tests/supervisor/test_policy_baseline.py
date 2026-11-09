"""first-run O2 (ADR 0056 D1/D2): the locked baseline — canonical form, lock,
approvals ledger, verify — and the tamper / zero-state behaviour end to end.
Always a temp home: never ``~/.skep``."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from skep.cli import main
from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor import policy_baseline as pb
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.policy_engine import resolve_stack
from skep.supervisor.policy_layers import layers_for_repo
from skep.supervisor.policy_resolver import resolve_operator_policy
from skep.supervisor.policy_schema import SCOPES
from skep.supervisor.policy_templates import explain_view, load_policy_template
from skep.supervisor.serve.actions import effective_policy_view
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import (
    MUTATING_TOOL_NAMES,
    execute_read_tool,
    mutation_execution_decision,
)

from .conftest import serve_client


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


# --- O2-2: the reader ---------------------------------------------------------


def _bound_store(config: SupervisorConfig, repo: Path, *, project_policy: dict[str, Any]) -> None:
    store = RunStore(config.db_path)
    try:
        store.set_setting("policy_engine_v2_scopes", list(SCOPES))
        store.set_setting("default_execution_mode", "workspace")
        store.set_setting("allowed_shell_commands", [["uv", "sync"]])
        store.add_project_policy(
            project_id="app",
            name="app",
            strategy="trusted_local_dev",
            phase="build",
            policy=project_policy,
        )
        store.add_project_binding(
            project_id="app", binding_kind="repo_path", binding_value=str(repo)
        )
    finally:
        store.close()


def test_the_baseline_is_the_enforced_template_layer(config: SupervisorConfig, repo: Path) -> None:
    _bound_store(config, repo, project_policy={"allowed_shell_commands": [["make"]]})
    builder = load_policy_template("builder")
    store = RunStore(config.db_path)
    try:
        before, _ = layers_for_repo(store, config, repo)
        assert "template" not in [layer.name for layer in before]
        pb.write_lock(config.home, builder, approved_by="op")
        layers, _ = layers_for_repo(store, config, repo)
        assert [layer.name for layer in layers][:3] == ["global", "template", "phase"]
        knob = resolve_stack(layers).knobs["allowed_shell_commands"]
        # The project overlay may no longer loosen (or change) what was approved.
        assert knob.value == builder.settings["allowed_shell_commands"]
        assert knob.enforced_by == "template:builder"
        refused = [s for s in knob.chain if s.outcome == "refused"]
        assert [s.layer for s in refused] == ["project"]
        assert refused[0].reason == "enforced at template:builder"
        # unset passes through: a knob the baseline does not set is untouched.
        mode = resolve_stack(layers).knobs["default_execution_mode"]
        assert mode.value == "workspace" and mode.enforced_by is None
        # The rules ride the same slot: the Queen-side stack answers from them.
        operator = resolve_operator_policy(store)
        assert operator.decision("shell", "run", "uv run pytest -q").rule_id == (
            "shell-uv-run-pytest"
        )
        assert operator.fail_closed is None
    finally:
        store.close()


def test_a_tampered_baseline_fails_the_queen_and_dispatch_closed(
    config: SupervisorConfig, repo: Path
) -> None:
    _bound_store(config, repo, project_policy={})
    pb.write_lock(config.home, load_policy_template("builder"), approved_by="op")
    _edit(config.home, lambda d: d["settings"].update(default_network=["*"]))
    store = RunStore(config.db_path)
    try:
        # No layer from a baseline that does not verify — and nothing looser either.
        layers, _ = layers_for_repo(store, config, repo)
        assert "template" not in [layer.name for layer in layers]
        decision = resolve_operator_policy(store).decision("network", "search", "ddgs")
        assert decision.verdict == "require_approval"
        assert decision.rule_id == pb.BASELINE_TAMPERED
        view = effective_policy_view(ConfigHolder(config, store), store, str(repo))
        assert view["error"].startswith("baseline_tampered:")
        assert "skep policy approve --reapprove" in view["error"]
    finally:
        store.close()


def test_the_store_finds_its_home() -> None:
    config = SupervisorConfig(home=Path("/x/home"), worker_command=("w",))
    assert config.db_path.parent == config.home  # store_home relies on it


# --- O2-3: approve and baseline status|diff on three faces --------------------


def test_cli_approve_shows_the_table_takes_a_typed_yes_and_locks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    approve = ["--home", str(tmp_path), "policy", "approve"]
    home = build_config(tmp_path, None).home  # the supervisor home under --home
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    assert main([*approve, "--template", "builder"]) != 0
    shown = capsys.readouterr()
    # What is about to be locked: the builder rows, under a baseline header.
    assert shown.out.startswith("policy baseline: builder\n")
    assert explain_view("builder")["table"].split("\n", 1)[1] in shown.out
    assert "nothing was written" in shown.err and not pb.baseline_path(home).exists()
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    assert main([*approve, "--template", "builder"]) == 0
    assert "baseline locked: builder sha256 " in capsys.readouterr().out
    assert isinstance(pb.verify(home), pb.Ok)
    assert [r["template"] for r in _approvals(home)] == ["builder"]
    # A second approval must say it re-locks, and shows the diff first.
    assert main([*approve, "--template", "assistant", "--yes"]) != 0
    assert "--reapprove" in capsys.readouterr().err
    assert main([*approve, "--template", "assistant", "--reapprove", "--yes"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("changes against the locked baseline:\n")
    assert "~ template: 'builder' -> 'assistant'" in out
    with pytest.raises(SystemExit):  # one source: argparse refuses both
        main([*approve, "--template", "builder", "--file", "x.json"])
    capsys.readouterr()
    assert main(["--home", str(tmp_path / "fresh"), "policy", "approve", "--yes"]) != 0
    assert "name a --template" in capsys.readouterr().err


def test_cli_approve_from_a_file_refuses_a_floor_grant(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = build_config(tmp_path, None).home
    layer = load_policy_template("builder").model_dump(mode="json")
    layer["rules"].append(
        {"scope": "shell", "verdict": "allow", "action": "run", "pattern": "git push",
         "rule_id": "push"}
    )  # fmt: skip
    path = tmp_path / "wide.json"
    path.write_text(json.dumps(layer))
    assert main(["--home", str(tmp_path), "policy", "approve", "--file", str(path), "--yes"]) != 0
    assert "push" in capsys.readouterr().err and not pb.baseline_path(home).exists()
    layer["rules"].pop()
    path.write_text(json.dumps(layer))
    assert main(["--home", str(tmp_path), "policy", "approve", "--file", str(path), "--yes"]) == 0


def test_cli_baseline_status_diff_and_explain_baseline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = ["--home", str(tmp_path), "policy"]
    home = build_config(tmp_path, None).home
    assert main([*base, "baseline", "status"]) == 0
    assert capsys.readouterr().out == "baseline: none approved\n"
    assert main([*base, "approve", "--template", "builder", "--yes"]) == 0
    capsys.readouterr()
    assert main([*base, "explain", "baseline"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("policy baseline: builder\nbaseline: builder approved ")
    # The locked layer's rows are the builder template's rows.
    assert explain_view("builder")["table"].split("\n", 1)[1] in out
    assert main([*base, "baseline", "diff"]) == 0
    assert capsys.readouterr().out == "no change against the locked baseline\n"
    _edit(home, lambda d: d["settings"].update(default_network=["*"]))
    assert main([*base, "baseline"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("baseline: TAMPERED since ")
    assert '  ~ setting default_network: ["pypi.org"' in out
    assert main([*base, "baseline", "diff"]) == 0
    assert capsys.readouterr().out.startswith("~ setting default_network:")
    assert main([*base, "explain", "baseline"]) == 0
    assert "baseline: TAMPERED since" in capsys.readouterr().out


def test_rest_reads_and_approves_the_baseline(config: SupervisorConfig) -> None:
    client = serve_client(config)
    assert client.get("/api/policy/baseline").json()["state"] == "none"
    bad = client.post("/api/policy/baseline/approve", json={"template": "warp"})
    assert bad.status_code == 400 and "no policy template" in bad.json()["detail"]
    ok = client.post("/api/policy/baseline/approve", json={"template": "operator"})
    assert ok.status_code == 200 and ok.json()["approved"]["approved_by"] == "api"
    view = client.get("/api/policy/baseline").json()
    assert view["state"] == "ok" and view["template"] == "operator"
    assert view["line"].startswith("baseline: operator approved ")
    explained = client.get("/api/policy/explain", params={"subject": "baseline"}).json()
    assert explained["name"] == "operator" and explained["status"]["state"] == "ok"
    again = client.post("/api/policy/baseline/approve", json={"template": "builder"})
    assert again.status_code == 400 and "--reapprove" in again.json()["detail"]


def test_chat_reads_the_baseline_and_the_approval_always_cards(config: SupervisorConfig) -> None:
    assert "approve_policy_baseline" in MUTATING_TOOL_NAMES
    store = RunStore(config.db_path)
    holder = ConfigHolder(config, store)
    try:
        args = {"template": "builder"}
        assert mutation_execution_decision("approve_policy_baseline", args, store=store,
                                           holder=holder) is None  # fmt: skip
        pb.write_lock(config.home, load_policy_template("assistant"), approved_by="op")
        view = execute_read_tool(
            "policy_explain", {"subject": "baseline"}, store=store, holder=holder
        )
    finally:
        store.close()
    assert view["name"] == "assistant" and view["status"]["state"] == "ok"
