"""v25-F1: the command deck — deterministic /commands over the existing verbs.

Three thin server wrappers (repo state, land, set-phase), plus the operator-
command audit path: a slash mutation becomes a chat_actions row with source
'operator', resolves on the commands endpoints under actor 'operator-command',
and the model is never in that loop (no LLM is configured in these tests).
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.packs import get_policy_pack
from skep.supervisor.serve import actions
from skep.supervisor.serve.commands import WEB, commands_for
from skep.supervisor.serve.jobs import Dispatcher
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import COMMAND_TOOL_NAMES, execute_mutation

from .conftest import git, serve_client, wait_terminal


def _seed_workspace_project(config: SupervisorConfig, repo: Path) -> None:
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="deck-project",
            name="deck project",
            strategy="trusted_local_dev",
            phase="build",
            policy={
                "default_execution_mode": "workspace",
                "auto_apply_verified_patch": False,
            },
        )
        store.add_project_binding(
            project_id="deck-project",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
    finally:
        store.close()


def test_repo_state_endpoint_wraps_repo_state_view(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    client = serve_client(config)
    # v48-F4: a /workon path-bound repo is addressable by its absolute path.
    # The ASGI server decodes %2F in the URL, so the old single-segment {name}
    # converter could never match a host path — the deck's /state and /policy
    # 404ed for every workon workspace.
    state = client.get(f"/api/repos/{repo}/state")
    assert state.status_code == 200
    assert state.json()["repo"] == str(repo.resolve())
    assert state.json()["recent_default_branch_commits"]

    policy = client.get(f"/api/repos/{repo}/effective-policy")
    assert policy.status_code == 200
    assert policy.json()["repo"] == str(repo.resolve())

    root = config.home.parent / "repos"
    root.mkdir(parents=True, exist_ok=True)
    slug_repo = root / "deck-repo"
    slug_repo.mkdir()
    git(slug_repo, "init", "-q")
    git(slug_repo, "config", "user.email", "test@example.com")
    git(slug_repo, "config", "user.name", "Test")
    (slug_repo / "a.txt").write_text("a\n")
    git(slug_repo, "add", "a.txt")
    git(slug_repo, "commit", "-qm", "seed")

    view = client.get("/api/repos/deck-repo/state").json()
    assert view["repo"] == str(slug_repo.resolve())
    assert view["checked_out_branch"] == view["default_branch"]
    assert [branch["name"] for branch in view["branches"]] == [view["default_branch"]]
    assert view["recent_default_branch_commits"]


def test_effective_policy_names_engine_protocol_and_verify_pin(
    repo: Path, config: SupervisorConfig
) -> None:
    """v96-F1: the one policy read every surface shares says which engine,
    protocol, and verify command a run will get. Unpinned verify renders the
    fallback marker, never blank — the weaker guarantee stays visible (I2/I8).
    """
    client = serve_client(config)
    _seed_workspace_project(config, repo)

    unpinned = client.get(f"/api/repos/{repo}/effective-policy").json()
    assert unpinned["coding_engine"] == "builtin"
    assert unpinned["worker_protocol"] == "plan"
    assert unpinned["verify_command"] == "(worker-nominated fallback)"

    store = RunStore(config.db_path)
    try:
        # add_project_policy is an upsert — pin the same project.
        store.add_project_policy(
            project_id="deck-project",
            name="deck project",
            strategy="trusted_local_dev",
            phase="build",
            policy={
                "default_execution_mode": "workspace",
                "auto_apply_verified_patch": False,
                "verify_command": "uv run pytest",
                "worker_protocol": "react",
            },
        )
    finally:
        store.close()

    pinned = client.get(f"/api/repos/{repo}/effective-policy").json()
    assert pinned["verify_command"] == "uv run pytest"
    assert pinned["worker_protocol"] == "react"
    assert pinned["coding_engine"] == "builtin"


def test_push_and_open_pr_ride_the_operator_command_path(
    repo: Path, config: SupervisorConfig
) -> None:
    """v96-F4: the composer's Push / Open PR buttons propose the EXISTING
    carded verbs — the only gate that stood between them and the
    operator-command resolution path was the COMMAND_TOOL_NAMES allowlist.
    open_pr's selector refusal names all three modes (I9)."""
    assert "push_branch" in COMMAND_TOOL_NAMES
    assert "open_pr" in COMMAND_TOOL_NAMES

    store = RunStore(config.db_path)
    holder = ConfigHolder(config, store)
    runner = cast(Dispatcher, None)  # validation refuses before any dispatch

    def open_pr(args: dict[str, object]) -> None:
        execute_mutation(
            "open_pr", args, store=store, holder=holder, runner=runner, actor="operator-command"
        )

    try:
        with pytest.raises(ValueError, match="task_id, task_ids, or branch"):
            open_pr({})
        with pytest.raises(ValueError, match="exactly one"):
            open_pr({"task_id": "t-1", "branch": "skep/x"})
        with pytest.raises(ValueError, match="repo="):
            open_pr({"branch": "skep/x"})
    finally:
        store.close()


