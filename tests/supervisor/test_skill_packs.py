"""v85-F3: the pack ladder — script-shipping SKILL.md packs walk the v17
lifecycle. The pins: no path writes a scripted template to the registry
without a passing trial + a human action; the trial parses and never
executes; suspend keeps registered ⟺ active; rolled_back is terminal."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from skep.supervisor.config import SupervisorConfig
from skep.supervisor.skill_packs import (
    PACK_PROVENANCE,
    SkillPackError,
    draft_pack,
    installed_packs_root,
    load_packs,
    promote_pack,
    suspend_pack,
)
from skep.supervisor.store import RunStore


def _pack_dir(tmp_path: Path, *, py_body: str = "print('hi')\n") -> Path:
    directory = tmp_path / "homelab-control"
    directory.mkdir(exist_ok=True)
    (directory / "SKILL.md").write_text(
        "---\nname: homelab-control\ndescription: homelab queries\n---\n"
        "# Homelab control\n\nRun `python scripts/query.py`.\n"
    )
    scripts = directory / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "query.py").write_text(py_body)
    (scripts / "helper.sh").write_text("#!/bin/sh\necho ok\n")
    return directory


def _config(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(home=tmp_path / "home" / "supervisor")


def _real_config(tmp_path: Path) -> SupervisorConfig:
    """v100-F5: the self-test path runs the RESOLVER, so it needs a real config
    (a SimpleNamespace has no worker_command / sandbox knobs to resolve from)."""
    from skep.supervisor.cli_cmds import build_config

    return build_config(tmp_path / "home", None)


def test_ladder_happy_path_lands_template_and_snapshot(tmp_path: Path) -> None:
    directory = _pack_dir(tmp_path)
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        record = draft_pack(
            store, directory, grants=("python scripts/query.py",)
        )
        assert record.state == "draft"
        assert record.scripts == ("scripts/helper.sh", "scripts/query.py")
        # Draft is inert: nothing in the registry yet.
        assert store.get_template("homelab-control") is None

        config = _config(tmp_path)
        promoted, template = promote_pack(
            store, config, "homelab-control", human_action=True
        )
        assert promoted.state == "active"
        assert template is not None
        assert template.provenance == PACK_PROVENANCE
        # v85-F4: shipped-script tokens are rewritten onto the workspace
        # materialization path, so grant and file agree by construction.
        assert template.shell_allowlist == (
            ("python", ".skep-skill/homelab-control/scripts/query.py"),
        )
        assert ".skep-skill/homelab-control/" in template.instructions
        assert promoted.trial is not None and promoted.trial["ok"] is True
        stored = store.get_template("homelab-control")
        assert stored is not None and stored.provenance == PACK_PROVENANCE
        # Activation snapshots the pack beside the store.
        snapshot = installed_packs_root(config) / "homelab-control"
        assert (snapshot / "SKILL.md").is_file()
        assert (snapshot / "scripts" / "query.py").is_file()

        # Promoting an active pack is a no-op, not an error.
        again, none_template = promote_pack(
            store, config, "homelab-control", human_action=True
        )
        assert again.state == "active" and none_template is None
    finally:
        store.close()


def test_failing_trial_stays_sandboxed_and_writes_nothing(tmp_path: Path) -> None:
    directory = _pack_dir(tmp_path, py_body="def broken(:\n")
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        draft_pack(store, directory, grants=("python scripts/query.py",))
        with pytest.raises(SkillPackError, match="stays 'sandboxed'"):
            promote_pack(store, _config(tmp_path), "homelab-control", human_action=True)
        record = load_packs(store)["homelab-control"]
        assert record.state == "sandboxed"
        assert record.trial is not None and record.trial["ok"] is False
        assert store.get_template("homelab-control") is None  # no registry write

        # Fixing the script at the source lets a re-promote pass.
        (directory / "scripts" / "query.py").write_text("print('fixed')\n")
        promoted, template = promote_pack(
            store, _config(tmp_path), "homelab-control", human_action=True
        )
        assert promoted.state == "active" and template is not None
    finally:
        store.close()


def test_no_human_action_no_activation(tmp_path: Path) -> None:
    from skep.supervisor.plugin_lifecycle import PluginLifecycleError

    directory = _pack_dir(tmp_path)
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        draft_pack(store, directory)
        with pytest.raises(PluginLifecycleError, match="requires a human action"):
            promote_pack(store, _config(tmp_path), "homelab-control", human_action=False)
        assert store.get_template("homelab-control") is None
    finally:
        store.close()


def test_draft_refusals_and_idempotence(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        scriptless = tmp_path / "plain"
        scriptless.mkdir()
        (scriptless / "SKILL.md").write_text("# Plain\n\nJust text.\n")
        with pytest.raises(SkillPackError, match="ships no scripts"):
            draft_pack(store, scriptless)

        directory = _pack_dir(tmp_path)
        first = draft_pack(store, directory, grants=("python scripts/query.py",))
        second = draft_pack(store, directory)  # existing record wins, grants kept
        assert second == first

        with pytest.raises(SkillPackError, match="no skill pack"):
            promote_pack(store, _config(tmp_path), "nope", human_action=True)
    finally:
        store.close()


def test_suspend_removes_template_and_rollback_is_terminal(tmp_path: Path) -> None:
    directory = _pack_dir(tmp_path)
    store = RunStore(tmp_path / "s.sqlite3")
    config = _config(tmp_path)
    try:
        draft_pack(store, directory, grants=("python scripts/query.py",))
        promote_pack(store, config, "homelab-control", human_action=True)
        assert store.get_template("homelab-control") is not None

        suspended = suspend_pack(store, "homelab-control")
        assert suspended.state == "suspended"
        assert store.get_template("homelab-control") is None  # registered ⟺ active

        # Reactivation returns the template without a re-trial.
        reactivated, template = promote_pack(
            store, config, "homelab-control", human_action=True
        )
        assert reactivated.state == "active" and template is not None
        assert store.get_template("homelab-control") is not None

        retired = suspend_pack(store, "homelab-control", rollback=True)
        assert retired.state == "rolled_back"
        assert store.get_template("homelab-control") is None
        with pytest.raises(SkillPackError, match="terminal"):
            promote_pack(store, config, "homelab-control", human_action=True)
    finally:
        store.close()


def test_operator_template_collision_refuses_activation(tmp_path: Path) -> None:
    from skep.supervisor.templates import WorkflowTemplate

    directory = _pack_dir(tmp_path)
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        store.add_template(
            WorkflowTemplate(
                name="homelab-control",
                instructions="the operator's own recipe",
                provenance="user",
            )
        )
        draft_pack(store, directory)
        with pytest.raises(SkillPackError, match="operator's copy wins"):
            promote_pack(store, _config(tmp_path), "homelab-control", human_action=True)
        kept = store.get_template("homelab-control")
        assert kept is not None and kept.provenance == "user"
    finally:
        store.close()


def test_materialize_packs_for_run_copies_only_referenced_active_packs(
    tmp_path: Path,
) -> None:
    """v85-F4: the dispatch hook — grants referencing .skep-skill/<id>/ get the
    snapshot copied into the workspace; anything else copies nothing."""
    from skep.supervisor.skill_packs import materialize_packs_for_run

    directory = _pack_dir(tmp_path)
    store = RunStore(tmp_path / "s.sqlite3")
    config = _config(tmp_path)
    try:
        draft_pack(store, directory, grants=("python scripts/query.py",))
        _, template = promote_pack(store, config, "homelab-control", human_action=True)
        assert template is not None

        workspace = tmp_path / "worktree"
        workspace.mkdir()
        copied = materialize_packs_for_run(
            store, config, workspace, [list(c) for c in template.shell_allowlist]
        )
        assert copied == ["homelab-control"]
        materialized = workspace / ".skep-skill" / "homelab-control"
        assert (materialized / "scripts" / "query.py").is_file()
        assert (materialized / "SKILL.md").is_file()

        # A non-pack allowlist copies nothing.
        plain = tmp_path / "worktree2"
        plain.mkdir()
        assert materialize_packs_for_run(
            store, config, plain, [["uv", "run", "pytest"]]
        ) == []
        assert not (plain / ".skep-skill").exists()

        # A suspended pack is never materialized — registered ⟺ active.
        suspend_pack(store, "homelab-control")
        again = tmp_path / "worktree3"
        again.mkdir()
        assert materialize_packs_for_run(
            store, config, again, [list(c) for c in template.shell_allowlist]
        ) == []
    finally:
        store.close()


def test_chat_surface_promote_suspend_and_listing(tmp_path: Path) -> None:
    """v85-F5: the ladder drivable from chat — the confirmed card is the
    human action; list_skills shows the walk."""
    from skep.supervisor.serve.tools import execute_mutation, execute_read_tool

    directory = _pack_dir(tmp_path)
    store = RunStore(tmp_path / "s.sqlite3")
    config = _config(tmp_path)
    holder = SimpleNamespace(current=config)
    try:
        draft_pack(store, directory, grants=("python scripts/query.py",))
        listing = execute_read_tool(
            "list_skills", {}, store=store, holder=holder  # type: ignore[arg-type]
        )
        assert listing["packs"][0]["pack_id"] == "homelab-control"
        assert listing["packs"][0]["state"] == "draft"

        result = execute_mutation(
            "promote_skill_pack",
            {"pack_id": "homelab-control"},
            store=store,
            holder=holder,  # type: ignore[arg-type]
            runner=None,  # type: ignore[arg-type]
            actor="tester",
        )
        assert result["state"] == "active"
        assert result["trial"]["ok"] is True
        assert store.get_template("homelab-control") is not None

        result = execute_mutation(
            "suspend_skill_pack",
            {"pack_id": "homelab-control"},
            store=store,
            holder=holder,  # type: ignore[arg-type]
            runner=None,  # type: ignore[arg-type]
            actor="tester",
        )
        assert result["state"] == "suspended"
        assert store.get_template("homelab-control") is None

        with pytest.raises(ValueError, match="suspend pauses an ACTIVE pack"):
            execute_mutation(
                "suspend_skill_pack",
                {"pack_id": "homelab-control"},
                store=store,
                holder=holder,  # type: ignore[arg-type]
                runner=None,  # type: ignore[arg-type]
                actor="tester",
            )
    finally:
        store.close()


# ---------- v100-F5 (R13): the trial runs the pack's own check ----------


def _self_test_pack(tmp_path: Path, *, command: str, exit_code: int = 0) -> Path:
    directory = tmp_path / "checked-pack"
    directory.mkdir(exist_ok=True)
    (directory / "SKILL.md").write_text(
        "---\nname: checked-pack\ndescription: a pack that proves itself\n"
        f"self_test: {command}\n---\n"
        "# Checked pack\n\nRun `python scripts/check.py`.\n"
    )
    scripts = directory / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "check.py").write_text(
        "import os, sys\n"
        "print('cwd', os.getcwd())\n"
        "print('the check ran')\n"
        "sys.stderr.write('why it failed\\n')\n"
        f"raise SystemExit({exit_code})\n"
    )
    return directory


def test_self_test_parses_off_the_frontmatter_and_reaches_the_record(tmp_path: Path) -> None:
    """v100-F5: ADR 0045 shipped an honesty gap — a pack whose scripts PARSE was
    promoted to 'tested'. The declaration is step one of closing it."""
    from skep.supervisor.skill_md import parse_skill_md

    directory = _self_test_pack(tmp_path, command="python scripts/check.py")
    assert parse_skill_md(directory).self_test == "python scripts/check.py"
    # No declaration is legal, and is the whole existing shelf's case.
    assert parse_skill_md(_pack_dir(tmp_path)).self_test == ""

    store = RunStore(tmp_path / "s.sqlite3")
    try:
        record = draft_pack(store, directory)
        assert record.self_test == "python scripts/check.py"
        assert load_packs(store)["checked-pack"].self_test == "python scripts/check.py"
    finally:
        store.close()


@pytest.mark.parametrize("exit_code", [0, 1])
def test_the_self_test_harness_runs_the_check_for_real(tmp_path: Path, exit_code: int) -> None:
    """Executed for real with no dispatch pipeline (the test_seed_tools
    precedent): the harness extracts the pack at the SAME path activation
    grants and materialize_packs_for_run use, runs the declared command there,
    and prints the forge's evidence line for trial_verdict to read (I2)."""
    import json as _json
    import subprocess
    import sys

    from skep.supervisor.skill_packs import (
        WORKSPACE_PACK_DIR,
        self_test_script,
    )

    directory = _self_test_pack(tmp_path, command="python scripts/check.py", exit_code=exit_code)
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        record = draft_pack(store, directory)
    finally:
        store.close()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    proc = subprocess.run(
        [sys.executable, "-c", self_test_script(record)],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("FORGE_TRIAL "))
    evidence = _json.loads(line[len("FORGE_TRIAL ") :])

    extracted = workspace / WORKSPACE_PACK_DIR / "checked-pack"
    assert (extracted / "scripts" / "check.py").is_file()  # the real run's layout
    if exit_code == 0:
        assert evidence["ok"] is True
        assert "the check ran" in evidence["self_test"]
    else:
        assert evidence["ok"] is False
        assert "self_test FAILED (exit 1)" in evidence["error"]
        assert "why it failed" in evidence["error"]  # the stderr tail


