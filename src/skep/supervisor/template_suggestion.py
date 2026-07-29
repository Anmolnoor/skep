"""Suggest workflow templates from remembered approval profiles."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path

from .permission_profile import PermissionProfile, derive_permission_profile, keyword_overlap
from .store import RunStore
from .templates import WorkflowTemplate

_NAME_WORD = re.compile(r"[a-z0-9]+")
_NAME_STOP_WORDS = {"a", "an", "and", "for", "in", "of", "on", "or", "that", "the", "to", "with"}


@dataclass(frozen=True)
class TemplateSuggestion:
    template: WorkflowTemplate
    profile: PermissionProfile


def suggest_template(
    store: RunStore,
    *,
    name: str,
    repo: Path | str,
    instructions: str,
    worker_kind: str = "coding",
) -> TemplateSuggestion | None:
    profile = derive_permission_profile(store, repo=repo, instructions=instructions)
    if not _has_permissions(profile):
        return None
    template = WorkflowTemplate(
        name=name,
        instructions=instructions,
        worker_kind=worker_kind,
        repo=str(repo),
        network=profile.network,
        env_allowlist=profile.env_allowlist,
        shell_allowlist=profile.shell_allowlist,
        allow_git_mutation=profile.allow_git_mutation,
        provenance="learned",
    )
    return TemplateSuggestion(template=template, profile=profile)


def match_template(
    store: RunStore,
    *,
    repo: Path | str,
    instructions: str,
    min_keyword_overlap: float = 0.3,
) -> WorkflowTemplate | None:
    matches = matching_templates(
        store,
        repo=repo,
        instructions=instructions,
        min_keyword_overlap=min_keyword_overlap,
    )
    return matches[0] if len(matches) == 1 else None


def matching_templates(
    store: RunStore,
    *,
    repo: Path | str,
    instructions: str,
    min_keyword_overlap: float = 0.3,
) -> list[WorkflowTemplate]:
    matches: list[WorkflowTemplate] = []
    repo_path = str(repo)
    for template in store.list_templates():
        if template.repo != repo_path or template.params:
            continue
        if keyword_overlap(instructions, template.instructions) >= min_keyword_overlap:
            matches.append(template)
    return matches


def suggest_template_name(instructions: str) -> str:
    words = [
        word for word in _NAME_WORD.findall(instructions.lower()) if word not in _NAME_STOP_WORDS
    ]
    return "-".join(words[:4]) or "learned-template"


def merge_template_permissions(
    template: WorkflowTemplate, profile: PermissionProfile
) -> WorkflowTemplate:
    return replace(
        template,
        network=tuple(sorted(set(template.network) | set(profile.network))),
        env_allowlist=tuple(sorted(set(template.env_allowlist) | set(profile.env_allowlist))),
        shell_allowlist=tuple(sorted(set(template.shell_allowlist) | set(profile.shell_allowlist))),
        allow_git_mutation=template.allow_git_mutation or profile.allow_git_mutation,
    )


def _has_permissions(profile: PermissionProfile) -> bool:
    return bool(
        profile.network
        or profile.env_allowlist
        or profile.shell_allowlist
        or profile.allow_git_mutation
    )
