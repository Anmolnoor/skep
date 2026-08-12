"""v97 (ADR 0048): policy groups — reusable convenience grants.

F1: a group is vetted at write time with the SAME validators project policy
passes (I5) — trust-ramp keys refused naming the groupable set (I9),
dangerous shell prefixes refused by the shared guard (I4) — and builtins
resolve without a settings row, edit by materializing a copy, revert on
delete.

F2: attached groups compose LIVE in run_policy_for_repo — list keys union,
project scalars beat group scalars, attach order wins among groups; a
dangling attach fails the dispatch closed naming the group; the v90 engine
guard runs AFTER composition (a grouped engine still needs the verify pin).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.policy_resolver import run_policy_for_repo
from skep.supervisor.projects import (
    BUILTIN_POLICY_GROUPS,
    GROUPABLE_POLICY_KEYS,
    POLICY_GROUPS_SETTING,
    delete_policy_group_record,
    save_policy_group,
    stored_policy_groups,
    validate_policy_group,
)
from skep.supervisor.serve.actions import effective_policy_view
from skep.supervisor.serve.actions import set_policy_group as set_policy_group_action
from skep.supervisor.serve.settings import ConfigHolder


def _project_with_groups(
    store: RunStore,
    repo: Path,
    *,
    groups: list[str],
    project_id: str = "grouped",
    **extra_policy: object,
) -> None:
    store.add_project_policy(
        project_id=project_id,
        name=f"{project_id} project",
        strategy="trusted_local_dev",
        phase="build",
        policy={
            "default_execution_mode": "workspace",
            "policy_groups": groups,
            **extra_policy,
        },
    )
    store.add_project_binding(
        project_id=project_id, binding_kind="repo_path", binding_value=str(repo)
    )


def test_groups_round_trip_and_builtins_resolve_without_a_row(
    config: SupervisorConfig,
) -> None:
    store = RunStore(config.db_path)
    try:
        groups = stored_policy_groups(store)
        assert set(BUILTIN_POLICY_GROUPS) <= set(groups)
        assert ["uv", "sync"] in groups["python-bootstrap"]["allowed_shell_commands"]

        save_policy_group(store, "npm-api", {"default_network": ["api.npmjs.example"]})
        groups = stored_policy_groups(store)
        assert groups["npm-api"] == {"default_network": ["api.npmjs.example"]}

        # Editing a builtin materializes the copy; deleting it reverts.
        save_policy_group(
            store, "node-dev", {"default_network": ["registry.npmjs.org", "extra.example"]}
        )
        assert "extra.example" in stored_policy_groups(store)["node-dev"]["default_network"]
        delete_policy_group_record(store, "node-dev")
        assert stored_policy_groups(store)["node-dev"] == BUILTIN_POLICY_GROUPS["node-dev"]
        # v106-F9: yarn's registry rides the builtin — a yarn install got a
        # 403 from the egress proxy on an npm-only allowlist in the field.
        assert "registry.yarnpkg.com" in BUILTIN_POLICY_GROUPS["node-dev"]["default_network"]
    finally:
        store.close()


def test_trust_ramp_keys_are_ungroupable_and_the_refusal_names_the_set() -> None:
    for ramp_key in (
        "auto_apply_verified_patch",
        "auto_apply_branch",
        "allow_git_mutation",
        "auto_dispatch_allowed",
        "trusted_workspace_roots",
    ):
        assert ramp_key not in GROUPABLE_POLICY_KEYS
        with pytest.raises(ValueError) as refused:
            validate_policy_group("ramp", {ramp_key: True})
        assert "groupable keys" in str(refused.value)
        assert "trust-ramp" in str(refused.value)


def test_group_shell_prefixes_pass_the_shared_danger_guard() -> None:
    """I4: the never-grantable list vets group contents at write time exactly
    like project policy — a group can never carry `git push`."""
    with pytest.raises(ValueError) as refused:
        validate_policy_group("evil", {"allowed_shell_commands": [["git", "push"]]})
    assert "remote git commands cannot be allowlisted" in str(refused.value)


def test_group_names_and_shapes_are_validated() -> None:
    for bad_name in ("", "X", "UPPER", "9start", "has space", "a" * 40):
        with pytest.raises(ValueError, match="policy group names"):
            validate_policy_group(bad_name, {"default_network": []})
    with pytest.raises(ValueError, match="non-empty object"):
        validate_policy_group("empty", {})
    with pytest.raises(ValueError, match="list of strings"):
        validate_policy_group("shaped", {"default_network": "pypi.org"})


def test_groups_compose_live_and_the_project_beats_them(
    config: SupervisorConfig, repo: Path
) -> None:
    store = RunStore(config.db_path)
    try:
        save_policy_group(
            store,
            "npm-api",
            {"default_network": ["api.npmjs.example"], "default_max_actions": 5},
        )
        save_policy_group(
            store,
            "budgets",
            {"default_max_actions": 7, "default_network": ["api.npmjs.example", "b.example"]},
        )
        _project_with_groups(
            store,
            repo,
            groups=["python-bootstrap", "npm-api", "budgets"],
            default_network=["own.example"],
        )

        effective = run_policy_for_repo(store, config, repo)
        # List union across project's own value and every group, no dupes.
        network = effective["default_network"]
        assert "own.example" in network
        assert "pypi.org" in network and "api.npmjs.example" in network
        assert "b.example" in network
        assert network.count("api.npmjs.example") == 1
        assert ["uv", "sync"] in effective["allowed_shell_commands"]
        # Scalar: attach order wins among groups (budgets after npm-api).
        assert effective["default_max_actions"] == 7

        # LIVE composition: edit the group, the next resolve sees it.
        save_policy_group(
            store, "npm-api", {"default_network": ["api.npmjs.example", "late.example"]}
        )
        assert "late.example" in run_policy_for_repo(store, config, repo)["default_network"]

        # The project's own scalar beats any group.
        _project_with_groups(
            store,
            repo,
            groups=["budgets"],
            project_id="grouped",
            default_max_actions=3,
        )
        assert run_policy_for_repo(store, config, repo)["default_max_actions"] == 3
    finally:
        store.close()


def test_dangling_group_fails_the_dispatch_closed(config: SupervisorConfig, repo: Path) -> None:
    store = RunStore(config.db_path)
    try:
        _project_with_groups(store, repo, groups=["ghost"])
        # The peek path stays usable (breadcrumb, not a crash) …
        assert run_policy_for_repo(store, config, repo)["_missing_policy_groups"] == ["ghost"]
        # … and the resolve path refuses, naming the group and the fix (I9).
        view = effective_policy_view(ConfigHolder(config, store), store, str(repo))
        assert "ghost" in view["error"]
        assert "set_policy_group" in view["error"]
    finally:
        store.close()


def test_grouped_engine_still_hits_the_verify_pin_guard(
    config: SupervisorConfig, repo: Path
) -> None:
    """I2/I5: composition happens BEFORE the v90 guard block — a group cannot
    smuggle an external engine past the pinned-verify requirement."""
    store = RunStore(config.db_path)
    try:
        save_policy_group(store, "cc", {"coding_engine": "claude_code"})
        _project_with_groups(store, repo, groups=["cc"])
        view = effective_policy_view(ConfigHolder(config, store), store, str(repo))
        assert "verify_command" in view["error"]
    finally:
        store.close()


def test_delete_while_attached_refuses_naming_the_projects(
    config: SupervisorConfig, repo: Path
) -> None:
    store = RunStore(config.db_path)
    try:
        save_policy_group(store, "shared", {"default_network": ["x.example"]})
        _project_with_groups(store, repo, groups=["shared"])
        with pytest.raises(ValueError) as refused:
            delete_policy_group_record(store, "shared")
        assert "grouped" in str(refused.value)
        assert "detach" in str(refused.value)
    finally:
        store.close()


def _mutate(
    store: RunStore, config: SupervisorConfig, name: str, args: dict[str, object]
) -> object:
    from typing import cast

    from skep.supervisor.serve.jobs import Dispatcher
    from skep.supervisor.serve.tools import execute_mutation

    return execute_mutation(
        name,
        args,
        store=store,
        holder=ConfigHolder(config, store),
        runner=cast(Dispatcher, None),
        actor="operator-command",
    )


def test_group_verbs_ride_the_mutation_path(config: SupervisorConfig, repo: Path) -> None:
    """v97-F3: CRUD + attach/detach through execute_mutation under
    actor operator-command — the same path a confirmed card takes."""
    store = RunStore(config.db_path)
    try:
        # v95-F2 regression guard: a stringified policy object still lands.
        created = _mutate(
            store,
            config,
            "set_policy_group",
            {"name": "npm-api", "policy": '{"default_network": ["api.npmjs.example"]}'},
        )
        assert created["policy"] == {"default_network": ["api.npmjs.example"]}  # type: ignore[index]
        assert created["updated_in_place"] is False  # type: ignore[index]

        _project_with_groups(store, repo, groups=[])
        attached = _mutate(
            store,
            config,
            "attach_policy_group",
            {"project_id": "grouped", "name": "npm-api"},
        )
        assert attached["policy_groups"] == ["npm-api"]  # type: ignore[index]
        again = _mutate(
            store,
            config,
            "attach_policy_group",
            {"project_id": "grouped", "name": "npm-api"},
        )
        assert again["already_attached"] is True  # type: ignore[index]

        # In-place update names the projects that will follow it (I8).
        updated = _mutate(
            store,
            config,
            "set_policy_group",
            {"name": "npm-api", "policy": {"default_network": ["api.npmjs.example", "b.example"]}},
        )
        assert updated["updated_in_place"] is True  # type: ignore[index]
        assert updated["attached_projects"] == ["grouped"]  # type: ignore[index]

        from skep.supervisor.serve.actions import list_policy_groups

        groups = list_policy_groups(store)["groups"]
        entry = next(g for g in groups if g["name"] == "npm-api")
        assert entry["attached_projects"] == ["grouped"]
        assert entry["builtin"] is False

        detached = _mutate(
            store,
            config,
            "detach_policy_group",
            {"project_id": "grouped", "name": "npm-api"},
        )
        assert detached["policy_groups"] == []  # type: ignore[index]
        with pytest.raises(ValueError, match="does not attach"):
            _mutate(
                store,
                config,
                "detach_policy_group",
                {"project_id": "grouped", "name": "npm-api"},
            )

        deleted = _mutate(store, config, "delete_policy_group", {"name": "npm-api"})
        assert deleted == {"deleted": "npm-api"}
    finally:
        store.close()


def test_fork_is_copy_on_write_and_validates_before_writing(
    config: SupervisorConfig, repo: Path
) -> None:
    """v97-F3: the fork leaves the source byte-identical, repoints only the
    named project, and every refusal happens BEFORE the first write."""
    store = RunStore(config.db_path)
    try:
        save_policy_group(store, "npm-api", {"default_network": ["api.npmjs.example"]})
        _project_with_groups(store, repo, groups=["npm-api"], project_id="frontend")
        _project_with_groups(store, repo, groups=["npm-api"], project_id="backend")
        source_before = stored_policy_groups(store)["npm-api"]

        result = _mutate(
            store,
            config,
            "set_policy_group",
            {
                "name": "npm-api-2",
                "policy": {"default_network": ["api.npmjs.example", "extra.example"]},
                "fork_from": "npm-api",
                "repoint_project": "frontend",
            },
        )
        assert result["forked_from"] == "npm-api"  # type: ignore[index]
        assert result["source_untouched"] is True  # type: ignore[index]
        assert result["repointed_project"] == "frontend"  # type: ignore[index]

        groups = stored_policy_groups(store)
        assert groups["npm-api"] == source_before  # byte-identical source
        assert "extra.example" in groups["npm-api-2"]["default_network"]
        front = store.get_project_policy("frontend")
        back = store.get_project_policy("backend")
        assert front is not None and front.policy["policy_groups"] == ["npm-api-2"]
        assert back is not None and back.policy["policy_groups"] == ["npm-api"]

        # Refusals, all pre-write: unknown source; taken name; unattached
        # repoint; repoint without fork.
        with pytest.raises(ValueError, match="to fork"):
            set_policy_group_action(store, name="x", policy={}, fork_from="ghost")
        with pytest.raises(ValueError, match="fresh name"):
            set_policy_group_action(store, name="npm-api", policy={}, fork_from="npm-api-2")
        _project_with_groups(store, repo, groups=[], project_id="loose")
        before = stored_policy_groups(store)
        with pytest.raises(ValueError, match="not attached"):
            set_policy_group_action(
                store,
                name="npm-api-3",
                policy={},
                fork_from="npm-api",
                repoint_project="loose",
            )
        assert "npm-api-3" not in stored_policy_groups(store)  # nothing written
        assert stored_policy_groups(store) == before
        with pytest.raises(ValueError, match="fork_from"):
            set_policy_group_action(
                store, name="x", policy={"default_network": []}, repoint_project="frontend"
            )
    finally:
        store.close()


def test_setup_attaches_groups_and_suggests_from_the_toolchain(
    config: SupervisorConfig, repo: Path
) -> None:
    """v97-F4: groups= attaches at setup (same sugar shape as engine=), a
    typo refuses naming the known set (I9), and the toolchain sniff SUGGESTS
    builtin groups without ever attaching them silently (I6)."""
    from fastapi import HTTPException

    from skep.supervisor.serve.registry import preview_project_setup

    (repo / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
    store = RunStore(config.db_path)
    try:
        preview = preview_project_setup(
            root=config.home.parent / "repos",
            run_store=store,
            project_id="sniffed",
            name="Sniffed",
            strategy="trusted_local_dev",
            phase="build",
            pack_name=None,
            repo_path=str(repo),
            repo_slug=None,
            template_names=[],
            policy_overrides={},
        )
        assert preview["suggested_policy_groups"] == ["python-bootstrap"]

        result = _mutate(
            store,
            config,
            "setup_project",
            {
                "project_id": "sniffed",
                "name": "Sniffed",
                "strategy": "trusted_local_dev",
                "repo_path": str(repo),
                "groups": ["python-bootstrap"],
            },
        )
        assert result["policy"]["policy_groups"] == ["python-bootstrap"]  # type: ignore[index]
        # An attached suggestion stops being suggested (no nagging, I8).
        assert result["suggested_policy_groups"] == []  # type: ignore[index]
        record = store.get_project_policy("sniffed")
        assert record is not None
        assert record.policy["policy_groups"] == ["python-bootstrap"]

        with pytest.raises(HTTPException) as refused:
            _mutate(
                store,
                config,
                "setup_project",
                {
                    "project_id": "typo",
                    "name": "Typo",
                    "strategy": "trusted_local_dev",
                    "repo_path": str(repo),
                    "groups": ["python-bootstrp"],
                },
            )
        assert "python-bootstrp" in refused.value.detail
        assert "known:" in refused.value.detail
        assert store.get_project_policy("typo") is None  # refusal wrote nothing
    finally:
        store.close()


def test_effective_policy_view_carries_group_provenance(
    config: SupervisorConfig, repo: Path
) -> None:
    """v97-F5 (I8): the one shared policy read names the attached groups AND
    what each contributes — "why is this host allowed" has an answer."""
    store = RunStore(config.db_path)
    try:
        save_policy_group(store, "npm-api", {"default_network": ["api.npmjs.example"]})
        _project_with_groups(store, repo, groups=["npm-api"])
        view = effective_policy_view(ConfigHolder(config, store), store, str(repo))
        assert "api.npmjs.example" in view["network"]
        assert view["policy_groups"] == [
            {"name": "npm-api", "grants": {"default_network": ["api.npmjs.example"]}}
        ]
    finally:
        store.close()


def test_group_http_routes_are_operator_direct(config: SupervisorConfig, repo: Path) -> None:
    """v97-F5: the #/policies UI rides plain routes (the authenticated UI is
    the human); the fork route is ONE atomic request, and refusals are 400s
    that teach, never tracebacks (I9)."""
    from .conftest import serve_client

    client = serve_client(config)
    put = client.put("/api/policy-groups/npm-api", json={"default_network": ["a.example"]})
    assert put.status_code == 200
    assert put.json()["updated_in_place"] is False

    listed = client.get("/api/policy-groups").json()["groups"]
    assert {g["name"] for g in listed} >= {"npm-api", "python-bootstrap", "node-dev"}

    store = RunStore(config.db_path)
    try:
        _project_with_groups(store, repo, groups=["npm-api"], project_id="frontend")
    finally:
        store.close()

    fork = client.post(
        "/api/policy-groups/npm-api/fork",
        json={
            "new_name": "npm-api-2",
            "policy": {"default_network": ["a.example", "b.example"]},
            "repoint_project": "frontend",
        },
    )
    assert fork.status_code == 200
    assert fork.json()["source_untouched"] is True
    assert fork.json()["repointed_project"] == "frontend"

    # Attached (frontend now on npm-api-2) → delete refuses naming it, 400.
    refused = client.delete("/api/policy-groups/npm-api-2")
    assert refused.status_code == 400
    assert "frontend" in refused.json()["detail"]
    # A bad fork is a 400 naming the problem, and writes nothing.
    bad = client.post("/api/policy-groups/ghost/fork", json={"new_name": "x", "policy": {}})
    assert bad.status_code == 400
    assert "ghost" in bad.json()["detail"]

    gone = client.delete("/api/policy-groups/npm-api")
    assert gone.status_code == 200


def test_ui_carries_the_groups_section_and_the_fork_toggle() -> None:
    """v97-F5 pins (I11: no-build static app): the groups editor, the
    copy-on-write toggle with its context-sensitive default, the project-view
    handoff, and the strip popover's groups line."""
    static_dir = (
        Path(__file__).resolve().parents[2] / "src" / "skep" / "supervisor" / "serve" / "static"
    )
    app_js = (static_dir / "app.js").read_text()
    css = (static_dir / "style.css").read_text()
    for needle in (
        'api("GET", "/api/policy-groups")',
        "Save as new group",
        "forkBox.checked = Boolean(fromProject && group.attached_projects.length > 1)",
        '"skep-group-edit"',
        "/fork`",
        "repoint_project: repoint.value || null",
        "window.confirm(`Update ${group.name} IN PLACE",
        "policy.policy_groups?.length",
    ):
        assert needle in app_js, needle
    for selector in (".policy-group-row", ".project-group-edit"):
        assert selector in css, selector


