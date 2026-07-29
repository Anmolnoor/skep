"""v73-F11: never card into a missing path.

The field failure: a dispatch at ~/homelab failed "not a git repository"
AFTER its card was confirmed, and workon on the SAME absent path failed
"not a directory" — two stories, one missing directory, two burned
confirmations. One shared resolver now refuses at proposal time with one
string from both verbs.
"""

from __future__ import annotations

from pathlib import Path

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import mutation_execution_decision

from .conftest import git, serve_client


def test_missing_path_is_refused_at_proposal_time_with_one_story(
    repo: Path, config: SupervisorConfig
) -> None:
    gone = repo.parent / "homelab"
    store = RunStore(config.db_path)
    try:
        decision = mutation_execution_decision(
            "dispatch_run",
            {"repo": str(gone), "instructions": "plan the homelab"},
            store=store,
            holder=ConfigHolder(config, store),
        )
    finally:
        store.close()
    assert decision is not None
    assert decision.verdict == "deny"  # a deny never cards (v40-F10)
    assert decision.reason == "dispatch.deny.repo_path_missing"
    assert decision.detail is not None
    assert "does not exist on this machine" in decision.detail
    assert "list_repos" in decision.detail

    # workon speaks the SAME string for the SAME absent path.
    client = serve_client(config)
    preview = client.post(
        "/api/workon/preview",
        json={"path": str(gone), "pack": "trusted_local_dev", "phase": "build"},
    )
    assert preview.status_code == 400
    assert preview.json()["detail"] == decision.detail


def test_existing_non_git_path_keeps_its_card_and_message(
    repo: Path, config: SupervisorConfig
) -> None:
    plain = repo.parent / "plain-dir"
    plain.mkdir()
    store = RunStore(config.db_path)
    try:
        decision = mutation_execution_decision(
            "dispatch_run",
            {"repo": str(plain), "instructions": "x"},
            store=store,
            holder=ConfigHolder(config, store),
        )
    finally:
        store.close()
    assert decision is not None
    assert decision.verdict == "require_approval"  # today's card, unchanged
    assert decision.reason == "dispatch.require_approval.repo_not_bound_git_project"


def test_registered_slug_dispatches_are_unaffected(
    repo: Path, config: SupervisorConfig
) -> None:
    root = config.home.parent / "repos"
    clone = root / "known-repo"
    clone.mkdir(parents=True)
    git(clone, "init", "-q")
    store = RunStore(config.db_path)
    try:
        decision = mutation_execution_decision(
            "dispatch_run",
            {"repo": "known-repo", "instructions": "x"},
            store=store,
            holder=ConfigHolder(config, store),
        )
    finally:
        store.close()
    assert decision is not None
    assert decision.verdict != "deny"


def test_missing_path_deny_names_the_registered_repos(
    repo: Path, config: SupervisorConfig
) -> None:
    """v81-F9: the deny carries the answer — the registered slugs — not just
    a pointer to the tool that has it."""
    root = config.home.parent / "repos"
    clone = root / "known-repo"
    clone.mkdir(parents=True)
    git(clone, "init", "-q")
    store = RunStore(config.db_path)
    try:
        decision = mutation_execution_decision(
            "dispatch_run",
            {"repo": "knwon-repo", "instructions": "typo'd slug"},
            store=store,
            holder=ConfigHolder(config, store),
        )
    finally:
        store.close()
    assert decision is not None and decision.verdict == "deny"
    assert decision.detail is not None
    assert "registered repos: known-repo" in decision.detail