def test_open_pr_for_branch_refuses_default_and_missing_branches(
    repo: Path, config: SupervisorConfig
) -> None:
    """Branch mode inherits the house line: the default branch never rides a
    PR head (main moves via merge_pr, I1), and a ghost branch teaches (I9)."""
    store = RunStore(config.db_path)
    holder = ConfigHolder(config, store)
    default = git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip()
    try:
        with pytest.raises(HTTPException) as refused:
            actions.open_pr_for_branch(holder, store, str(repo), branch=default)
        assert refused.value.status_code == 400
        assert "merge_pr" not in refused.value.detail  # teaches the fix, not jargon
        assert "working branch" in refused.value.detail

        with pytest.raises(HTTPException) as missing:
            actions.open_pr_for_branch(holder, store, str(repo), branch="skep/ghost")
        assert missing.value.status_code == 404
        assert "skep/ghost" in missing.value.detail
    finally:
        store.close()


def test_push_branch_pushes_the_checked_out_branch_never_the_default(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    """v96-F5 (live-verify find): v57-F7's guard ORed in `default_branch`,
    which returns the CURRENT checkout — so push_branch refused whatever
    branch you were on. The I1 line is the DEFAULT branch, nothing else."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    git(repo, "remote", "add", "origin", str(origin))
    default = git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip()
    git(repo, "push", "-q", "origin", default)
    git(repo, "checkout", "-qb", "feature-x")
    (repo / "more.txt").write_text("more\n")
    git(repo, "add", "more.txt")
    git(repo, "commit", "-qm", "more")

    store = RunStore(config.db_path)
    holder = ConfigHolder(config, store)
    try:
        pushed = actions.push_branch(holder, str(repo), name="feature-x", store=store)
        assert pushed["pushed"] is True
        remote_heads = git(
            tmp_path, "-C", str(origin), "for-each-ref", "--format=%(refname:short)"
        ).stdout.split()
        assert "feature-x" in remote_heads

        # The true default still refuses — even while feature-x is checked out.
        with pytest.raises(HTTPException) as refused:
            actions.push_branch(holder, str(repo), name=default, store=store)
        assert refused.value.status_code == 400
        assert "merge_pr" in refused.value.detail

        # F5's twin: the PR guard passes a checked-out feature branch too
        # (a remoteless repo then fails honestly, never a 400 — Phase 09 R3
        # S16: it has no GitHub repo to name to gh).
        git(repo, "remote", "remove", "origin")
        result = actions.open_pr_for_branch(holder, store, str(repo), branch="feature-x")
        assert result["opened"] is False
        assert "not a github.com repository" in result["detail"]
    finally:
        store.close()


def test_land_endpoint_lands_a_completed_run_on_a_named_branch(
    repo: Path, config: SupervisorConfig
) -> None:
    _seed_workspace_project(config, repo)
    client = serve_client(config)
    response = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "workspace",
        },
    )
    assert response.status_code == 202
    task_id = str(response.json()["task_id"])
    assert wait_terminal(client, task_id)["state"] == "completed"

    landed = client.post(
        f"/api/runs/{task_id}/land",
        json={"actor": "operator-command", "branch": "deck/landing"},
    )
    assert landed.status_code == 200
    assert landed.json()["action"] == "applied"
    assert landed.json()["branch"] == "deck/landing"
    assert "deck/landing" in git(repo, "branch", "--list", "deck/landing").stdout
    # The landing IS the approval: the review resolved under the authenticated principal (ES-F1).
    store = RunStore(config.db_path)
    try:
        approvals = store.approvals_for(task_id)
    finally:
        store.close()
    assert [a.status for a in approvals] == ["approved"]
    assert approvals[0].resolved_by == "operator"

    assert client.post("/api/runs/no-such-task/land", json={}).status_code == 404


def test_phase_endpoint_matches_cli_set_phase_semantics(
    repo: Path, config: SupervisorConfig
) -> None:
    client = serve_client(config)
    setup = client.post(
        "/api/projects/setup",
        json={
            "project_id": "deck-project",
            "name": "deck project",
            "pack": "trusted_local_dev",
            "phase": "build",
            "repo_path": str(repo),
            # v127-F5: maintain's auto-apply default needs a real pin to hold.
            "policy_overrides": {"verify_command": "uv run pytest"},
        },
    )
    assert setup.status_code == 201

    updated = client.post("/api/projects/deck-project/phase", json={"phase": "maintain"})
    assert updated.status_code == 200
    body = updated.json()
    assert body["phase"] == "maintain"
    # Policy is re-derived from the pack's phase defaults, like the CLI.
    pack = get_policy_pack("trusted_local_dev")
    for key, value in pack.phase_defaults["maintain"].items():
        assert body["policy"][key] == value
    assert client.get("/api/projects/deck-project").json()["phase"] == "maintain"

    assert (
        client.post("/api/projects/deck-project/phase", json={"phase": "warp"}).status_code == 400
    )
    assert client.post("/api/projects/ghost/phase", json={"phase": "build"}).status_code == 404


def test_operator_command_executes_without_a_model(repo: Path, config: SupervisorConfig) -> None:
    """The whole point of the deck: no LLM is configured here, and the
    propose → confirm loop still lands the mutation, audited."""
    client = serve_client(config)
    client.post(
        "/api/projects/setup",
        json={
            "project_id": "deck-project",
            "name": "deck project",
            "pack": "trusted_local_dev",
            "phase": "build",
            "repo_path": str(repo),
        },
    )
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]

    phase_args = {"project_id": "deck-project", "phase": "maintain"}
    proposed = client.post(
        f"/api/chats/{chat_id}/commands",
        json={"tool": "set_project_phase", "args": phase_args},
    )
    assert proposed.status_code == 201
    action_id = proposed.json()["action_id"]

    detail = client.get(f"/api/chats/{chat_id}").json()
    (action,) = detail["actions"]
    assert action["source"] == "operator"
    assert action["status"] == "proposed"
    # Nothing ran yet: proposing is not executing.
    assert client.get("/api/projects/deck-project").json()["phase"] == "build"

    confirmed = client.post(f"/api/chats/{chat_id}/commands/{action_id}/confirm")
    assert confirmed.status_code == 200
    assert confirmed.json()["ok"] is True
    assert confirmed.json()["result"]["phase"] == "maintain"
    assert client.get("/api/projects/deck-project").json()["phase"] == "maintain"

    (resolved,) = client.get(f"/api/chats/{chat_id}").json()["actions"]
    assert resolved["status"] == "confirmed"
    # Resolution is final.
    assert client.post(f"/api/chats/{chat_id}/commands/{action_id}/confirm").status_code == 409
    # No model transcript entries were created for any of this.
    assert client.get(f"/api/chats/{chat_id}").json()["messages"] == []


def test_operator_command_deny_cancels_without_execution(
    repo: Path, config: SupervisorConfig
) -> None:
    client = serve_client(config)
    client.post(
        "/api/projects/setup",
        json={
            "project_id": "deck-project",
            "name": "deck project",
            "pack": "trusted_local_dev",
            "phase": "build",
            "repo_path": str(repo),
        },
    )
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    phase_args = {"project_id": "deck-project", "phase": "maintain"}
    action_id = client.post(
        f"/api/chats/{chat_id}/commands",
        json={"tool": "set_project_phase", "args": phase_args},
    ).json()["action_id"]

    denied = client.post(f"/api/chats/{chat_id}/commands/{action_id}/deny")
    assert denied.status_code == 200
    assert denied.json()["denied"] is True
    assert client.get("/api/projects/deck-project").json()["phase"] == "build"
    (resolved,) = client.get(f"/api/chats/{chat_id}").json()["actions"]
    assert resolved["status"] == "denied"


MODEL_CARD_ON_THE_WEB = "/model is not available on web; it runs on cli — answer it in skep chat"


def _model_card(client: TestClient, chat_id: str) -> str:
    """The one card only skep chat carries: /model's set_assistant_model."""
    response = client.post(
        f"/api/chats/{chat_id}/commands",
        json={"tool": "set_assistant_model", "args": {"model": "m2"}},
    )
    return str(response.json()["action_id"])


def test_surface_is_a_hint_confirm_answers_where_asked_and_cancel_is_anywhere(
    config: SupervisorConfig,
) -> None:
    """Phase 10 R1 D1 (I7, I8): web and skep chat share one token — one
    operator — so ``?surface=`` is a client hint, not authorization. Confirm
    still answers a card where it was asked (the web is told where); a cancel
    only narrows, so any desk surface may give it."""
    client = serve_client(config)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    action_id = _model_card(client, chat_id)
    refused = client.post(f"/api/chats/{chat_id}/commands/{action_id}/confirm")
    assert refused.status_code == 403
    assert refused.json()["detail"] == MODEL_CARD_ON_THE_WEB
    (card,) = client.get(f"/api/chats/{chat_id}").json()["actions"]
    assert card["status"] == "proposed"
    assert card["web_refusal"] == MODEL_CARD_ON_THE_WEB  # the web draws it read-only
    canceled = client.post(f"/api/chats/{chat_id}/commands/{action_id}/deny")
    assert canceled.status_code == 200 and canceled.json()["denied"] is True
    (card,) = client.get(f"/api/chats/{chat_id}").json()["actions"]
    assert card["status"] == "denied" and "web_refusal" not in card
    # A card the web carries is never marked.
    web_card = client.post(
        f"/api/chats/{chat_id}/commands", json={"tool": "set_role", "args": {"name": "x"}}
    ).json()["action_id"]
    marked = {a["action_id"]: a for a in client.get(f"/api/chats/{chat_id}").json()["actions"]}
    assert "web_refusal" not in marked[web_card]


def test_a_card_the_web_cannot_confirm_never_holds_the_web_composer(
    config: SupervisorConfig,
) -> None:
    """D1: the web composer locks on exactly the cards the web can confirm,
    and the server's 409 follows the same rule for the web — while skep chat
    (no hint: the strict rule) still answers its own card first."""
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        client = serve_client(config)
        client.put(
            "/api/llm/config",
            json={"base_url": ollama.base_url, "default_model": "qwen3", "api_key": "sk-fake"},
        )
        chat_id = client.post("/api/chats", json={}).json()["chat_id"]
        _model_card(client, chat_id)
        held = client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"})
        assert held.status_code == 409  # skep chat: resolve its card first
        ollama.script_reply("hello")
        web = client.post(f"/api/chats/{chat_id}/messages?surface=web", json={"content": "hi"})
        assert web.status_code == 200, web.text
        assert "hello" in web.text
        client.post(
            f"/api/chats/{chat_id}/commands", json={"tool": "set_role", "args": {"name": "x"}}
        )
        again = client.post(f"/api/chats/{chat_id}/messages?surface=web", json={"content": "hi"})
        assert again.status_code == 409  # a card the web can confirm holds it
    finally:
        ollama.stop()


def test_command_endpoint_rejects_tools_outside_the_deck(config: SupervisorConfig) -> None:
    client = serve_client(config)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    for tool in ("dispatch_run", "set_policy", "forget_memory", "no_such_tool"):
        response = client.post(f"/api/chats/{chat_id}/commands", json={"tool": tool, "args": {}})
        assert response.status_code == 400, tool


def test_operator_and_assistant_verdict_paths_do_not_cross(config: SupervisorConfig) -> None:
    client = serve_client(config)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    operator_action = client.post(
        f"/api/chats/{chat_id}/commands",
        json={"tool": "deny_review", "args": {"review_id": "r-1"}},
    ).json()["action_id"]
    store = RunStore(config.db_path)
    try:
        assistant_action = store.add_chat_action(
            chat_id, tool="deny_review", args={"review_id": "r-2"}
        )
    finally:
        store.close()

    # The model-verdict endpoint refuses operator commands (before it ever
    # asks for a model), and the commands endpoint refuses model proposals.
    crossed = client.post(f"/api/chats/{chat_id}/actions/{operator_action}/confirm")
    assert crossed.status_code == 409
    assert "operator command" in crossed.json()["detail"]
    crossed_back = client.post(f"/api/chats/{chat_id}/commands/{assistant_action}/confirm")
    assert crossed_back.status_code == 409
    assert "assistant proposal" in crossed_back.json()["detail"]


def test_chat_composer_intercepts_slash_commands() -> None:
    """UI structure: '/' messages are parsed client-side and never reach the
    chat API; the command table and its executor live side by side."""
    from skep.supervisor.serve.app import STATIC_DIR

    source = (STATIC_DIR / "app.js").read_text()
    styles = (STATIC_DIR / "style.css").read_text()

    assert "function parseSlashCommand" in source
    assert 'content.startsWith("/")' in source
    assert "await runSlashCommand(content)" in source
    # Phase 10 10.3: /help, completion and refusals render from the registry's
    # web slice (GET /api/commands), never from a table of app.js's own.
    assert 'api("GET", "/api/commands")' in source
    assert "deck.commands.map(command =>" in source
    assert "commandHelp(deckRefusal(deck, name))" in source
    assert "commandHelp(`unknown command: /${name}`)" in source
    # The plan's command set is all on the web surface (F2 adds /workon).
    web = {row.name for row in commands_for(WEB)}
    for command in (
        "help:",
        "policy:",
        "repos:",
        "skills:",  # v81-F11
        "runs:",
        "approvals:",
        "state:",
        "setup:",
        "phase:",
        "land:",
        "approve:",
        "deny:",
        "schedule:",
    ):
        assert command[:-1] in web, command
    # Mutations go through the operator-command card endpoints, reads direct.
    assert "`/api/chats/${activeChatId}/commands`" in source
    assert "/api/chats/${activeChatId}/commands/${d.action_id}/${verb}" in source
    assert 'action.source === "operator"' in source
    assert ".command-help" in styles


def test_deck_reads_map_to_existing_routes() -> None:
    from skep.supervisor.serve.app import STATIC_DIR

    source = (STATIC_DIR / "app.js").read_text()
    assert "/api/repos/${encodeURIComponent(repo)}/${tail}" in source
    assert '"effective-policy" : "state"' in source
    assert "`/api/runs?limit=${limit}`" in source
    # /setup previews before proposing, so the card can state the grants.
    assert 'await api("POST", "/api/projects/preview", body)' in source
    assert "dangerous_grant_warnings" in source


def test_web_executors_and_the_registry_web_slice_cannot_drift() -> None:
    """The drift guard (v25-F3, Phase 10 10.3): app.js keeps no command table
    — /help, completion and refusals read the registry's web slice over
    GET /api/commands — so its executor branches and that slice must be one
    set, both ways. A web row with no executor would answer "unknown command";
    an executor with no web row is dead code, refused before it can run."""
    import re

    from skep.supervisor.serve.app import STATIC_DIR

    source = (STATIC_DIR / "app.js").read_text()
    executor_start = source.index("const runSlashCommand")
    executor = source[executor_start : source.index("const runStream", executor_start)]
    handled = set(re.findall(r'name === "(\w+)"', executor))
    web = {row.name for row in commands_for(WEB)}
    assert handled - web == set(), "app.js executors with no web row in serve/commands.py"
    assert web - handled == set(), "web rows in serve/commands.py with no app.js executor"


def test_composer_autocomplete_offers_the_deck() -> None:
    from skep.supervisor.serve.app import STATIC_DIR

    source = (STATIC_DIR / "app.js").read_text()
    styles = (STATIC_DIR / "style.css").read_text()
    assert 'class: "command-suggest"' in source
    assert "const updateSuggest" in source
    assert 'input.addEventListener("input", updateSuggest)' in source
    # Suggestions come from the same registry slice as the parser and /help.
    assert "deck.commands.filter(command => command.name.startsWith(needle))" in source
    assert ".command-suggest" in styles
    assert ".command-suggest-item" in styles


def test_queen_system_prompt_points_at_the_deck() -> None:
    from skep.supervisor.serve.chat import SYSTEM_PROMPT

    assert "/commands" in SYSTEM_PROMPT
    for command in ("/policy", "/workon", "/land", "/help"):
        assert command in SYSTEM_PROMPT
    assert "rather than" in SYSTEM_PROMPT  # ...proposing multi-step tool sequences


def test_docs_describe_the_command_deck() -> None:
    root = Path(__file__).resolve().parents[2]
    readme = (root / "README.md").read_text()
    claude_md = (root / "CLAUDE.md").read_text()
    assert "## The command deck" in readme
    assert "/workon" in readme
    assert "operator-command" in readme
    assert "command deck" in claude_md
    assert "source='operator'" in claude_md