def test_group_verbs_pass_the_operator_command_gate(config: SupervisorConfig, repo: Path) -> None:
    """v97-F6 (acceptance find, I10): the F3 tests proved the executor but
    never the COMMAND_TOOL_NAMES gate in front of it — the v96-F4 lesson
    relearned. An attach proposed and confirmed through the REAL commands
    endpoints resolves, and the next policy read carries the group."""
    from skep.supervisor.serve.tools import COMMAND_TOOL_NAMES

    from .conftest import serve_client

    for verb in (
        "set_policy_group",
        "delete_policy_group",
        "attach_policy_group",
        "detach_policy_group",
    ):
        assert verb in COMMAND_TOOL_NAMES

    store = RunStore(config.db_path)
    try:
        save_policy_group(store, "npm-api", {"default_network": ["api.npmjs.example"]})
        _project_with_groups(store, repo, groups=[])
    finally:
        store.close()

    client = serve_client(config)
    chat_id = client.post("/api/chats", json={"title": "groups"}).json()["chat_id"]
    action = client.post(
        f"/api/chats/{chat_id}/commands",
        json={"tool": "attach_policy_group", "args": {"project_id": "grouped", "name": "npm-api"}},
    )
    assert action.status_code == 201, action.text
    confirmed = client.post(
        f"/api/chats/{chat_id}/commands/{action.json()['action_id']}/confirm"
    ).json()
    assert confirmed["ok"] is True
    assert confirmed["result"]["policy_groups"] == ["npm-api"]
    view = client.get(f"/api/repos/{repo}/effective-policy").json()
    assert "api.npmjs.example" in view["network"]
    assert [g["name"] for g in view["policy_groups"]] == ["npm-api"]


