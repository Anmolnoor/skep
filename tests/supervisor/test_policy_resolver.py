"""Unit tests for the run-policy network merge (v19-F2).

The provider host must land in every *coding* run's allowlist regardless of how
network was specified, unless the run opted into allow-all (``["*"]``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skep.supervisor import RunStore
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.policy_resolver import ResolvedRunPolicy, resolve_run_policy


def _resolve(
    store: RunStore,
    config: object,
    repo: Path,
    *,
    caste: str,
    network: list[str] | None,
    extra: list[str],
) -> ResolvedRunPolicy:
    return resolve_run_policy(
        store=store,
        config=config,  # type: ignore[arg-type]
        repo=repo,
        caste=caste,
        network=network,
        env_allowlist=None,
        wall_clock_seconds=None,
        max_iterations=None,
        max_actions=None,
        max_provider_calls=None,
        execution_mode="sandbox",
        extra_network_hosts=extra,
    )


@pytest.mark.parametrize(
    ("caste", "network", "extra", "expected"),
    [
        # coding: the provider host is merged whether or not network was set.
        ("coding", None, ["ollama.com"], ["ollama.com"]),
        ("coding", ["github.com"], ["ollama.com"], ["github.com", "ollama.com"]),
        ("coding", [], ["ollama.com"], ["ollama.com"]),
        # allow-all already reaches everything; leave it untouched.
        ("coding", ["*"], ["ollama.com"], ["*"]),
        # already present -> no duplicate.
        ("coding", ["ollama.com"], ["ollama.com"], ["ollama.com"]),
        # no configured provider -> nothing to merge.
        ("coding", None, [], []),
        # v72-F2: the document caste drafts through the provider — same merge.
        ("document", [], ["ollama.com"], ["ollama.com"]),
        ("document", ["github.com"], ["ollama.com"], ["github.com", "ollama.com"]),
        # provider-less casts never get the provider merge.
        ("audit", None, ["ollama.com"], []),
        ("audit", ["github.com"], ["ollama.com"], ["github.com"]),
    ],
)
def test_provider_host_merge(
    tmp_path: Path,
    repo: Path,
    caste: str,
    network: list[str] | None,
    extra: list[str],
    expected: list[str],
) -> None:
    config = build_config(tmp_path / "home", None)
    store = RunStore(config.db_path)
    try:
        resolved = _resolve(store, config, repo, caste=caste, network=network, extra=extra)
    finally:
        store.close()
    assert resolved.permissions.network == expected


def test_merge_is_sorted_and_deduped_regardless_of_input_order(
    tmp_path: Path, repo: Path
) -> None:
    """v19-F11: equal inputs give a byte-equal, sorted, deduped allowlist."""
    config = build_config(tmp_path / "home", None)
    store = RunStore(config.db_path)
    try:
        one = _resolve(
            store,
            config,
            repo,
            caste="coding",
            network=["github.com"],
            extra=["ollama.com", "github.com", "api.openai.com"],
        )
        two = _resolve(
            store,
            config,
            repo,
            caste="coding",
            network=["github.com"],
            extra=["api.openai.com", "ollama.com", "github.com"],
        )
    finally:
        store.close()
    expected = ["api.openai.com", "github.com", "ollama.com"]
    assert one.permissions.network == expected
    # Different extra-host ordering resolves to the identical list.
    assert two.permissions.network == expected


def test_managed_repo_is_trusted_by_construction(tmp_path: Path, repo: Path) -> None:
    """v23-F1: a repo under <SKEP_HOME>/repos gets the shell allowlist in
    workspace mode with NO trusted_workspace_roots configured — registering it
    was the trust decision."""
    import shutil

    config = build_config(tmp_path / "home", None)
    managed = config.home.parent / "repos" / "proj"
    managed.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(repo, managed)
    store = RunStore(config.db_path)
    try:
        store.set_setting("allowed_shell_commands", [["pytest", "-q"]])
        store.set_setting("default_execution_mode", "workspace")
        resolved = resolve_run_policy(
            store=store,
            config=config,
            repo=managed,
            caste="coding",
            network=None,
            env_allowlist=None,
            wall_clock_seconds=None,
            max_iterations=None,
            max_actions=None,
            max_provider_calls=None,
            execution_mode="workspace",
        )
    finally:
        store.close()
    assert resolved.permissions.shell_allowlist == [["pytest", "-q"]]
    assert resolved.trust_root == str(config.home.parent / "repos")


def test_unmanaged_repo_without_roots_still_gets_no_allowlist(
    tmp_path: Path, repo: Path
) -> None:
    """v23-F1: outside repos_root, operator-set roots keep governing —
    no roots configured means no allowlist in workspace mode."""
    config = build_config(tmp_path / "home", None)
    store = RunStore(config.db_path)
    try:
        store.set_setting("allowed_shell_commands", [["pytest", "-q"]])
        store.set_setting("default_execution_mode", "workspace")
        resolved = resolve_run_policy(
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
    finally:
        store.close()
    assert resolved.permissions.shell_allowlist == []
    assert resolved.trust_root is None


def _resolve_for_project(
    tmp_path: Path, repo: Path, *, strategy: str, execution_mode: str, network: list[str] | None
) -> ResolvedRunPolicy:
    config = build_config(tmp_path / "home", None)
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="p1",
            name="P1",
            strategy=strategy,
            phase="build",
            policy={"default_execution_mode": execution_mode},
        )
        store.add_project_binding(
            project_id="p1", binding_kind="repo_path", binding_value=str(repo)
        )
        return resolve_run_policy(
            store=store,
            config=config,
            repo=repo,
            caste="coding",
            network=network,
            env_allowlist=None,
            wall_clock_seconds=None,
            max_iterations=None,
            max_actions=None,
            max_provider_calls=None,
            execution_mode=execution_mode,
        )
    finally:
        store.close()


def test_trusted_dev_workspace_run_gets_registry_hosts(tmp_path: Path, repo: Path) -> None:
    """v23-F5: trusted_local_dev + workspace + no explicit network -> registries."""
    resolved = _resolve_for_project(
        tmp_path, repo, strategy="trusted_local_dev", execution_mode="workspace", network=None
    )
    for host in ("pypi.org", "files.pythonhosted.org", "registry.npmjs.org"):
        assert host in resolved.permissions.network


def test_registry_hosts_follow_enforceability_and_strategy(
    tmp_path: Path, repo: Path
) -> None:
    """v23-F5 + v28: registries reach a sandbox run ONLY where the backend can
    enforce a domain list (both native backends now can); non-trusted
    strategies stay fail-closed, and an explicit network list is never widened."""
    from skep.supervisor.policy_resolver import per_domain_egress_enforceable

    sandboxed = _resolve_for_project(
        tmp_path, repo, strategy="trusted_local_dev", execution_mode="sandbox", network=None
    )
    if per_domain_egress_enforceable():
        # v28: bubblewrap/seatbelt pin egress to the proxy, so the merge is honest.
        assert "pypi.org" in sandboxed.permissions.network
    else:
        # A non-enforcing backend must never be handed a list it cannot enforce.
        assert "pypi.org" not in sandboxed.permissions.network
    public = _resolve_for_project(
        tmp_path, repo, strategy="public_free", execution_mode="workspace", network=None
    )
    assert "pypi.org" not in public.permissions.network
    explicit = _resolve_for_project(
        tmp_path,
        repo,
        strategy="trusted_local_dev",
        execution_mode="workspace",
        network=["github.com"],
    )
    assert "pypi.org" not in explicit.permissions.network


# -- v52-F2: the operator policy — the Queen's standing rules ------------------


def test_operator_policy_default_allows_search_denies_connect(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "supervisor.sqlite3")
    try:
        from skep.supervisor.policy_resolver import resolve_operator_policy

        policy = resolve_operator_policy(store)
        search = policy.decision("network", "search", "ddgs")
        assert search.verdict == "allow"
        assert search.decided_by == "operator-default/net:search"
        assert policy.decision("network", "connect", "example.com").verdict == "deny"
    finally:
        store.close()


def test_operator_document_rules_compose_with_the_global_document(tmp_path: Path) -> None:
    """Operator-document rules decide for the Queen; global rules keep their
    effect; a same-specificity global deny beats an operator allow."""
    from skep.supervisor.policy_resolver import resolve_operator_policy
    from skep.supervisor.policy_schema import (
        OPERATOR_POLICY_SETTINGS_KEY,
        POLICY_DOCUMENT_SETTINGS_KEY,
        PolicyDocument,
    )

    store = RunStore(tmp_path / "supervisor.sqlite3")
    try:
        store.set_setting(
            POLICY_DOCUMENT_SETTINGS_KEY,
            PolicyDocument.model_validate(
                {
                    "template": "locked-down",
                    "scopes": [
                        {
                            "scope": "filesystem",
                            "deny": [
                                {"rule_id": "no-vault", "action": "read", "pattern": "/vault/*"}
                            ],
                        }
                    ],
                }
            ).model_dump_json(),
        )
        store.set_setting(
            OPERATOR_POLICY_SETTINGS_KEY,
            PolicyDocument.model_validate(
                {
                    "scopes": [
                        {
                            "scope": "filesystem",
                            "allow": [
                                {"rule_id": "op:tmp", "action": "read", "pattern": "/tmp/*"},
                                {"rule_id": "op:vault", "action": "read", "pattern": "/vault/*"},
                            ],
                        }
                    ]
                }
            ).model_dump_json(),
        )

        policy = resolve_operator_policy(store)
        # The operator rule decides for the Queen.
        allowed = policy.decision("filesystem", "read", "/tmp/notes.txt")
        assert allowed.verdict == "allow"
        assert allowed.decided_by == "locked-down/op:tmp"
        # Deny wins the tie against the operator allow at equal specificity.
        assert policy.decision("filesystem", "read", "/vault/key").verdict == "deny"
    finally:
        store.close()
