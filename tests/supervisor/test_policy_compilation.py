"""v40-F7 (v36-F3): resolve_run_policy compiles the unified schema.

Zero visible change is proven by the untouched policy regression corpus and
capability matrix; these tests pin the new plumbing — Permissions fields are
views of the compiled document, the document is decision-capable, and no new
shadow Permissions writer can appear unnoticed."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.policy_resolver import resolve_run_policy
from skep.supervisor.policy_schema import decide


def _resolve(
    config: SupervisorConfig, repo: Path, store: RunStore, **overrides: Any
) -> Any:
    kwargs: dict[str, Any] = dict(
        store=store,
        config=config,
        repo=repo,
        caste="coding",
        network=None,
        env_allowlist=None,
        wall_clock_seconds=None,
        max_iterations=None,
        max_actions=None,
        max_provider_calls=None,
        execution_mode="workspace",
    )
    kwargs.update(overrides)
    return resolve_run_policy(**kwargs)


def test_permissions_are_views_of_the_compiled_document(
    repo: Path, config: SupervisorConfig
) -> None:
    store = RunStore(config.db_path)
    try:
        resolved = _resolve(config, repo, store, network=["pypi.org", "api.example.com"])
    finally:
        store.close()
    document = resolved.document
    assert document is not None
    assert document.template == "global"  # unbound repo → the global template
    network_scope = next(s for s in document.scopes if s.scope == "network")
    assert [rule.pattern for rule in network_scope.allow] == resolved.permissions.network
    assert resolved.permissions.network == ["api.example.com", "pypi.org"]  # v19-F11 sort

    # The compiled document is decision-capable (F8 reads decided_by off it).
    allowed = decide(
        resolved.resolved_scopes, "network", "connect", "pypi.org", template=document.template
    )
    assert allowed.verdict == "allow"
    assert allowed.decided_by == "global/net:pypi.org"
    denied = decide(
        resolved.resolved_scopes, "network", "connect", "evil.example", template=document.template
    )
    assert denied.verdict == "deny"
    assert denied.decided_by == "global/default-deny"


def test_project_shell_allowlist_flows_through_the_document(
    repo: Path, config: SupervisorConfig
) -> None:
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="compiled-project",
            name="compiled project",
            strategy="trusted_local_dev",
            phase="build",
            policy={
                "default_execution_mode": "workspace",
                "trusted_workspace_roots": [str(repo.parent)],
                "allowed_shell_commands": [["uv", "run", "pytest"], ["git", "status"]],
            },
        )
        store.add_project_binding(
            project_id="compiled-project",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
        resolved = _resolve(config, repo, store)
    finally:
        store.close()
    assert resolved.permissions.shell_allowlist == [
        ["uv", "run", "pytest"],
        ["git", "status"],
    ]
    assert resolved.document is not None
    assert resolved.document.template == "trusted_local_dev"
    lifted = decide(
        resolved.resolved_scopes,
        "shell",
        "run",
        "uv run pytest -q",
        template=resolved.document.template,
    )
    assert lifted.verdict == "allow"
    assert lifted.decided_by == "trusted_local_dev/shell:uv run pytest"
    unmatched = decide(
        resolved.resolved_scopes,
        "shell",
        "run",
        "curl evil.example",
        template=resolved.document.template,
    )
    assert unmatched.verdict == "deny"  # the schema denies; the worker gate escalates


def test_no_new_shadow_permissions_writer_can_appear() -> None:
    """The v36-F3 teeth, house lockstep idiom: every Permissions constructor
    site in src is named here; a new one must justify itself in this list.
    The dispatch path's writer is policy_resolver — actions.submit_run and
    the serve layer never construct Permissions themselves."""
    root = Path(__file__).resolve().parents[2] / "src" / "skep"
    constructor = re.compile(r"(?<!class )Permissions\(")
    known = {
        "supervisor/contracts_io.py",  # the contract default envelope
        "supervisor/scheduler.py",  # template-bound scheduled runs
        "supervisor/templates.py",  # template instantiation
        "supervisor/cli_cmds.py",  # run --minimal deny-all bootstrap
        "supervisor/policy_resolver.py",  # THE dispatch-path writer
        "worker_contract/task.py",  # the model's own validators/tests
    }
    holders = {
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if constructor.search(path.read_text(encoding="utf-8"))
    }
    assert holders <= known, f"new Permissions writer(s): {sorted(holders - known)}"
    assert "supervisor/policy_resolver.py" in holders