def _stub_run_task(monkeypatch: pytest.MonkeyPatch, *, ok: bool) -> dict[str, object]:
    """Monkeypatch the dispatch seam (the _forge_trial precedent) and capture
    the three properties the trial's MEANING rests on."""
    import skep.supervisor.dispatch as dispatch_module

    seen: dict[str, object] = {}
    marker = (
        '{"ok": true, "self_test": "the check ran"}'
        if ok
        else '{"ok": false, "error": "self_test FAILED (exit 1): why it failed"}'
    )

    event = SimpleNamespace(
        type=SimpleNamespace(value="command.result"),
        payload={"stdout": f"FORGE_TRIAL {marker}\n"},
    )

    def fake_run_task(repo: Path, instructions: str, **kwargs: object) -> object:
        seen["repo"] = str(repo)
        seen["instructions"] = instructions
        seen.update(kwargs)
        record = SimpleNamespace(task_id="trial-task", state="completed", summary="")
        return SimpleNamespace(record=record, review_id=None)

    monkeypatch.setattr(dispatch_module, "run_task", fake_run_task)
    monkeypatch.setattr(RunStore, "events_for", lambda self, task_id: [event])
    return seen


def test_promotion_dispatches_the_self_test_on_the_script_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sandbox, script caste, EMPTY network — a self-test that could reach the
    network is not a trial. Deny-all egress is the forge contract's own rule,
    so a pack needing network must declare an offline check."""
    seen = _stub_run_task(monkeypatch, ok=True)
    directory = _self_test_pack(tmp_path, command="python scripts/check.py")
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        draft_pack(store, directory)
        record, template = promote_pack(
            store, _real_config(tmp_path), "checked-pack", human_action=True
        )
    finally:
        store.close()

    assert seen["worker_kind"] == "script"
    assert seen["execution_mode"] == "sandbox"
    # The envelope came from the RESOLVER, not from a second permission path
    # built here (I5) — the request said network=[] and that is what landed.
    assert list(seen["permissions"].network) == []  # type: ignore[attr-defined]
    assert list(seen["permissions"].write) == ["workspace"]  # type: ignore[attr-defined]
    assert record.state == "active"
    assert template is not None
    assert record.trial is not None
    assert record.trial["level"] == "self_test"
    assert record.trial["command"] == "python scripts/check.py"


def test_a_failing_self_test_stays_sandboxed_and_registers_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same rule a failing syntax smoke already had: the pack does not advance,
    and the failure text is the pack's own, not a generic one."""
    _stub_run_task(monkeypatch, ok=False)
    directory = _self_test_pack(tmp_path, command="python scripts/check.py", exit_code=1)
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        draft_pack(store, directory)
        with pytest.raises(SkillPackError) as excinfo:
            promote_pack(store, _real_config(tmp_path), "checked-pack", human_action=True)
        assert "self_test FAILED" in str(excinfo.value)
        assert "why it failed" in str(excinfo.value)

        stored = load_packs(store)["checked-pack"]
        assert stored.state == "sandboxed"
        assert stored.trial is not None
        assert stored.trial["level"] == "self_test"
        assert stored.trial["ok"] is False
        assert store.get_template("checked-pack") is None
    finally:
        store.close()


