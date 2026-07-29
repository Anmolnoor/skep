"""Derive reusable permissions from remembered approval ledger entries."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .store import RunStore

_WORD = re.compile(r"[a-z0-9_]+")
_STOP_WORDS = {
    "a",
    "an",
    "and",
    "for",
    "in",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}


@dataclass(frozen=True)
class PermissionProfile:
    """Minimal reusable permissions derived from successful remembered approvals."""

    repo_path: str
    instruction_keywords: tuple[str, ...]
    network: tuple[str, ...] = ()
    env_allowlist: tuple[str, ...] = ()
    shell_allowlist: tuple[tuple[str, ...], ...] = ()
    allow_git_mutation: bool = False
    source_entry_ids: tuple[int, ...] = ()


def derive_permission_profile(
    store: RunStore,
    *,
    repo: Path | str,
    instructions: str,
    min_keyword_overlap: float = 0.3,
) -> PermissionProfile:
    """Build a permission profile from matching remembered approvals.

    Similarity intentionally starts simple: same repo plus keyword overlap with
    the new instructions. The caller can later decide whether to suggest, bind,
    or ignore this profile.
    """

    target_keywords = _keywords(instructions)
    network: set[str] = set()
    env_allowlist: set[str] = set()
    shell_allowlist: set[tuple[str, ...]] = set()
    allow_git_mutation = False
    source_entry_ids: list[int] = []

    for entry in store.ledger_for_repo(repo):
        if not entry.remembered or entry.task_outcome != "completed":
            continue
        if _keyword_overlap(target_keywords, entry.instructions_snippet) < min_keyword_overlap:
            continue

        contributed = False
        if entry.action.startswith("network."):
            host = _network_host(entry.resource)
            if host:
                network.add(host)
                contributed = True
        elif entry.action == "shell.run":
            command = _shell_command(entry.resource)
            if command:
                shell_allowlist.add(command)
                contributed = True
        elif entry.action.startswith("git."):
            allow_git_mutation = True
            contributed = True
        elif entry.action.startswith("env."):
            env_allowlist.add(entry.resource)
            contributed = True

        if contributed:
            source_entry_ids.append(entry.id)

    return PermissionProfile(
        repo_path=str(repo),
        instruction_keywords=tuple(sorted(target_keywords)),
        network=tuple(sorted(network)),
        env_allowlist=tuple(sorted(env_allowlist)),
        shell_allowlist=tuple(sorted(shell_allowlist)),
        allow_git_mutation=allow_git_mutation,
        source_entry_ids=tuple(source_entry_ids),
    )


def _keywords(text: str) -> set[str]:
    return {word for word in _WORD.findall(text.lower()) if word not in _STOP_WORDS}


def keyword_overlap(target: str, candidate: str) -> float:
    return _keyword_overlap(_keywords(target), candidate)


def _keyword_overlap(target_keywords: set[str], candidate: str) -> float:
    candidate_keywords = _keywords(candidate)
    if not target_keywords or not candidate_keywords:
        return 0.0
    return len(target_keywords & candidate_keywords) / len(target_keywords)


def _network_host(resource: str) -> str:
    parsed = urlparse(resource)
    if parsed.hostname:
        return parsed.hostname
    return resource.strip().removesuffix("/")


def _shell_command(resource: str) -> tuple[str, ...]:
    try:
        return tuple(shlex.split(resource))
    except ValueError:
        return (resource,) if resource.strip() else ()
