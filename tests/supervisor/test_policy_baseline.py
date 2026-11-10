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
from skep.supervisor.policy_golden import freeze, write_golden
from skep.supervisor.policy_layers import layers_for_repo
from skep.supervisor.policy_resolver import resolve_operator_policy
from skep.supervisor.policy_schema import SCOPES
from skep.supervisor.policy_templates import explain_view, load_policy_template
from skep.supervisor.serve.actions import effective_policy_view, policy_decide
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import (
    MUTATING_TOOL_NAMES,
    execute_read_tool,
    mutation_execution_decision,
)

from .conftest import serve_client

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "policy_golden"


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


def test_the_zero_state_is_explicit_and_names_no_baseline(tmp_path: Path) -> None:
    state = pb.verify(tmp_path / "fresh")
    assert isinstance(state, pb.NoBaseline)
    assert pb.fail_closed_reason(state) == pb.NO_BASELINE_REASON == "no_baseline"
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
        # The rules ride the same slot: the Queen-side stack answers from them —
        # all but the worker shell rules, which never reach the host lane (ES-F3).
        operator = resolve_operator_policy(store)
        assert operator.decision("network", "fetch", "example.com").rule_id == "net-fetch-gated"
        assert operator.decision("shell", "run", "uv run pytest -q").rule_id == "default-deny"
        assert operator.fail_closed == {}
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
    os.chmod(pb.baseline_path(home), 0o644)
    pb.baseline_path(home).write_text("{not json")
    assert main([*approve, "--reapprove", "--yes"]) != 0
    assert "baseline.json no longer loads" in capsys.readouterr().err
    assert main([*approve, "--reapprove", "--template", "builder", "--yes"]) == 0


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


# --- O2-4: tamper and zero state, end to end ----------------------------------


def _tamper(home: Path) -> None:
    _edit(home, lambda d: d["settings"]["allowed_shell_commands"].append(["make"]))


def _decide(config: SupervisorConfig, **kwargs: Any) -> dict[str, Any]:
    store = RunStore(config.db_path)
    try:
        return policy_decide(ConfigHolder(config, store), store, **kwargs)
    finally:
        store.close()