def test_a_pack_without_a_self_test_promotes_and_says_syntax_only(tmp_path: Path) -> None:
    """The promotion never claims behaviour it did not test (I8) — R13's gap is
    closed either by running the check or by saying plainly that none ran."""
    directory = _pack_dir(tmp_path)
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        draft_pack(store, directory)
        record, template = promote_pack(
            store, _config(tmp_path), "homelab-control", human_action=True
        )
        assert record.state == "active"
        assert template is not None
        assert record.trial is not None
        assert record.trial["level"] == "syntax"
        assert record.trial["command"] == ""
    finally:
        store.close()


def test_an_oversize_pack_is_refused_by_name(tmp_path: Path) -> None:
    """Inlining a large tree into a run's instructions is not a trial, it is a
    payload. The refusal names the size and both ways forward (I9)."""
    from skep.supervisor.skill_packs import SELF_TEST_MAX_BYTES, self_test_script

    directory = _self_test_pack(tmp_path, command="python scripts/check.py")
    # Incompressible, so the gzipped tar really does exceed the cap.
    (directory / "scripts" / "bulk.bin").write_bytes(
        __import__("os").urandom(SELF_TEST_MAX_BYTES + 1024)
    )
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        record = draft_pack(store, directory)
        with pytest.raises(SkillPackError) as excinfo:
            self_test_script(record)
    finally:
        store.close()
    message = str(excinfo.value)
    assert "KB packed" in message
    assert "self-test limit" in message
    assert "dropping its self_test" in message