def test_delete_refusals_teach(config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        with pytest.raises(ValueError, match="builtin policy group"):
            delete_policy_group_record(store, "python-bootstrap")
        with pytest.raises(ValueError, match="known:"):
            delete_policy_group_record(store, "ghost")

        save_policy_group(store, "temp", {"default_network": ["x.example"]})
        delete_policy_group_record(store, "temp")
        assert "temp" not in stored_policy_groups(store)
        assert "temp" not in (store.get_setting(POLICY_GROUPS_SETTING) or {})
    finally:
        store.close()


# ---- v112-F2: the bundle is offered at decision time --------------------------


def test_covering_policy_group_names_the_unattached_bundle(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    """pypi.org is python-bootstrap's key; a bound project without the group
    gets the offer, and shell prefixes match by argv prefix."""
    from skep.supervisor.serve.actions import covering_policy_group

    store = RunStore(config.db_path)
    try:
        repo = tmp_path / "proj"
        repo.mkdir()
        _project_with_groups(store, repo, groups=[])

        assert (
            covering_policy_group(
                store, action="network.fetch", resource="pypi.org", repo=str(repo)
            )
            == "python-bootstrap"
        )
        assert (
            covering_policy_group(
                store,
                action="shell.run",
                resource="uv pip install requests",
                repo=str(repo),
            )
            == "python-bootstrap"
        )
        # A key no group bundles, and an unbound repo: nothing to offer.
        assert (
            covering_policy_group(
                store, action="network.fetch", resource="example.com", repo=str(repo)
            )
            is None
        )
        assert (
            covering_policy_group(
                store, action="network.fetch", resource="pypi.org", repo=str(tmp_path / "unbound")
            )
            is None
        )
    finally:
        store.close()


def test_an_attached_group_offers_nothing(config: SupervisorConfig, tmp_path: Path) -> None:
    from skep.supervisor.serve.actions import covering_policy_group

    store = RunStore(config.db_path)
    try:
        repo = tmp_path / "proj"
        repo.mkdir()
        _project_with_groups(store, repo, groups=["python-bootstrap"])
        assert (
            covering_policy_group(
                store, action="network.fetch", resource="pypi.org", repo=str(repo)
            )
            is None
        )
    finally:
        store.close()


def test_remember_with_attach_group_attaches_the_bundle_not_the_crumb(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    """v112-F2: attach_group routes through the existing attach (I5) and the
    raw key is NOT written into the project overlay — the group carries it."""
    from skep.supervisor.serve.actions import remember_ledger_entry

    store = RunStore(config.db_path)
    holder = ConfigHolder(config, store)
    try:
        repo = tmp_path / "proj"
        repo.mkdir()
        _project_with_groups(store, repo, groups=[])

        result = remember_ledger_entry(
            store,
            holder,
            action="network.fetch",
            resource="pypi.org",
            repo=str(repo),
            attach_group="python-bootstrap",
        )
        assert result["attached_group"] == "python-bootstrap"

        record = store.get_project_policy("grouped")
        assert record is not None
        assert record.policy.get("policy_groups") == ["python-bootstrap"]
        # The crumb stays out of the overlay; composition carries it instead.
        assert "pypi.org" not in (record.policy.get("default_network") or [])
        composed = run_policy_for_repo(store, config, repo)
        assert "pypi.org" in composed["default_network"]
    finally:
        store.close()


def test_remember_refuses_a_group_that_does_not_cover(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    from fastapi import HTTPException

    from skep.supervisor.serve.actions import remember_ledger_entry

    store = RunStore(config.db_path)
    holder = ConfigHolder(config, store)
    try:
        repo = tmp_path / "proj"
        repo.mkdir()
        _project_with_groups(store, repo, groups=[])
        with pytest.raises(HTTPException) as excinfo:
            remember_ledger_entry(
                store,
                holder,
                action="network.fetch",
                resource="example.com",
                repo=str(repo),
                attach_group="python-bootstrap",
            )
        assert "does not cover" in str(excinfo.value.detail)
    finally:
        store.close()