def test_done_criterion_approve_edit_decide_tampered_until_reapprove(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0056 §1 done-criterion 2, on a temp home: approve, hand-edit, and
    the very next decide() answers baseline_tampered until a re-approval."""
    config = build_config(tmp_path, None)
    _bound_store(config, repo, project_policy={})
    approve = ["--home", str(tmp_path), "policy", "approve"]
    assert main([*approve, "--template", "builder", "--yes"]) == 0
    assert not capsys.readouterr().err
    ok = _decide(config, scope="shell", action="run", value="uv sync", repo=str(repo))
    assert ok["verdict"] == "allow" and ok["source"] == "run"
    _tamper(config.home)
    tampered = _decide(config, scope="shell", action="run", value="uv sync", repo=str(repo))
    assert (tampered["verdict"], tampered["rule_id"], tampered["source"]) == (
        "require_approval",
        "baseline_tampered",
        "baseline",
    )
    assert tampered["note"].startswith("baseline: TAMPERED since ")
    assert main(["--home", str(tmp_path), "policy", "approve", "--reapprove", "--yes"]) == 0
    shown = capsys.readouterr().out
    assert shown.startswith("changes against the locked baseline:\n")
    assert "~ setting allowed_shell_commands:" in shown  # the diff, before the re-lock
    back = _decide(config, scope="shell", action="run", value="uv sync", repo=str(repo))
    assert back["verdict"] == "allow" and back["source"] == "run"
    ledger = _approvals(config.home)
    assert [r["kind"] for r in ledger] == [
        "baseline_approved",
        "baseline_tampered",
        "baseline_approved",
    ]


def test_policy_decide_fails_closed_on_every_lane_and_names_the_ring(
    config: SupervisorConfig, repo: Path
) -> None:
    _bound_store(config, repo, project_policy={})
    pb.write_lock(config.home, load_policy_template("builder"), approved_by="op")
    pb.lock_path(config.home).unlink()  # Missing is treated as tampered
    in_repo = _decide(config, scope="filesystem", action="write", value="workspace", repo=str(repo))
    queen = _decide(config, scope="network", action="search", value="ddgs")
    hard = _decide(config, scope="shell", action="run", value="ls", repo=str(repo), ring="hard")
    floor = _decide(config, scope="shell", action="run", value="git push", repo=str(repo))
    assert in_repo["verdict"] == queen["verdict"] == "require_approval"
    assert in_repo["rule_id"] == queen["rule_id"] == hard["rule_id"] == "baseline_tampered"
    assert hard["verdict"] == "deny" and hard["decided_by"] == "baseline/baseline_tampered"
    # The floor is above the baseline, tampered or not (I4): its own deny.
    assert floor["source"] == "floor" and floor["rule_id"] == "floor.worker-git-hard-denies"


def test_skepd_verifies_at_startup_and_cards_once_per_tamper_event(
    config: SupervisorConfig, repo: Path
) -> None:
    _bound_store(config, repo, project_policy={})
    pb.write_lock(config.home, load_policy_template("builder"), approved_by="op")
    _tamper(config.home)
    decide = {"scope": "shell", "action": "run", "value": "uv sync", "repo": str(repo)}
    with serve_client(config, start_ticker=False) as client:
        store = RunStore(config.db_path)
        try:
            (chat,) = [c for c in store.list_chats() if c.source == "policy"]
            (card,) = store.pending_chat_actions(chat.chat_id)
        finally:
            store.close()
        assert card.tool == "approve_policy_baseline" and card.args["reapprove"] is True
        assert any("allowed_shell_commands" in line for line in card.args["diff"])
        for _ in range(2):
            answer = client.post("/api/policy/decide", json=decide).json()
            assert answer["rule_id"] == "baseline_tampered"
        why = client.get("/api/policy/why", params={**decide}).json()
        assert why["baseline"].startswith("baseline: TAMPERED since ")
        store = RunStore(config.db_path)
        try:
            assert len(store.pending_chat_actions(chat.chat_id)) == 1  # debounced
        finally:
            store.close()
        confirmed = client.post(f"/api/chats/{chat.chat_id}/commands/{card.action_id}/confirm")
        assert confirmed.json()["ok"] is True, confirmed.json()
        assert client.get("/api/policy/baseline").json()["state"] == "ok"
        assert client.post("/api/policy/decide", json=decide).json()["verdict"] == "allow"


def test_the_floor_wins_over_an_approved_baseline_that_reaches_for_it(
    config: SupervisorConfig,
) -> None:
    store = RunStore(config.db_path)
    try:
        store.set_setting("policy_engine_v2_scopes", list(SCOPES))
    finally:
        store.close()
    wide = load_policy_template("builder").model_dump(mode="json")
    wide["rules"] = [r for r in wide["rules"] if r["rule_id"] != "shell-learnable"]
    wide["rules"].append(
        {"scope": "shell", "verdict": "allow", "action": "run", "pattern": "*",
         "rule_id": "shell-everything"}
    )  # fmt: skip
    pb.write_lock(config.home, pb._load_layer(wide), approved_by="op")
    # ES-F3: a baseline's shell rules bind workers, not the Queen's host lane.
    assert _decide(config, scope="shell", action="run", value="make")["verdict"] == "deny"
    for command, key in (
        ("git push origin main", "floor.worker-git-hard-denies"),
        ("sudo make install", "floor.never-grantable-prefixes"),
    ):
        record = _decide(config, scope="shell", action="run", value=command)
        assert (record["verdict"], record["source"], record["rule_id"]) == ("deny", "floor", key)


def test_golden_parity_is_byte_identical_with_no_baseline(tmp_path: Path) -> None:
    seed = json.loads((GOLDEN / "seed.json").read_text())
    golden = freeze(seed, tmp_path / "home")
    assert isinstance(pb.verify(tmp_path / "home" / "supervisor"), pb.NoBaseline)
    for path in write_golden(golden, tmp_path / "out"):
        assert path.read_bytes() == (GOLDEN / path.name).read_bytes(), path.name


def test_a_schema_bump_cards_a_reapproval_and_keeps_deciding(
    config: SupervisorConfig, repo: Path
) -> None:
    _bound_store(config, repo, project_policy={})
    pb.write_lock(config.home, load_policy_template("builder"), approved_by="op", schema_version=0)
    decide = {"scope": "shell", "action": "run", "value": "uv run pytest", "repo": str(repo)}
    with serve_client(config, start_ticker=False) as client:
        answer = client.post("/api/policy/decide", json=decide).json()
        assert answer["verdict"] == "allow" and answer["source"] == "run"
        store = RunStore(config.db_path)
        try:
            (chat,) = [c for c in store.list_chats() if c.source == "policy"]
            (card,) = store.pending_chat_actions(chat.chat_id)
        finally:
            store.close()
        assert card.args["reason"] == "schema_changed"
        assert client.get("/api/policy/baseline").json()["state"] == "reapprove"
        client.post(f"/api/chats/{chat.chat_id}/commands/{card.action_id}/confirm")
        view = client.get("/api/policy/baseline").json()
        assert view["state"] == "ok" and view["schema_version"] == 1


# --- T1: tamper variants the O2 tests left open --------------------------------


def test_an_unreadable_lock_fails_closed_and_explain_still_answers(tmp_path: Path) -> None:
    home = tmp_path / "home"
    pb.write_lock(home, load_policy_template("builder"), approved_by="op")
    pb.lock_path(home).write_text("{torn")
    state = pb.verify(home)
    assert isinstance(state, pb.Tampered) and state.diff[0].startswith(
        "baseline.lock is unreadable"
    )
    assert pb.fail_closed_reason(state) == pb.BASELINE_TAMPERED
    # The read faces degrade to "no locked layer", not a crash.
    view = explain_view("baseline", home)
    assert view["name"] is None and view["status"]["state"] == "tampered"


def test_deleting_the_ledger_is_tampering(tmp_path: Path) -> None:
    """File and lock intact, the approvals ledger gone: the lock no longer
    matches an approval, so nothing verifies (D1: the ledger is the record)."""
    home = tmp_path / "home"
    pb.write_lock(home, load_policy_template("builder"), approved_by="op")
    pb.ledger_path(home).unlink()
    state = pb.verify(home)
    assert isinstance(state, pb.Tampered) and "approvals ledger" in state.diff[0]


def test_a_torn_ledger_line_is_not_tampering(tmp_path: Path) -> None:
    """A half-written line (a crash mid-append) is skipped, not fatal — the
    last whole approval row still has to match the lock."""
    home = tmp_path / "home"
    pb.write_lock(home, load_policy_template("builder"), approved_by="op")
    with pb.ledger_path(home).open("a") as handle:
        handle.write('{"kind": "baseline_tamp\n[1, 2]\n')
    assert isinstance(pb.verify(home), pb.Ok)


def test_rule_order_is_content_and_the_diff_names_each_kind_of_change(tmp_path: Path) -> None:
    """Reordering rules changes the hash (first match decides), so it is
    tampering with its own diff line; added, removed and changed settings and
    rules each get theirs."""
    home = tmp_path / "home"
    pb.write_lock(home, load_policy_template("builder"), approved_by="op")
    _edit(home, lambda d: d["rules"].reverse())
    assert pb.verify(home).diff == ("~ rule order changed",)  # type: ignore[union-attr]

    def widen(d: dict[str, Any]) -> None:  # on top of the reversed file
        del d["settings"]["default_network"]
        d["rules"][0]["verdict"] = "allow"  # builder's last rule, an ask

    _edit(home, widen)
    diff = pb.verify(home).diff  # type: ignore[union-attr]
    builder = load_policy_template("builder")
    last = builder.rules[-1]
    assert last.verdict == "require_approval"
    assert diff == (
        f"- setting default_network: {json.dumps(builder.settings['default_network'])}",
        f"~ rule {last.rule_id}: {last.scope}: require_approval {last.action} {last.pattern}"
        f" -> {last.scope}: allow {last.action} {last.pattern}",
    )
    # And the other way round: a knob the approved layer did not set.
    bare = builder.model_copy(update={"settings": {}})
    assert pb.layer_diff(bare, builder)[0].startswith("+ setting allowed_shell_commands: [")


def test_deleting_both_files_is_one_tamper_event(tmp_path: Path) -> None:
    home = tmp_path / "home"
    pb.write_lock(home, load_policy_template("builder"), approved_by="op")
    pb.baseline_path(home).unlink()
    pb.lock_path(home).unlink()
    row = pb.note_event(home, pb.verify(home))
    assert row is not None and row["key"] == "baseline.json absent"
    assert pb.note_event(home, pb.verify(home)) is None  # debounced
    assert pb.status_line(home) == f"baseline: TAMPERED since {row['at']}"


def test_a_reapproval_relocks_the_file_read_only(tmp_path: Path) -> None:
    home = tmp_path / "home"
    pb.write_lock(home, load_policy_template("builder"), approved_by="op")
    _edit(home, lambda d: d["settings"].update(default_network=["*"]))
    assert stat.S_IMODE(pb.baseline_path(home).stat().st_mode) == 0o644
    pb.write_lock(home, load_policy_template("operator"), approved_by="op")
    assert stat.S_IMODE(pb.baseline_path(home).stat().st_mode) == 0o444
    state = pb.verify(home)
    assert isinstance(state, pb.Ok) and state.layer.template == "operator"


def test_the_rest_face_of_approve_refuses_what_argparse_cannot_see(tmp_path: Path) -> None:
    """T3-3: argparse keeps --template and --file apart on the CLI; REST and
    chat reach baseline_preview directly, so it refuses both itself, an
    unreadable file, and a bare --reapprove with no baseline.json left."""
    from skep.supervisor.serve.actions import baseline_preview

    home = build_config(tmp_path, None).home
    with pytest.raises(ValueError, match="not both"):
        baseline_preview(home, template="builder", file="x.json")
    with pytest.raises(ValueError, match=r"cannot read .*missing\.json"):
        baseline_preview(home, file=str(tmp_path / "missing.json"))
    pb.write_lock(home, load_policy_template("builder"), approved_by="op")
    os.chmod(pb.baseline_path(home), 0o644)
    pb.baseline_path(home).unlink()
    with pytest.raises(ValueError, match=r"no baseline\.json to re-approve"):
        baseline_preview(home, reapprove=True)
