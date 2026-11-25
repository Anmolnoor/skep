"""v133 B5 (I1 I5 I6 I8 I9 I13): proposals and cards.

F25 a check that needs no side effect refuses at proposal, and still at
confirm; F26 the NO AUTH ENV refusal names the manual steps; F27 a PR review
on an unpinned project says so first; F28 the setup card shows the pin and
any auto-landing change, HIGH when it turns auto-landing on; F29 a host-shell
card says it runs outside the sandbox, and diagnose_run never runs unconfined;
F30 tool descriptions and results tell the truth; F35 a re-setup keeps a
stored "auto-dispatch off"; F36 wave 1's record is wired into the tool and
CLI faces.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import RunStore, SupervisorConfig, mint_task
from skep.supervisor.serve import actions
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import execute_mutation, mutation_execution_decision

from .conftest import predating_home


def _parts(config: SupervisorConfig) -> tuple[RunStore, ConfigHolder]:
    store = RunStore(predating_home(config.db_path))
    return store, ConfigHolder(config, store)


def _bind(store: RunStore, repo: Path, policy: dict[str, Any], phase: str = "build") -> None:
    store.add_project_policy(
        project_id="proj",
        name="proj",
        strategy="trusted_local_dev",
        phase=phase,
        policy={"default_execution_mode": "workspace", **policy},
    )
    store.add_project_binding(project_id="proj", binding_kind="repo_path", binding_value=str(repo))


def _confirm(store: RunStore, holder: ConfigHolder, name: str, args: dict[str, Any]) -> Any:
    return execute_mutation(
        name, args, store=store, holder=holder, runner=None,  # type: ignore[arg-type]
        actor="tester",
    )  # fmt: skip


# ---------- F25: refused at proposal, still checked at confirm (A9, F-I7) ----------


def test_an_unparseable_shell_command_is_refused_at_proposal_and_at_confirm(
    config: SupervisorConfig,
) -> None:
    store, holder = _parts(config)
    try:
        args = {"command": "echo 'unbalanced"}
        refused = mutation_execution_decision("run_shell", args, store=store, holder=holder)
        assert refused is not None and refused.verdict == "deny"
        assert "could not be parsed" in str(refused.detail)
        assert "No closing quotation" in str(refused.detail)
        with pytest.raises(ValueError, match="could not be parsed"):
            _confirm(store, holder, "run_shell", args)  # I5: the confirm checks again
        fine = mutation_execution_decision(
            "run_shell", {"command": "echo 'balanced'"}, store=store, holder=holder
        )
        assert fine is None or fine.verdict != "deny"
    finally:
        store.close()


def test_diagnose_run_on_a_swept_worktree_is_refused_at_proposal(
    config: SupervisorConfig, repo: Path, tmp_path: Path
) -> None:
    store, holder = _parts(config)
    try:
        task = mint_task(workspace=tmp_path / "gone", instructions="x")
        store.create_run(task, repo=repo, ref=None, execution_mode="sandbox")
        args = {"task_id": task.task_id, "command": "ls"}
        refused = mutation_execution_decision("diagnose_run", args, store=store, holder=holder)
        assert refused is not None and refused.verdict == "deny"
        assert "worktree is gone" in str(refused.detail)
        with pytest.raises(ValueError, match="worktree is gone"):
            _confirm(store, holder, "diagnose_run", args)
        unknown = mutation_execution_decision(
            "diagnose_run", {"task_id": "nope", "command": "ls"}, store=store, holder=holder
        )
        assert unknown is None or unknown.verdict != "deny"  # the confirm says "no run"
    finally:
        store.close()


def test_setup_project_with_an_unknown_field_is_refused_at_proposal(
    config: SupervisorConfig, repo: Path
) -> None:
    store, holder = _parts(config)
    try:
        args = {
            "project_id": "proj",
            "name": "proj",
            "repo_path": str(repo),
            "policy_overrides": {"network": ["registry.npmjs.org"]},
        }
        refused = mutation_execution_decision("setup_project", args, store=store, holder=holder)
        assert refused is not None and refused.verdict == "deny"
        assert "network" in str(refused.detail) and "default_network" in str(refused.detail)
        engine = mutation_execution_decision(
            "setup_project", {**args, "policy_overrides": {}, "engine": "nope"},
            store=store, holder=holder,
        )  # fmt: skip
        assert engine is not None and engine.verdict == "deny"
        assert "nope" in str(engine.detail)
        fine = mutation_execution_decision(
            "setup_project", {**args, "policy_overrides": {"default_network": ["x.org"]}},
            store=store, holder=holder,
        )  # fmt: skip
        assert fine is None or fine.verdict != "deny"
    finally:
        store.close()


def test_a_dispatch_whose_engine_has_no_auth_env_is_refused_at_proposal(
    config: SupervisorConfig, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    store, holder = _parts(config)
    try:
        _bind(
            store, repo,
            {"coding_engine": "claude_code", "verify_command": "uv run pytest",
             "default_execution_mode": "sandbox"},
        )  # fmt: skip
        args = {"repo": str(repo), "instructions": "do a thing"}
        refused = mutation_execution_decision("dispatch_run", args, store=store, holder=holder)
        assert refused is not None and refused.verdict == "deny"
        assert "NO AUTH ENV" in str(refused.detail)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "x")
        fine = mutation_execution_decision("dispatch_run", args, store=store, holder=holder)
        assert fine is None or fine.verdict != "deny"
    finally:
        store.close()


def test_r2_t2_a_dispatch_that_never_starts_the_engine_is_not_refused_for_its_auth(
    config: SupervisorConfig, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a coding run launches the project's external engine (the
    dispatcher's own condition), so only it needs the engine's login."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    store, holder = _parts(config)
    try:
        _bind(
            store, repo,
            {"coding_engine": "claude_code", "verify_command": "uv run pytest",
             "default_execution_mode": "sandbox"},
        )  # fmt: skip
        script = {"repo": str(repo), "instructions": "count lines", "caste": "script"}
        fine = mutation_execution_decision("dispatch_run", script, store=store, holder=holder)
        assert fine is None or "NO AUTH ENV" not in str(fine.detail)
        coding = {"repo": str(repo), "instructions": "do a thing", "caste": "coding"}
        refused = mutation_execution_decision("dispatch_run", coding, store=store, holder=holder)
        assert refused is not None and "NO AUTH ENV" in str(refused.detail)
    finally:
        store.close()


def _landed(store: RunStore, repo: Path, tmp_path: Path, branch: str) -> str:
    task = mint_task(workspace=repo, instructions="land me")
    store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
    store.transition(task.task_id, "completed")
    patch = tmp_path / f"{task.task_id}.patch"
    patch.write_text("diff --git a/n b/n\n", encoding="utf-8")
    store.add_artifact(task.task_id, kind="patch", audit_path=patch, sha256="0" * 64)
    store.record_reverification(
        task.task_id, outcome="passed", worker_outcome="passed", confirmed=True,
        commands=["uv run pytest"], exit_codes=[0], detail="ok",
    )  # fmt: skip
    review_id = store.enqueue_approval(task.task_id, action="apply_patch", reason="land")
    store.resolve_approval(review_id, approved=True, actor="chat-user", landing_branch=branch)
    return str(task.task_id)


def test_open_pr_grouping_an_already_landed_run_is_refused_at_proposal(
    config: SupervisorConfig, repo: Path, tmp_path: Path
) -> None:
    """The 08:30 "already landed" error, reproduced: a run landed on its own
    branch cannot join a grouped PR's shared branch. The confirm said so after
    the click; the proposal now says it, with no card."""
    from skep.supervisor.policy_write import PolicyWriteRejected
    from skep.supervisor.serve.tools import policy_write_preview

    store, holder = _parts(config)
    try:
        task_id = _landed(store, repo, tmp_path, f"skep/{'x' * 4}")
        args = {"task_ids": [task_id], "title": "bump deps"}
        with pytest.raises(Exception, match="already landed on 'skep/xxxx'") as confirmed:
            _confirm(store, holder, "open_pr", args)
        assert getattr(confirmed.value, "status_code", None) == 409
        with pytest.raises(PolicyWriteRejected, match="already landed on 'skep/xxxx'"):
            policy_write_preview(store, holder, "open_pr", args)
        # the single-run form reuses the landing branch: no refusal
        assert policy_write_preview(store, holder, "open_pr", {"task_id": task_id}) == {}
    finally:
        store.close()


def test_r2_t1_the_grouped_pr_proposal_resolves_runs_as_its_confirm_does(
    config: SupervisorConfig, repo: Path, tmp_path: Path
) -> None:
    """A patchless first entry does not name the shared branch (the confirm
    skips it), and an abbreviated id still finds a run landed elsewhere."""
    from skep.supervisor.policy_write import PolicyWriteRejected
    from skep.supervisor.serve.tools import policy_write_preview

    store, holder = _parts(config)
    try:
        script = mint_task(workspace=repo, instructions="report only")
        store.create_run(script, repo=repo, ref=None, execution_mode="workspace")
        store.transition(script.task_id, "completed")
        retried = _landed(store, repo, tmp_path, "")  # landed on its own default branch
        args = {"task_ids": [script.task_id, retried]}
        # a retry: the confirm skips the script run and lands on skep/<retried>
        assert policy_write_preview(store, holder, "open_pr", args) == {}

        elsewhere = _landed(store, repo, tmp_path, "skep/xxxx")
        short = {"task_ids": [elsewhere[:-6]], "title": "bump deps"}  # abbreviated
        with pytest.raises(PolicyWriteRejected, match="already landed on 'skep/xxxx'"):
            policy_write_preview(store, holder, "open_pr", short)
    finally:
        store.close()


# ---------- F26: NO AUTH ENV names the operator's manual steps (C1) ----------


def test_the_no_auth_env_refusal_names_the_three_manual_steps(
    config: SupervisorConfig, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor.dispatch import no_auth_env_refusal
    from skep.supervisor.engines import resolve_engine

    for var in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    serve_env = Path("/h/.skep/serve.env")
    claude = str(no_auth_env_refusal(resolve_engine("claude_code"), serve_env=serve_env))
    assert "NO AUTH ENV" in claude
    assert "`claude setup-token`" in claude
    assert "CLAUDE_CODE_OAUTH_TOKEN=<token>" in claude and str(serve_env) in claude
    assert "restart skep serve" in claude
    assert "the chat cannot do these steps" in claude
    codex = str(no_auth_env_refusal(resolve_engine("codex"), serve_env=serve_env))
    assert "OPENAI_API_KEY=<key>" in codex and "setup-token" not in codex
    # the proposal refusal is the same text, with this home's serve.env
    store, holder = _parts(config)
    try:
        _bind(
            store, repo,
            {"coding_engine": "claude_code", "verify_command": "uv run pytest",
             "default_execution_mode": "sandbox"},
        )  # fmt: skip
        refused = mutation_execution_decision(
            "dispatch_run", {"repo": str(repo), "instructions": "x"}, store=store, holder=holder
        )
        assert refused is not None and "`claude setup-token`" in str(refused.detail)
        assert str(config.home.parent / "serve.env") in str(refused.detail)
    finally:
        store.close()


# ---------- F27: a PR review on an unpinned project says so first (A5) ----------


def test_a_pr_review_dispatch_on_an_unpinned_project_says_so_on_its_card(
    config: SupervisorConfig, repo: Path
) -> None:
    from skep.supervisor.serve.cards import card_summary, recorded_card
    from skep.supervisor.serve.tools import dispatch_manifest_for

    store, holder = _parts(config)
    try:
        _bind(store, repo, {})
        args = {"repo": str(repo), "instructions": "review", "ref": "refs/skep/pr/8/head"}

        def card(call: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            manifest = dispatch_manifest_for(store, holder, call, None)
            recorded = recorded_card("dispatch_run", call, None, store, manifest) or {}
            return recorded, card_summary("dispatch_run", call, store=store, recorded=recorded)

        recorded, summary = card(args)
        line = recorded["pin_warning"]
        assert "PR #8" in line and "no verify_command" in line
        assert f'skep project setup {repo} --verify-command "<command>"' in line
        assert "the review can still run" in line
        assert str(summary["risk"]).startswith(line)
        # still a card, not a refusal
        decision = mutation_execution_decision("dispatch_run", args, store=store, holder=holder)
        assert decision is None or decision.verdict != "deny"
        # a pinned project, or a dispatch that reviews no PR: no line
        assert "pin_warning" not in card({**args, "ref": None})[0]
        _bind(store, repo, {"verify_command": "uv run pytest"})
        assert "pin_warning" not in card(args)[0]
    finally:
        store.close()


# ---------- F28: the setup card shows the pin and auto-landing (J9, F-I1) ----------


def _setup_card(store: RunStore, args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    from skep.supervisor.serve.cards import card_summary, high_tier, recorded_card

    high = high_tier("setup_project", args, store=store)
    recorded = recorded_card("setup_project", args, high, store) or {}
    return recorded, card_summary("setup_project", args, store=store, recorded=recorded)


def test_the_setup_card_shows_a_syntax_only_pin_change_and_no_landing_change(
    config: SupervisorConfig, repo: Path
) -> None:
    store, _holder = _parts(config)
    try:
        _bind(
            store, repo,
            {"verify_command": "uv run pytest", "auto_apply_verified_patch": False},
            phase="maintain",
        )  # fmt: skip
        args = {
            "project_id": "proj", "name": "proj", "phase": "maintain",
            "policy_overrides": {"verify_command": "node --check a.js"},
        }  # fmt: skip
        recorded, summary = _setup_card(store, args)
        pin = recorded["setup"]["verify_command"]
        assert (pin["old"], pin["new"]) == ("uv run pytest", "node --check a.js")
        assert "syntax-only" in str(pin["syntax_only"])
        assert "auto_landing" not in recorded["setup"]  # F5: the stored "off" holds
        risk = str(summary["risk"])
        assert "`uv run pytest` → `node --check a.js`" in risk and "syntax-only" in risk
        assert "auto-landing:" not in risk
    finally:
        store.close()


def test_a_setup_card_that_turns_auto_landing_on_is_high_and_names_the_branch(
    config: SupervisorConfig, repo: Path
) -> None:
    store, _holder = _parts(config)
    try:
        _bind(store, repo, {"verify_command": "uv run pytest"})  # build: off
        args = {"project_id": "proj", "name": "proj", "phase": "maintain"}  # derived, not asked
        recorded, summary = _setup_card(store, args)
        landing = recorded["setup"]["auto_landing"]
        assert landing == {"old": False, "new": True, "asked": False, "branch": "skep/maintain"}
        assert recorded["tier"] == "high" and "auto-landing on" in recorded["high"]
        assert summary["tier"] == "high"
        assert "auto-landing: off → ON (from the phase default), lands on skep/maintain" in str(
            summary["risk"]
        )
        # the same phase move with a syntax-only pin cannot turn it on: not HIGH
        _bind(store, repo, {"verify_command": "node --check a.js"})
        recorded, _summary = _setup_card(store, args)
        assert "auto_landing" not in recorded["setup"] and "tier" not in recorded
        assert recorded["setup"]["verify_command"]["syntax_only"]
    finally:
        store.close()


# ---------- R2 (Sol T3): a setup card is bound to the effect it showed ----------


def test_r2_a_setup_confirm_after_a_phase_change_is_refused_not_landed_silently(
    config: SupervisorConfig, repo: Path
) -> None:
    """The card showed no auto-landing change; the project moved phase while it
    was open, so the same write would now turn auto-landing on. Refused."""
    from fastapi import HTTPException

    store, holder = _parts(config)
    try:
        policy = {"verify_command": "uv run pytest", "auto_apply_verified_patch": False}
        _bind(store, repo, policy, phase="maintain")
        args = {"project_id": "proj", "name": "proj", "phase": "maintain"}
        recorded, _summary = _setup_card(store, args)
        assert "auto_landing" not in recorded["setup"]  # the card shows no change
        from skep.supervisor.serve.registry import set_project_phase

        set_project_phase(store, "proj", "build")  # the project moves while the card is open
        with pytest.raises(HTTPException) as refused:
            execute_mutation(
                "setup_project", args, store=store, holder=holder,
                runner=None, actor="tester", card=recorded,  # type: ignore[arg-type]
            )  # fmt: skip
        assert refused.value.status_code == 409
        assert "changed since this card was shown" in str(refused.value.detail)
        stored = store.get_project_policy("proj")
        assert stored is not None and stored.phase == "build"
        assert stored.policy.get("auto_apply_verified_patch") is not True
        # a card drawn now shows the change, and its confirm writes it
        recorded, _summary = _setup_card(store, args)
        assert recorded["setup"]["auto_landing"]["new"] is True
        execute_mutation(
            "setup_project", args, store=store, holder=holder,
            runner=None, actor="tester", card=recorded,  # type: ignore[arg-type]
        )  # fmt: skip
        stored = store.get_project_policy("proj")
        assert stored is not None and stored.phase == "maintain"
    finally:
        store.close()


# ---------- F29: a host shell says where it runs; diagnose_run never unconfined (F-I6) ----------


def test_a_host_shell_card_says_it_runs_outside_the_sandbox_as_you() -> None:
    from skep.supervisor.serve.cards import risk

    plain = str(risk("run_shell", {"command": "df -h"}))
    assert "on this machine as your account, outside the sandbox" in plain
    assert "outside the sandbox" in str(risk("start_process", {"command": "python -m http.server"}))
    agent = str(risk("run_shell", {"command": 'cd /tmp && claude -p "Reply OK" --model opus'}))
    assert "starts the agent CLI `claude`" in agent and "your own login" in agent
    for command in ("ls -la ~/.claude", "cat $HOME/.codex/config.toml", "ls /home/al/.aws"):
        line = str(risk("run_shell", {"command": command}))
        assert "credential directory" in line, command
    assert "credential" not in plain and "agent CLI" not in plain
    # diagnose_run runs in the sandbox: its card makes no host-shell claim
    assert "outside the sandbox" not in str(risk("diagnose_run", {"command": "ls"}))


def test_diagnose_run_refuses_when_the_sandbox_is_off(
    config: SupervisorConfig, repo: Path, tmp_path: Path
) -> None:
    """Sol's note (K): with the sandbox off, diagnose_run ran the command on
    the worker's kept tree unconfined, as the operator. It refuses instead,
    at proposal and at confirm."""
    import dataclasses

    off = dataclasses.replace(config, sandbox=False)
    store = RunStore(predating_home(off.db_path))
    holder = ConfigHolder(off, store)
    try:
        kept = tmp_path / "kept"
        kept.mkdir()
        task = mint_task(workspace=kept, instructions="x")
        store.create_run(task, repo=repo, ref=None, execution_mode="sandbox")
        args = {"task_id": task.task_id, "command": "touch escaped"}
        refused = mutation_execution_decision("diagnose_run", args, store=store, holder=holder)
        assert refused is not None and refused.verdict == "deny"
        assert "sandbox is off" in str(refused.detail)
        with pytest.raises(ValueError, match="sandbox is off"):
            _confirm(store, holder, "diagnose_run", args)
        assert not (kept / "escaped").exists()
    finally:
        store.close()


# ---------- F30: descriptions and results tell the truth (F-I2, F-I4, A10, B4, G4, B1) ----------


def test_the_dispatch_card_shows_the_base_ref_and_commit(
    config: SupervisorConfig, repo: Path
) -> None:
    from skep.supervisor.serve.cards import card_summary, recorded_card
    from skep.supervisor.serve.tools import dispatch_manifest_for

    from .conftest import git

    store, holder = _parts(config)
    try:
        _bind(store, repo, {})
        head = git(repo, "rev-parse", "HEAD").stdout.strip()
        git(repo, "branch", "skep/harness")
        args = {"repo": str(repo), "instructions": "x"}
        manifest = dispatch_manifest_for(store, holder, args, None)
        assert (manifest["base_ref"], manifest["base_commit"]) == ("main", head)
        recorded = recorded_card("dispatch_run", args, None, store, manifest) or {}
        summary = card_summary("dispatch_run", args, store=store, recorded=recorded)
        assert summary["headline"].endswith(f"@ main ({head[:7]})")
        named = dispatch_manifest_for(store, holder, {**args, "ref": "skep/harness"}, None)
        assert named["base_ref"] == "skep/harness"
    finally:
        store.close()


def test_dispatch_run_says_what_it_cannot_do() -> None:
    from skep.supervisor.serve.tools import tool_description

    text = tool_description("dispatch_run")
    assert "No model or effort option" in text
    assert "read-only ask" in text and "run_code" in text and "read tools" in text
    assert "changes no file ends failed" in text


def test_list_approvals_says_it_excludes_chat_cards_and_counts_this_chats(
    config: SupervisorConfig,
) -> None:
    from skep.supervisor.serve.tools import execute_read_tool, tool_description

    assert "chat cards are not in it" in tool_description("list_approvals")
    store, holder = _parts(config)
    try:
        chat_id = store.create_chat(title="t", model=None).chat_id
        other = store.create_chat(title="o", model=None).chat_id
        store.add_chat_action(chat_id, tool="run_shell", args={"command": "ls"})
        store.add_chat_action(chat_id, tool="run_shell", args={"command": "pwd"})
        store.add_chat_action(other, tool="run_shell", args={"command": "df"})
        result = execute_read_tool(
            "list_approvals", {}, store=store, holder=holder, chat_id=chat_id
        )
        assert result["approvals"] == [] and result["chat_cards_pending"] == 2
    finally:
        store.close()


def test_list_skills_reports_the_seed_recipes_beside_drafts_and_packs(
    config: SupervisorConfig,
) -> None:
    from skep.supervisor.serve.tools import execute_read_tool, tool_description

    assert "seed recipes" in tool_description("list_skills")
    from skep.supervisor.seed_skills import load_seed_skills

    store, holder = _parts(config)
    try:
        load_seed_skills(store)  # serve seeds at startup
        seeds = [t for t in store.list_templates() if t.provenance == "seed"]
        result = execute_read_tool("list_skills", {}, store=store, holder=holder)
        assert result["skills"] == [] and len(seeds) > 0
        assert result["seed_recipes"] == len(seeds)
        assert "list_templates" in result["seed_note"]
    finally:
        store.close()


def test_run_shell_points_read_only_probes_at_the_read_tools() -> None:
    from skep.supervisor.serve.tools import tool_description

    text = tool_description("run_shell")
    assert "read-only probe" in text
    assert all(name in text for name in ("read_file", "search_files", "list_prs"))


# ---------- F35: a re-setup keeps a stored "auto-dispatch off" (J9) ----------


def test_a_same_phase_re_setup_keeps_a_stored_auto_dispatch_off(
    config: SupervisorConfig, repo: Path
) -> None:
    from skep.supervisor.serve.registry import repos_root, setup_project_record

    store, holder = _parts(config)
    try:
        _bind(store, repo, {"auto_dispatch_allowed": False}, phase="build")

        def setup(phase: str, overrides: dict[str, Any]) -> dict[str, Any]:
            return setup_project_record(
                run_store=store, root=repos_root(holder), project_id="proj", name="proj",
                strategy="trusted_local_dev", phase=phase, repo_path=str(repo),
                policy_overrides=overrides,
            )  # fmt: skip

        kept = setup("build", {"verify_command": "uv run pytest"})
        assert kept["policy"]["auto_dispatch_allowed"] is False
        assert "auto_dispatch_allowed" not in {c["key"] for c in kept["policy_changes"]}
        asked = setup("build", {"auto_dispatch_allowed": True})
        assert asked["policy"]["auto_dispatch_allowed"] is True
        _bind(store, repo, {"auto_dispatch_allowed": False}, phase="build")
        moved = setup("maintain", {})  # a phase move re-derives it, as before
        assert moved["policy"]["auto_dispatch_allowed"] is True
    finally:
        store.close()


# ---------- F36: wave 1's record, wired into the tool and CLI faces (F11 F14 F18) ----------


def test_get_policy_from_chat_labels_itself_the_global_layer(config: SupervisorConfig) -> None:
    from skep.supervisor.serve.tools import execute_read_tool

    store, holder = _parts(config)
    try:
        view = execute_read_tool("get_policy", {}, store=store, holder=holder)
        assert view["layer"] == "global" and "global layer only" in view["layer_note"]
    finally:
        store.close()


def _fake_pr(monkeypatch: pytest.MonkeyPatch, bodies: list[str]) -> None:
    from skep.supervisor import cli_cmds, github

    def fake(**kwargs: Any) -> github.PullRequestResult:
        bodies.append(str(kwargs.get("body") or ""))
        return github.PullRequestResult(True, "https://x/pull/1", "opened", True, "c0ffee12")

    monkeypatch.setattr(github, "open_pull_request", fake)
    monkeypatch.setattr(cli_cmds, "open_pull_request", fake)


def test_open_pr_in_branch_mode_records_the_chat_actor(
    config: SupervisorConfig, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from .conftest import git

    _fake_pr(monkeypatch, [])
    store, holder = _parts(config)
    try:
        git(repo, "branch", "skep/stack")
        _confirm(store, holder, "open_pr", {"branch": "skep/stack", "repo": str(repo)})
        [row] = store.pr_pushes()
        assert (row["actor"], row["branch"]) == ("tester", "skep/stack")
    finally:
        store.close()


def test_the_cli_pr_paths_record_the_push_and_the_supervisor_result(
    config: SupervisorConfig, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse
    import getpass
    import shutil

    from skep.supervisor import cli_cmds

    from .conftest import git

    bodies: list[str] = []
    _fake_pr(monkeypatch, bodies)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    store = RunStore(config.db_path)
    try:
        task = mint_task(workspace=repo, instructions="x")
        store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
        store.record_reverification(
            task.task_id, outcome="failed", worker_outcome="passed", confirmed=False,
            commands=["uv run pytest"], exit_codes=[1], detail="1 failed",
        )  # fmt: skip
        record = store.get_run(task.task_id)
        assert record is not None
        cli_cmds.open_pr_for_branch(
            store, record, "skep/x", base="main", audit_dir=tmp_path, actor="op"
        )
        line = actions.g10_line(store.reverification_for(task.task_id))
        assert line and line in bodies[-1]  # the supervisor's own words, from the store
        [row] = store.pr_pushes()
        assert (row["actor"], row["task_ids"], row["head_commit"]) == (
            "op",
            [task.task_id],
            "c0ffee12",
        )
    finally:
        store.close()
    git(repo, "branch", "skep/cli")
    home = config.home.parent
    monkeypatch.setattr(cli_cmds, "build_config", lambda *_a: config)
    assert cli_cmds.cmd_pr_open(
        argparse.Namespace(home=home, repo=str(repo), branch="skep/cli", base="main", title=None)
    ) == 0  # fmt: skip
    store = RunStore(config.db_path)
    try:
        assert store.pr_pushes()[-1]["actor"] == getpass.getuser()
    finally:
        store.close()


def test_a_cli_merge_records_who_resolved_its_card(
    config: SupervisorConfig, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse
    import getpass

    from skep.supervisor import cli_cmds, github
    from skep.supervisor.serve import cards

    pin = {"repo": "acme/widget", "number": 7, "head_commit": "a" * 40, "base_commit": "b" * 40}
    monkeypatch.setattr(cli_cmds, "build_config", lambda *_a: config)
    monkeypatch.setattr(cli_cmds, "_pr_repo", lambda _args: repo)
    monkeypatch.setattr(cards, "merge_pin", lambda *_a: pin)
    monkeypatch.setattr(cards, "merge_line", lambda _pin: "pinned")
    monkeypatch.setattr(
        github, "merge_pull_request", lambda **_: github.MergeResult(True, "merged", "c" * 40)
    )
    args = argparse.Namespace(home=config.home.parent, repo=str(repo), number="7", method="merge")
    assert cli_cmds.cmd_pr_merge(args) == 0
    store = RunStore(config.db_path)
    try:
        [action] = [a for c in store.list_chats() for a in store.chat_actions(c.chat_id)]
        assert (action.resolved_by, action.resolved_surface) == (getpass.getuser(), "cli")
    finally:
        store.close()
