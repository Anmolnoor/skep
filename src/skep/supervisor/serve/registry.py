"""Registry routes (v5 Stage D): templates, schedules, skills, repos, settings.

Thin handlers over the v3.5/v4 core — the same template registry, schedule
table, and learned-skill lifecycle the CLI drives, plus the A7 repo registry
that makes "zero local dependencies" real: repos come in by URL, cloned under
``SKEP_HOME/repos``, and runs/schedules name them by slug instead of host path.
"""

from __future__ import annotations

import errno
import json
import re
import shutil
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from ...profile import load_profile, profile_path, run_personal_setup
from ..autonomy import project_policy_dispatch_decision, run_request_resolved_decision
from ..dispatch import auto_apply_decision
from ..packs import PolicyPack, builtin_policy_packs, get_policy_pack
from ..projects import (
    ProjectBinding,
    first_party_project_policy,
    first_party_schedule_seeds,
    list_projects,
    project_from_store,
    project_to_dict,
    validate_project_definition,
)
from ..scheduler import make_schedule, make_template_schedule, parse_interval, validate_chain
from ..skill_cmds import SkillError, approve, propose, reject, run_candidate_test
from ..store import RunStore
from ..template_suggestion import TemplateSuggestion, suggest_template
from ..templates import (
    TemplateError,
    WorkflowTemplate,
    template_from_dict,
    template_to_dict,
    validate_template,
)
from .settings import ConfigHolder

_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_GIT_URL_RE = re.compile(r"^(?:https?://|ssh://|git://|git@).+")


class ScheduleRequest(BaseModel):
    name: str
    repo: str | None = None  # optional only for caste 'note'
    every: str
    instructions: str | None = None
    template: str | None = None
    params: dict[str, str] = Field(default_factory=dict)
    caste: str = "coding"
    ref: str | None = None
    network: list[str] = Field(default_factory=list)
    env_allowlist: list[str] = Field(default_factory=list)
    # v44-F2 reminder semantics: fire once then self-disable; optionally not
    # before start_at (RFC3339 UTC — "remind me tomorrow at 9am", once).
    once: bool = False
    start_at: str | None = None
    # v53-F5: run with the named schedule's last output as labeled context.
    chain: str | None = None


class ScheduleToggle(BaseModel):
    enabled: bool


class RepoRequest(BaseModel):
    url: str
    name: str | None = None


class OpsRunRequest(BaseModel):
    """Body of the ops plan/run routes (v32)."""

    node_id: str
    capability: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    actor: str = "operator"


class NodeRequest(BaseModel):
    node_id: str = Field(min_length=1)
    name: str | None = None
    host: str | None = None
    kind: str = "local"
    trust_tier: str = "trusted_local"
    allowed_capabilities: list[str] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)


class ProjectBindingRequest(BaseModel):
    kind: str
    value: str


class ProjectRequest(BaseModel):
    project_id: str
    name: str
    strategy: str
    phase: str
    policy: dict[str, Any] = Field(default_factory=dict)
    bindings: list[ProjectBindingRequest] = Field(default_factory=list)


class ProjectSetupRequest(BaseModel):
    project_id: str
    name: str
    strategy: str | None = None
    pack: str | None = None
    phase: str = "build"
    repo_path: str | None = None
    repo_slug: str | None = None
    template_names: list[str] = Field(default_factory=list)
    policy_overrides: dict[str, Any] = Field(default_factory=dict)
    seed_default_schedules: bool = True
    seed_shell_commands: bool = True


class ProjectPreviewRequest(ProjectSetupRequest):
    seed_default_schedules: bool = True


class ProjectPhaseRequest(BaseModel):
    phase: str


class SuggestionConfirmRequest(BaseModel):
    repo: str
    instructions: str
    caste: str = "coding"


class SkillTestRequest(BaseModel):
    repo: str
    params: dict[str, str] = Field(default_factory=dict)
    ref: str | None = None


class SkillDecision(BaseModel):
    actor: str = "operator"
    note: str | None = None
    as_name: str | None = None


class ProviderSettings(BaseModel):
    provider: str
    model: str
    endpoint: str | None = None
    api_key_env: str | None = None


class ProviderCreateRequest(BaseModel):
    """v108-F2: register a registry profile. ``api_key_env`` is an env-var
    NAME — key values never ride this route (G2). v108-F3: ``preset`` names
    a catalog row that fills the other fields; explicit values override."""

    provider_id: str | None = None
    protocol: str | None = None
    base_url: str | None = None
    model: str | None = None
    api_key_env: str | None = None
    cost_class: str | None = None
    fallback_order: int = 0
    allowed_network_hosts: list[str] = Field(default_factory=list)
    preset: str | None = None
    activate: bool = False


class ChannelConfigRequest(BaseModel):
    """v26-F1: partial channel config update; secrets are write-only."""

    enabled: bool | None = None
    channel_can_confirm: bool | None = None
    allowed_identities: list[str] | None = None
    secret: str | None = None
    signing_secret: str | None = None  # slack only (webhook signature key)
    # v44-F1 routing knobs (effective on Discord; stored generically).
    require_mention: bool | None = None
    auto_thread: bool | None = None
    allowed_users: list[str] | None = None
    # v78-F1: delivery volume — can only silence pushes, never allow anything.
    notification_level: str | None = None


def repos_root(holder: ConfigHolder) -> Path:
    # config.home is <SKEP_HOME>/supervisor (build_config); repos sit beside it.
    return holder.current.home.parent / "repos"


def known_repos(root: Path, store: RunStore) -> list[dict[str, Any]]:
    """v81-F10: every repo skep can reach — clones under ``root`` plus
    workon-bound local dirs (the v73-F3 merge). One list, every surface:
    the /repos deck, the list_repos chat tool, and dispatch's teaching errors."""
    repos: list[dict[str, Any]] = (
        [
            {"name": entry.name, "path": str(entry), "source": "clone"}
            for entry in sorted(root.iterdir())
            if (entry / ".git").exists()
        ]
        if root.is_dir()
        else []
    )
    seen = {item["path"] for item in repos}
    for bound_project in list_projects(store):
        for binding in bound_project.bindings:
            path = Path(binding.value)
            # Existing dirs only — a deleted workspace must drop out (I8).
            if binding.kind != "repo_path" or str(path) in seen or not path.is_dir():
                continue
            seen.add(str(path))
            repos.append({"name": path.name, "path": str(path), "source": "workon"})
    return repos


def resolve_repo_arg(value: str, root: Path, store: RunStore | None = None) -> Path:
    """A registered repo slug, a workon-bound directory name, else a host path.

    v87-F1: a bare name NEVER resolves against the daemon's CWD — serve was
    launched from a checkout once and "skep-workspace" silently became
    <checkout>/skep-workspace while the workon binding sat unconsulted. A
    bare name that matches neither a clone nor a binding resolves under the
    clone root, so the downstream error teaches list_repos (I9) instead of
    reporting a CWD accident. Paths (anything with a separator or ``~``)
    keep the documented dev-only host-path route.
    """
    if _SLUG_RE.match(value):
        if (root / value / ".git").exists():
            return (root / value).resolve()
        if store is not None:
            for bound_project in list_projects(store):
                for binding in bound_project.bindings:
                    if binding.kind != "repo_path":
                        continue
                    path = Path(binding.value)
                    if path.name == value and path.is_dir():
                        return path.resolve()
        return (root / value).resolve()
    return Path(value).expanduser().resolve()


def existing_dir_error(resolved: Path) -> str | None:
    """v73-F11: the ONE story for a local path that cannot be a workspace.

    dispatch_run's decision hook and workon both route through here — the
    field test drew two different errors ("not a git repository" vs "not a
    directory") for the same missing directory, at the cost of two burned
    confirmations. None means the directory exists."""
    if resolved.is_dir():
        return None
    if resolved.exists():
        return f"{resolved} is not a directory"
    return (
        f"{resolved} does not exist on this machine — is the repo somewhere "
        "else? (list_repos shows what skep can reach)"
    )


def is_git_url(value: str) -> bool:
    return bool(_GIT_URL_RE.match(value))


def _repo_has_head(repo: Path) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )


def _initialize_empty_repo(repo: Path) -> bool:
    if _repo_has_head(repo):
        return False
    add = _git(repo, "add", "--all")
    if add.returncode != 0:
        detail = add.stderr.strip() or add.stdout.strip()
        raise HTTPException(status_code=502, detail=f"git add failed: {detail}")
    commit = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=skep@localhost",
            "-c",
            "user.name=skep",
            "commit",
            "--allow-empty",
            "-m",
            "Initialize repository for skep",
        ],
        capture_output=True,
        text=True,
    )
    if commit.returncode != 0:
        detail = commit.stderr.strip() or commit.stdout.strip()
        raise HTTPException(status_code=502, detail=f"git initial commit failed: {detail}")
    return True


def _push_baseline(repo: Path) -> tuple[bool, str | None]:
    """v79-F1: a synthesized baseline only helps if origin has it too.

    A repo created empty on GitHub has NO branches; the local baseline commit
    (`_initialize_empty_repo`) never reached origin, so every later PR failed
    on a missing base branch (field test 2026-07-17: four chats, zero PRs).
    Push the baseline iff the remote has ZERO heads — this only ever CREATES
    the remote default branch, never updates an existing ref (I1 intact).
    Returns (pushed, detail); a failed push is reported, never raised —
    registration itself already succeeded.
    """
    heads = _git(repo, "ls-remote", "--heads", "origin")
    if heads.returncode != 0:
        detail = heads.stderr.strip() or heads.stdout.strip()
        return False, f"git ls-remote failed: {detail}"
    if heads.stdout.strip():
        return False, None
    branch = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    if not branch:
        return False, "cannot determine the local default branch to push"
    pushed = subprocess.run(
        ["git", "-C", str(repo), "push", "-u", "origin", branch],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if pushed.returncode != 0:
        detail = pushed.stderr.strip() or pushed.stdout.strip()
        return False, f"git push failed: {detail}"
    return True, None


def ensure_repo_baseline(repo: Path) -> bool:
    """Ensure an existing local folder is a git repo with a dispatchable HEAD."""
    if not repo.is_dir():
        raise HTTPException(status_code=400, detail=f"{repo} is not a git repository")
    if not (repo / ".git").exists():
        init = subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, text=True)
        if init.returncode != 0:
            detail = init.stderr.strip() or init.stdout.strip()
            raise HTTPException(status_code=502, detail=f"git init failed: {detail}")
    return _initialize_empty_repo(repo)


def register_repo(root: Path, *, url: str, name: str | None = None) -> dict[str, Any]:
    slug = name or url.rstrip("/").split("/")[-1].removesuffix(".git")
    if not _SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail=f"cannot derive a repo name from {slug!r}")
    dest = root / slug
    if dest.exists():
        raise HTTPException(status_code=409, detail=f"repo {slug!r} already registered")
    root.mkdir(parents=True, exist_ok=True)
    clone = subprocess.run(["git", "clone", url, str(dest)], capture_output=True, text=True)
    if clone.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        raise HTTPException(status_code=502, detail=f"git clone failed: {clone.stderr.strip()}")
    try:
        initialized = ensure_repo_baseline(dest)
    except HTTPException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    baseline_pushed = False
    baseline_detail: str | None = None
    if initialized:
        baseline_pushed, baseline_detail = _push_baseline(dest)
    result: dict[str, Any] = {
        "name": slug,
        "url": url,
        "path": str(dest),
        "initialized_empty_repo": initialized,
        "baseline_pushed": baseline_pushed,
    }
    if baseline_detail is not None:
        result["baseline_push_detail"] = baseline_detail
    return result


def remove_registered_repo(store: RunStore, root: Path, name: str) -> dict[str, bool]:
    """v57-F8: shared removal for the HTTP route and the carded chat verb.

    Refuses while runs are in flight — rmtree under a live worker's feet
    would corrupt its worktree; waiting (or stopping the run) is the honest
    path. The route previously had no such guard."""
    if not _SLUG_RE.match(name) or not (root / name).is_dir():
        # v106-F8 (I9): the bare refusal taught nothing — the Queen retried
        # blind names in the field. Name what CAN be removed here, and where
        # workon-bound repos are removed instead.
        clones = ", ".join(
            sorted(item["name"] for item in known_repos(root, store) if item["source"] == "clone")
        )
        raise HTTPException(
            status_code=404,
            detail=f"no registered clone named {name!r}; clones: {clones or 'none'}. "
            "A workon-bound directory is removed by deleting its project "
            "(DELETE /api/projects/<id>), not by repo removal.",
        )
    from ..cli_cmds import STATE_EXIT_CODES

    target = str((root / name).resolve())
    busy = [
        record.task_id
        for record in store.recent_runs(50)
        if str(record.repo) == target and record.state not in STATE_EXIT_CODES
    ]
    if busy:
        sample = ", ".join(task_id[:13] for task_id in busy[:3])
        raise HTTPException(
            status_code=409,
            detail=f"repo {name!r} has in-flight runs ({sample}) — wait or stop them first",
        )
    _remove_repo_tree(root / name)
    return {"removed": True}


def _remove_repo_tree(path: Path) -> None:
    for attempt in range(5):
        try:
            shutil.rmtree(path)
            return
        except OSError as exc:
            if exc.errno != errno.ENOTEMPTY or attempt == 4:
                raise
            time.sleep(0.05)


def _candidate_view(candidate: Any) -> dict[str, Any]:
    view = asdict(candidate)
    view["template"] = template_to_dict(candidate.template)
    view["source_task_ids"] = list(candidate.source_task_ids)
    return view


def _schedule_view(store: RunStore, schedule: Any) -> dict[str, Any]:
    from .actions import schedule_view

    return schedule_view(store, schedule)


def _ledger_view(record: Any) -> dict[str, Any]:
    return asdict(record)


def _suggestion_view(suggestion: TemplateSuggestion) -> dict[str, Any]:
    return {
        "id": suggestion.template.name,
        "template": template_to_dict(suggestion.template),
        "profile": asdict(suggestion.profile),
    }


def _validate_project_binding(*, root: Path, run_store: RunStore, kind: str, value: str) -> None:
    if kind == "repo_slug":
        if not _SLUG_RE.match(value) or not (root / value / ".git").exists():
            raise HTTPException(status_code=400, detail=f"unknown registered repo slug {value!r}")
        return
    if kind == "repo_path":
        repo = Path(value).expanduser().resolve()
        if not repo.is_dir():
            raise HTTPException(status_code=400, detail=f"repo path {value!r} does not exist")
        return
    if kind == "template_name" and run_store.get_template(value) is None:
        raise HTTPException(status_code=400, detail=f"unknown template {value!r}")


def _persist_project(run_store: RunStore, project: Any) -> dict[str, Any]:
    run_store.add_project_policy(
        project_id=project.project_id,
        name=project.name,
        strategy=project.strategy,
        phase=project.phase,
        policy=project.policy,
        pack_name=project.pack_name,
        pack_version=project.pack_version,
    )
    run_store.remove_project_bindings(project.project_id)
    for binding in project.bindings:
        run_store.add_project_binding(
            project_id=project.project_id,
            binding_kind=binding.kind,
            binding_value=binding.value,
        )
    return project_to_dict(project)


def _pack_view(pack: PolicyPack) -> dict[str, str]:
    return {
        "name": pack.name,
        "version": pack.version,
        "strategy": pack.strategy,
        "status": pack.status,
        "description": pack.description,
    }


def _pack_summary(pack: PolicyPack) -> dict[str, str]:
    return {"name": pack.name, "version": pack.version, "status": pack.status}


def _dangerous_grant_warnings(policy: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if policy.get("allowed_shell_commands"):
        warnings.append("allowed_shell_commands")
    if policy.get("allow_git_mutation") is True:
        warnings.append("allow_git_mutation")
    if policy.get("default_network"):
        warnings.append("default_network")
    if policy.get("allowed_plugin_risks"):
        warnings.append("allowed_plugin_risks")
    if policy.get("auto_dispatch_allowed") is True:
        warnings.append("auto_dispatch_allowed")
    if policy.get("auto_apply_verified_patch") is True:
        warnings.append("auto_apply_verified_patch")
    return warnings


def _landing_decision_payload(policy: dict[str, Any]) -> dict[str, str | None]:
    raw = policy.get("auto_apply_verified_patch")
    decision = auto_apply_decision((), raw if isinstance(raw, bool) else None)
    return {"verdict": decision.verdict, "reason": decision.reason, "detail": decision.detail}


def _dispatch_decision_payload(policy: dict[str, Any]) -> dict[str, str | None]:
    decision = (
        project_policy_dispatch_decision(
            policy=policy,
            requested_execution_mode=None,
            explicit_run_overrides=False,
        )
        or run_request_resolved_decision()
    )
    return decision.to_payload().model_dump(mode="json")


def _pack_template_name(project_id: str, seed_name: str) -> str:
    return f"{project_id}-{seed_name}"


def _pack_provenance(pack: PolicyPack) -> str:
    return f"pack:{pack.name}@{pack.version}"


def _pack_template(project_id: str, pack: PolicyPack, seed: Any) -> WorkflowTemplate:
    return WorkflowTemplate(
        name=_pack_template_name(project_id, seed.name),
        description=seed.description,
        instructions=seed.instructions,
        worker_kind=seed.worker_kind,
        provenance=_pack_provenance(pack),
    )


def _pack_template_plan(project_id: str, pack: PolicyPack) -> list[dict[str, Any]]:
    return [template_to_dict(_pack_template(project_id, pack, seed)) for seed in pack.templates]


def _pack_schedule_plan(project_id: str, pack: PolicyPack) -> list[dict[str, Any]]:
    schedules: list[dict[str, Any]] = []
    for seed in pack.schedules:
        schedules.append(
            {
                "name": _pack_template_name(project_id, seed.name),
                "every": seed.every,
                "template": (
                    None
                    if seed.template is None
                    else _pack_template_name(project_id, seed.template)
                ),
                "instructions": seed.instructions,
                "enabled": seed.enabled,
            }
        )
    return schedules


def _schedule_repo_for_project_setup(
    *, root: Path, repo_path: str | None, repo_slug: str | None
) -> Path | None:
    if repo_path:
        return Path(repo_path).expanduser().resolve()
    if repo_slug:
        repo = (root / repo_slug).resolve()
        if repo.is_dir() and (repo / ".git").exists():
            return repo
    return None


def _makefile_targets(repo_dir: Path) -> list[str]:
    makefile = repo_dir / "Makefile"
    if not makefile.is_file():
        return []
    targets: list[str] = []
    for line in makefile.read_text(encoding="utf-8", errors="replace").splitlines():
        matched = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.-]*):", line)
        if matched and matched.group(1) not in targets:
            targets.append(matched.group(1))
    return targets


def toolchain_shell_seeds(repo_dir: Path | None) -> list[list[str]]:
    """The repo's own dev-loop commands, from an explicit toolchain table (v23-F4).

    Seeded into a project's allowlist preview so the human approves the batch
    once at setup instead of gating command-by-command. Not heuristics: only
    files that unambiguously name a toolchain, and every seed must survive the
    persistence guard (no interpreters, no remote git, no add/commit)."""
    from ..shell_prefixes import dangerous_prefix_reason

    if repo_dir is None or not repo_dir.is_dir():
        return []
    seeds: list[list[str]] = []
    if (repo_dir / "pyproject.toml").is_file():
        seeds += [["uv", "run", "pytest"], ["pytest"]]
    if (repo_dir / "package.json").is_file():
        seeds += [["npm", "test"], ["npm", "run", "build"], ["npm", "run", "lint"]]
    seeds += [["make", target] for target in _makefile_targets(repo_dir)[:12]]
    return [seed for seed in seeds if dangerous_prefix_reason(seed) is None]


def _npm_test_script(repo_dir: Path) -> bool:
    """True when package.json declares a REAL test script. ``npm init``'s
    placeholder (`echo "Error: no test specified" && exit 1`) exits 1, so
    pinning it would fail every re-verification."""
    package = repo_dir / "package.json"
    if not package.is_file():
        return False
    try:
        data = json.loads(package.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return False
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, dict):
        return False
    script = str(scripts.get("test") or "").strip()
    return bool(script) and "no test specified" not in script


def verify_command_seed(repo_dir: Path | None) -> str:
    """The command G10 re-runs, from the repo's own declared entry point (v91-F1).

    v88-F4 made ``verify_command`` opt-in and nothing set it, so every project
    kept re-running whatever the worker nominated for itself — a claim, not a
    verdict (I2). Setup now pins one by default, off the same explicit
    toolchain table ``toolchain_shell_seeds`` reads.

    Conservative on purpose: a confidently wrong pin is worse than none,
    because a re-verification that cannot pass is indistinguishable from a
    broken patch (``pytest`` with no tests exits 5, which ``reverify`` reads as
    "failed"). Every branch requires its target to actually exist — a declared
    ``test`` target, a real test tree, a real npm script. Nothing detected
    means no pin, and the project keeps the worker-nominated fallback.

    v101-F14: "exist" was checked in the REPO and never on the HOST, so this
    function broke its own rule. Run 019faa33 — claude_code on the skep project
    itself — re-verified with ``make test`` and got exit 127: `make` is not
    installed on that machine, and has not been since v19. The pin was
    unrunnable by construction, so G10 (supervisor-side re-verification, skep's
    central safety claim) was permanently inoperative on skep's own repo: every
    run, forever, NOT CONFIRMED. Nothing landed unsafely — `unavailable` is a
    distinct outcome from `failed` and it fails closed — but a gate that can
    never confirm has stopped measuring. The irony sharpens it: the target it
    could not reach was ``test: uv run pytest``, which is what the very next
    branch would have pinned. One indirection away from correct, and the
    indirection was the broken part.

    So every branch now gates on its entry point being RUNNABLE. ``shutil.which``
    is already the house probe — ``status.py`` uses it for worker commands and
    the engine registry for every CLI agent. Falling through is exactly what the
    docstring above asks for: no runnable detection means no pin."""
    if repo_dir is None or not repo_dir.is_dir():
        return ""
    if "test" in _makefile_targets(repo_dir) and shutil.which("make"):
        return "make test"
    if (repo_dir / "pyproject.toml").is_file() and (
        (repo_dir / "tests").is_dir() or any(repo_dir.glob("test_*.py"))
    ):
        # uv implies uv, which is how skep runs at all — probed anyway, for the
        # same reason: the host is what runs it, not the lockfile.
        if (repo_dir / "uv.lock").is_file() and shutil.which("uv"):
            return "uv run pytest"
        if shutil.which("pytest"):
            return "pytest"
    if _npm_test_script(repo_dir) and shutil.which("npm"):
        return "npm test"
    return ""


def policy_group_suggestions(repo_dir: Path | None) -> list[str]:
    """v97-F4 (ADR 0048): builtin groups the repo's own toolchain calls for.

    Suggestions only — they ride the preview/setup RESULT and never attach
    silently (I6): attaching takes an explicit ``groups=`` / ``--group`` /
    attach_policy_group, each behind the operator's confirm."""
    if repo_dir is None or not repo_dir.is_dir():
        return []
    suggestions: list[str] = []
    if (repo_dir / "pyproject.toml").is_file() or (repo_dir / "requirements.txt").is_file():
        suggestions.append("python-bootstrap")
    if (repo_dir / "package.json").is_file():
        suggestions.append("node-dev")
    return suggestions


def _vet_group_attaches(run_store: RunStore, policy: dict[str, Any]) -> None:
    """v97-F4: attach-time known-name check — a typo'd group refuses at setup
    naming the known set (I9) instead of failing the first dispatch closed."""
    from ..projects import stored_policy_groups

    names = policy.get("policy_groups") or []
    if not names:
        return
    known = stored_policy_groups(run_store)
    unknown = sorted(str(n) for n in names if str(n) not in known)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"unknown policy group(s) {unknown!r}; known: {sorted(known)} "
            "(set_policy_group creates one)",
        )


def _seed_repo_dir(root: Path, repo_path: str | None, repo_slug: str | None) -> Path | None:
    if repo_path:
        return Path(repo_path)
    if repo_slug:
        return root / repo_slug
    return None


def _merge_shell_seeds(base_policy: dict[str, Any], repo_dir: Path | None) -> list[list[str]]:
    """Merge toolchain seeds into the policy; return only the newly added ones."""
    seeds = toolchain_shell_seeds(repo_dir)
    if not seeds:
        return []
    existing = [list(command) for command in base_policy.get("allowed_shell_commands") or []]
    added = [seed for seed in seeds if seed not in existing]
    if added:
        base_policy["allowed_shell_commands"] = existing + added
    return added


def _merge_verify_seed(base_policy: dict[str, Any], repo_dir: Path | None) -> str:
    """Pin a verify_command when the policy carries none; return what was added.

    An explicit pin (a pack phase default, or policy_overrides) always wins —
    this only fills the hole."""
    if str(base_policy.get("verify_command") or "").strip():
        return ""
    seeded = verify_command_seed(repo_dir)
    if seeded:
        base_policy["verify_command"] = seeded
    return seeded


def _carry_forward_existing(
    run_store: RunStore,
    *,
    project_id: str,
    base_policy: dict[str, Any],
    bindings: list[ProjectBinding],
) -> tuple[dict[str, Any], list[ProjectBinding]]:
    """v100-F10: a repeated setup is a policy UPDATE, not a re-install.

    v24-F4 wrote that rule for seeded templates and it was never applied to the
    project's own policy or bindings, so `_persist_project` re-installed both:
    `remove_project_bindings` then re-add, and a policy rebuilt from the phase
    defaults alone. The defaults cover four keys (`default_execution_mode`,
    `auto_dispatch_allowed`, `auto_apply_verified_patch`, `auto_apply_branch`) —
    every OTHER key an operator ever set (`verify_command`, `coding_engine`,
    `allow_git_mutation`, `allowed_plugin_risks`, `policy_groups`, ...) was
    silently wiped by a re-run that changed one flag. v100's own acceptance lost
    a live `verify_command` pin and a `repo_slug` binding to exactly this.

    The rule, in layers: what the operator stored, then the strategy/phase
    defaults, then this call's explicit overrides. So a phase change still moves
    the trust flags it owns, an explicit flag still wins, and nothing the
    operator set is destroyed by a knob they did not touch. Bindings replace
    only the KINDS this call supplies: setting a repo_path no longer deletes a
    repo_slug, and a rebind still replaces the address it names.
    """
    stored = run_store.get_project_policy(project_id)
    if stored is None:
        return base_policy, bindings
    kinds = {binding.kind for binding in bindings}
    kept = [
        ProjectBinding(kind=record.binding_kind, value=record.binding_value)
        for record in run_store.project_bindings(project_id)
        if record.binding_kind not in kinds
    ]
    return {**stored.policy, **base_policy}, [*bindings, *kept]


def setup_project_record(
    *,
    run_store: RunStore,
    root: Path,
    project_id: str,
    name: str,
    strategy: str | None,
    phase: str,
    pack_name: str | None = None,
    repo_path: str | None = None,
    repo_slug: str | None = None,
    template_names: list[str] | None = None,
    policy_overrides: dict[str, Any] | None = None,
    seed_default_schedules: bool = True,
    seed_shell_commands: bool = True,
) -> dict[str, Any]:
    bindings: list[ProjectBinding] = []
    if repo_path:
        bindings.append(ProjectBinding(kind="repo_path", value=repo_path))
    if repo_slug:
        bindings.append(ProjectBinding(kind="repo_slug", value=repo_slug))
    for template_name in template_names or []:
        bindings.append(ProjectBinding(kind="template_name", value=template_name))
    if not bindings:
        raise HTTPException(
            status_code=400,
            detail="project setup requires at least one binding "
            "(repo_path, repo_slug, or template_names)",
        )
    pack: PolicyPack | None = None
    if pack_name is not None:
        try:
            pack = get_policy_pack(pack_name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        strategy = pack.strategy
    if strategy is None:
        raise HTTPException(status_code=400, detail="project setup requires a pack or strategy")
    base_policy = (
        dict(pack.phase_defaults[phase])
        if pack is not None
        else first_party_project_policy(strategy=strategy, phase=phase)
    )
    base_policy.update(policy_overrides or {})
    supplied = list(bindings)
    base_policy, bindings = _carry_forward_existing(
        run_store, project_id=project_id, base_policy=base_policy, bindings=bindings
    )
    _vet_group_attaches(run_store, base_policy)
    seeded_shell_commands: list[list[str]] = []
    seeded_verify_command = ""
    if seed_shell_commands:
        seed_dir = _seed_repo_dir(root, repo_path, repo_slug)
        seeded_shell_commands = _merge_shell_seeds(base_policy, seed_dir)
        seeded_verify_command = _merge_verify_seed(base_policy, seed_dir)
    try:
        project = validate_project_definition(
            project_id=project_id,
            name=name,
            strategy=strategy,
            phase=phase,
            policy=base_policy,
            bindings=bindings,
            pack_name=None if pack is None else pack.name,
            pack_version=None if pack is None else pack.version,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # v100-F10: validate the bindings THIS call claims. A carried-forward one was
    # validated when it was made, and re-validating it would let an unrelated
    # stale binding (its repo since unregistered) block every later update with
    # an error naming something the operator did not ask for (I9).
    for binding in supplied:
        _validate_project_binding(
            root=root,
            run_store=run_store,
            kind=binding.kind,
            value=binding.value,
        )
    result = _persist_project(run_store, project)
    seeded_templates: list[dict[str, Any]] = []
    seeds_skipped: list[str] = []
    if pack is not None:
        for template_seed in pack.templates:
            template = _pack_template(project.project_id, pack, template_seed)
            # v24-F4: re-running setup must never clobber or reset an existing
            # seed — a repeated setup is a policy update, not a re-install.
            if run_store.get_template(template.name) is not None:
                seeds_skipped.append(f"template:{template.name}")
                continue
            validate_template(template)
            run_store.add_template(template)
            seeded_templates.append(template_to_dict(template))
    seeded_schedules: list[dict[str, Any]] = []
    if seed_default_schedules:
        schedule_repo = _schedule_repo_for_project_setup(
            root=root,
            repo_path=repo_path,
            repo_slug=repo_slug,
        )
        if schedule_repo is not None:
            if pack is not None:
                for schedule_seed in pack.schedules:
                    schedule_name = _pack_template_name(project.project_id, schedule_seed.name)
                    if run_store.get_schedule(schedule_name) is not None:
                        seeds_skipped.append(f"schedule:{schedule_name}")
                        continue
                    if schedule_seed.template is not None:
                        template_name = _pack_template_name(
                            project.project_id, schedule_seed.template
                        )
                        saved_template = run_store.get_template(template_name)
                        if saved_template is None:
                            raise HTTPException(
                                status_code=409,
                                detail=f"seeded template {template_name!r} was not saved",
                            )
                        schedule = make_template_schedule(
                            name=schedule_name,
                            template=saved_template,
                            params={},
                            repo=schedule_repo,
                            interval_seconds=parse_interval(schedule_seed.every),
                            enabled=schedule_seed.enabled,
                        )
                    else:
                        assert schedule_seed.instructions is not None
                        schedule = make_schedule(
                            name=schedule_name,
                            repo=schedule_repo,
                            instructions=schedule_seed.instructions,
                            interval_seconds=parse_interval(schedule_seed.every),
                            enabled=schedule_seed.enabled,
                        )
                    run_store.add_schedule(schedule)
                    seeded_schedules.append(_schedule_view(run_store, schedule))
            else:
                for project_schedule_seed in first_party_schedule_seeds(
                    project_id=project.project_id,
                    strategy=project.strategy,
                    phase=project.phase,
                ):
                    if run_store.get_schedule(project_schedule_seed.name) is not None:
                        seeds_skipped.append(f"schedule:{project_schedule_seed.name}")
                        continue
                    schedule = make_schedule(
                        name=project_schedule_seed.name,
                        repo=schedule_repo,
                        instructions=project_schedule_seed.instructions,
                        interval_seconds=parse_interval(project_schedule_seed.every),
                    )
                    run_store.add_schedule(schedule)
                    seeded_schedules.append(_schedule_view(run_store, schedule))
    result["seeded_templates"] = seeded_templates
    result["seeded_schedules"] = seeded_schedules
    result["seeded_shell_commands"] = seeded_shell_commands
    result["seeded_verify_command"] = seeded_verify_command
    result["seeds_skipped"] = seeds_skipped
    attached_groups = [str(n) for n in project.policy.get("policy_groups") or []]
    result["suggested_policy_groups"] = [
        suggestion
        for suggestion in policy_group_suggestions(_seed_repo_dir(root, repo_path, repo_slug))
        if suggestion not in attached_groups
    ]
    return result


def set_project_phase(run_store: RunStore, project_id: str, phase: str) -> dict[str, Any]:
    """Move a project to ``phase``, re-deriving policy from its pack (or the
    first-party defaults) exactly like ``skep project set-phase`` (v25-F1) —
    one implementation for the CLI, the HTTP wrapper, and the command deck."""
    project = project_from_store(run_store, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"no project named {project_id!r}")
    if project.pack_name is not None:
        try:
            pack = get_policy_pack(project.pack_name, include_draft=True)
            policy = dict(pack.phase_defaults[phase])
            strategy = pack.strategy
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    else:
        try:
            policy = first_party_project_policy(strategy=project.strategy, phase=phase)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        strategy = project.strategy
    # v91-F1 (I2): phase defaults never carry a verify_command, so a plain
    # re-derive would drop the pin at exactly the wrong moment — the move INTO
    # maintain is the move into the only lane that lands without a human.
    pinned_verify = str(project.policy.get("verify_command") or "").strip()
    if pinned_verify and not policy.get("verify_command"):
        policy["verify_command"] = pinned_verify
    try:
        updated = validate_project_definition(
            project_id=project.project_id,
            name=project.name,
            strategy=strategy,
            phase=phase,
            policy=policy,
            bindings=list(project.bindings),
            pack_name=project.pack_name,
            pack_version=project.pack_version,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    run_store.add_project_policy(
        project_id=updated.project_id,
        name=updated.name,
        strategy=updated.strategy,
        phase=updated.phase,
        policy=updated.policy,
        pack_name=updated.pack_name,
        pack_version=updated.pack_version,
    )
    return project_to_dict(updated)


def preview_project_setup(
    *,
    root: Path,
    run_store: RunStore,
    project_id: str,
    name: str,
    strategy: str | None,
    phase: str,
    pack_name: str | None,
    repo_path: str | None,
    repo_slug: str | None,
    template_names: list[str],
    policy_overrides: dict[str, Any],
    seed_shell_commands: bool = True,
) -> dict[str, Any]:
    pack: PolicyPack | None = None
    if pack_name is not None:
        try:
            pack = get_policy_pack(pack_name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        strategy = pack.strategy
    if strategy is None:
        raise HTTPException(status_code=400, detail="project preview requires a pack or strategy")
    bindings: list[ProjectBinding] = []
    if repo_path:
        bindings.append(ProjectBinding(kind="repo_path", value=repo_path))
    if repo_slug:
        bindings.append(ProjectBinding(kind="repo_slug", value=repo_slug))
    for template_name in template_names:
        bindings.append(ProjectBinding(kind="template_name", value=template_name))
    if not bindings:
        raise HTTPException(
            status_code=400,
            detail="project preview requires at least one binding "
            "(repo_path, repo_slug, or template_names)",
        )
    base_policy = (
        dict(pack.phase_defaults[phase])
        if pack is not None
        else first_party_project_policy(strategy=strategy, phase=phase)
    )
    base_policy.update(policy_overrides)
    # The preview must show what setup will actually write, carry-forward and
    # all — v94-F6's lesson, where the preview said "none detected" for the very
    # pin it had just inferred.
    base_policy, bindings = _carry_forward_existing(
        run_store, project_id=project_id, base_policy=base_policy, bindings=bindings
    )
    _vet_group_attaches(run_store, base_policy)
    seeded_shell_commands: list[list[str]] = []
    seeded_verify_command = ""
    if seed_shell_commands:
        seed_dir = _seed_repo_dir(root, repo_path, repo_slug)
        seeded_shell_commands = _merge_shell_seeds(base_policy, seed_dir)
        seeded_verify_command = _merge_verify_seed(base_policy, seed_dir)
    try:
        project = validate_project_definition(
            project_id=project_id,
            name=name,
            strategy=strategy,
            phase=phase,
            policy=base_policy,
            bindings=bindings,
            pack_name=None if pack is None else pack.name,
            pack_version=None if pack is None else pack.version,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    for binding in project.bindings:
        _validate_project_binding(
            root=root,
            run_store=run_store,
            kind=binding.kind,
            value=binding.value,
        )
    return {
        "project": project_to_dict(project),
        "pack": None if pack is None else _pack_summary(pack),
        "phase_defaults": {} if pack is None else dict(pack.phase_defaults[phase]),
        "effective_policy": dict(project.policy),
        "dangerous_grant_warnings": _dangerous_grant_warnings(project.policy),
        "bindings_to_save": [
            {"kind": binding.kind, "value": binding.value} for binding in project.bindings
        ],
        "seeded_shell_commands": seeded_shell_commands,
        "seeded_verify_command": seeded_verify_command,
        "suggested_policy_groups": [
            suggestion
            for suggestion in policy_group_suggestions(_seed_repo_dir(root, repo_path, repo_slug))
            if suggestion not in (project.policy.get("policy_groups") or [])
        ],
        "seeded_templates": [] if pack is None else _pack_template_plan(project.project_id, pack),
        "seeded_schedules": [] if pack is None else _pack_schedule_plan(project.project_id, pack),
        "sample_dispatch_decision": _dispatch_decision_payload(project.policy),
        "sample_landing_decision": _landing_decision_payload(project.policy),
    }


def add_registry_routes(app: FastAPI, *, holder: ConfigHolder, run_store: RunStore) -> None:
    root = repos_root(holder)

    # -- templates (v3.5 registry; learned skills land here too) -------------

    @app.get("/api/templates")
    def list_templates() -> dict[str, Any]:
        return {"templates": [template_to_dict(t) for t in run_store.list_templates()]}

    @app.get("/api/templates/{name}")
    def get_template(name: str) -> dict[str, Any]:
        template = run_store.get_template(name)
        if template is None:
            raise HTTPException(status_code=404, detail=f"no template named {name!r}")
        return template_to_dict(template)

    @app.post("/api/templates", status_code=201)
    def put_template(body: dict[str, Any]) -> dict[str, Any]:
        try:
            template = template_from_dict(body)
            validate_template(template)
        except (TemplateError, ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        run_store.add_template(template)
        return template_to_dict(template)

    @app.delete("/api/templates/{name}")
    def delete_template(name: str) -> dict[str, bool]:
        if not run_store.remove_template(name):
            raise HTTPException(status_code=404, detail=f"no template named {name!r}")
        return {"removed": True}

    # -- approval ledger (approval-to-template foundation) -------------------

    @app.get("/api/ledger")
    def list_approval_ledger(repo: str) -> dict[str, Any]:
        return {"ledger": [_ledger_view(record) for record in run_store.ledger_for_repo(repo)]}

    # -- template suggestions -------------------------------------------------

    @app.get("/api/suggestions")
    def list_suggestions(
        name: str,
        repo: str,
        instructions: str,
        caste: str = "coding",
    ) -> dict[str, Any]:
        suggestion = suggest_template(
            run_store,
            name=name,
            repo=repo,
            instructions=instructions,
            worker_kind=caste,
        )
        return {"suggestions": [] if suggestion is None else [_suggestion_view(suggestion)]}

    @app.post("/api/suggestions/{name}/confirm", status_code=201)
    def confirm_suggestion(name: str, body: SuggestionConfirmRequest) -> dict[str, Any]:
        if run_store.get_template(name) is not None:
            raise HTTPException(status_code=409, detail=f"template {name!r} already exists")
        suggestion = suggest_template(
            run_store,
            name=name,
            repo=body.repo,
            instructions=body.instructions,
            worker_kind=body.caste,
        )
        if suggestion is None:
            raise HTTPException(status_code=404, detail="no suggestion for that repo and task")
        run_store.add_template(suggestion.template)
        return _suggestion_view(suggestion)

    # -- schedules ------------------------------------------------------------

    @app.get("/api/schedules")
    def list_schedules() -> dict[str, Any]:
        return {"schedules": [_schedule_view(run_store, s) for s in run_store.list_schedules()]}

    # -- v14: schedule + provider health views -------------------------------

    @app.get("/api/schedules/health")
    def schedule_health() -> dict[str, Any]:
        return {"health": [asdict(h) for h in run_store.list_schedule_health()]}

    @app.get("/api/providers")
    def list_providers() -> dict[str, Any]:
        return {"providers": [asdict(p) for p in run_store.list_provider_profiles()]}

    @app.get("/api/providers/health")
    def provider_health() -> dict[str, Any]:
        return {"health": [asdict(h) for h in run_store.list_provider_health()]}

    # v108-F2: the registry's write path — same actions.py verbs as the CLI
    # and the carded chat tools (ADR 0050).

    @app.get("/api/provider-presets")
    def provider_presets() -> dict[str, Any]:
        from ..provider_presets import PROVIDER_PRESETS, preset_view

        return {"presets": [preset_view(p) for p in PROVIDER_PRESETS.values()]}

    @app.post("/api/providers", status_code=201)
    def add_provider_route(body: ProviderCreateRequest) -> dict[str, Any]:
        # actions imports from this module, so the reverse import stays local.
        from ..providers import ProviderError
        from . import actions

        try:
            result = actions.add_provider(
                run_store,
                provider_id=body.provider_id,
                protocol=body.protocol,
                base_url=body.base_url,
                model=body.model,
                api_key_env=body.api_key_env,
                cost_class=body.cost_class,
                fallback_order=body.fallback_order,
                allowed_network_hosts=tuple(body.allowed_network_hosts),
                preset=body.preset,
            )
            if body.activate:
                saved_id = str(result["provider"]["provider_id"])
                result.update(
                    actions.use_provider(run_store, holder.current.home, provider_id=saved_id)
                )
        except ProviderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return result

    @app.post("/api/providers/{provider_id}/activate")
    def activate_provider_route(provider_id: str) -> dict[str, Any]:
        from ..providers import ProviderError
        from . import actions

        try:
            return actions.use_provider(run_store, holder.current.home, provider_id=provider_id)
        except ProviderError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete("/api/providers/{provider_id}")
    def remove_provider_route(provider_id: str) -> dict[str, Any]:
        from ..providers import ProviderError
        from . import actions

        try:
            return actions.remove_provider(run_store, provider_id=provider_id)
        except ProviderError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # -- v15: ops node registry ----------------------------------------------

    @app.get("/api/nodes")
    def list_nodes() -> dict[str, Any]:
        return {"nodes": [asdict(n) for n in run_store.list_nodes()]}

    @app.post("/api/nodes", status_code=201)
    def add_node(body: NodeRequest) -> dict[str, Any]:
        from ..nodes import Node, NodeError

        try:
            node = run_store.upsert_node(
                Node(
                    node_id=body.node_id,
                    name=body.name or body.node_id,
                    host=body.host or body.node_id,
                    kind=body.kind,
                    trust_tier=body.trust_tier,
                    allowed_capabilities=tuple(body.allowed_capabilities),
                    metadata=dict(body.metadata),
                )
            )
        except NodeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return asdict(node)

    # v32: ops execution. /plan resolves the decision + preview (mutates
    # nothing); /run is the gated real pass (a mutation — actor recorded).

    @app.post("/api/ops/plan")
    def ops_plan(body: OpsRunRequest) -> dict[str, Any]:
        from ...workers.ops_executor import plan_ops
        from ..policy_resolver import resolve_ops_decision

        decision = resolve_ops_decision(
            run_store,
            node_id=body.node_id,
            capability=body.capability,
            phase="maintain",
            arguments=body.arguments,
            approved=False,
        )
        return {
            "decision": {
                "verdict": decision.verdict,
                "reason": decision.reason,
                "dry_run": decision.dry_run,
            },
            "plan": plan_ops(decision, capability=body.capability, arguments=body.arguments),
        }

    @app.post("/api/ops/run")
    def ops_run(body: OpsRunRequest) -> dict[str, Any]:
        from ...workers.ops_executor import OpsExecutionError, execute_ops
        from ..policy_resolver import resolve_ops_decision

        decision = resolve_ops_decision(
            run_store,
            node_id=body.node_id,
            capability=body.capability,
            phase="maintain",
            arguments=body.arguments,
            approved=True,  # this endpoint IS the explicit approval
        )
        if not decision.allows_execution():
            raise HTTPException(status_code=409, detail=f"{decision.verdict}: {decision.reason}")
        if decision.dry_run:
            raise HTTPException(
                status_code=409,
                detail="this capability cannot execute (still dry-run after approval)",
            )
        try:
            result = execute_ops(decision, capability=body.capability, arguments=body.arguments)
        except OpsExecutionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "capability": result.capability,
            "executed": result.executed,
            "exit_code": result.exit_code,
            "output": result.output,
            "error": result.error,
            "evidence": result.evidence,
            "actor": body.actor,
        }

    @app.post("/api/schedules", status_code=201)
    def add_schedule(body: ScheduleRequest) -> dict[str, Any]:
        if body.caste in ("note", "script"):
            # note/script schedules are repo-less: the tick posts the text
            # (note) or runs the command supervisor-side and posts its output
            # (script, v44-F4) instead of dispatching a worker. The token-authed
            # API is the operator, so direct creation here is the same trust as
            # the operator's own crontab.
            try:
                interval = parse_interval(body.every)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if body.instructions is None:
                what = "the note text" if body.caste == "note" else "the shell command"
                raise HTTPException(
                    status_code=400,
                    detail=f"a {body.caste} schedule needs instructions ({what})",
                )
            try:
                validate_chain(run_store, name=body.name, chain=body.chain)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            schedule = make_schedule(
                name=body.name,
                repo="",
                instructions=body.instructions,
                interval_seconds=interval,
                worker_kind=body.caste,
                once=body.once,
                start_at=body.start_at,
                chain=body.chain,
            )
            run_store.add_schedule(schedule)
            return _schedule_view(run_store, schedule)
        if body.repo is None:
            raise HTTPException(status_code=400, detail="a worker schedule needs a repo")
        repo = resolve_repo_arg(body.repo, root, run_store)
        if not (repo / ".git").exists():
            raise HTTPException(status_code=400, detail=f"{repo} is not a git repository")
        try:
            interval = parse_interval(body.every)
            validate_chain(run_store, name=body.name, chain=body.chain)
            if body.template is not None:
                template = run_store.get_template(body.template)
                if template is None:
                    raise HTTPException(
                        status_code=404, detail=f"no template named {body.template!r}"
                    )
                schedule = make_template_schedule(
                    name=body.name,
                    template=template,
                    params=body.params,
                    repo=repo,
                    interval_seconds=interval,
                    ref=body.ref,
                    chain=body.chain,
                )
            else:
                if body.instructions is None:
                    raise HTTPException(
                        status_code=400, detail="a schedule needs instructions or a template"
                    )
                schedule = make_schedule(
                    name=body.name,
                    repo=repo,
                    instructions=body.instructions,
                    interval_seconds=interval,
                    worker_kind=body.caste,
                    ref=body.ref,
                    network=body.network,
                    env_allowlist=body.env_allowlist,
                    once=body.once,
                    start_at=body.start_at,
                    chain=body.chain,
                )
        except (TemplateError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        run_store.add_schedule(schedule)
        return _schedule_view(run_store, schedule)

    @app.patch("/api/schedules/{name}")
    def toggle_schedule(name: str, body: ScheduleToggle) -> dict[str, Any]:
        if not run_store.set_schedule_enabled(name, enabled=body.enabled):
            raise HTTPException(status_code=404, detail=f"no schedule named {name!r}")
        schedule = run_store.get_schedule(name)
        assert schedule is not None
        return _schedule_view(run_store, schedule)

    @app.delete("/api/schedules/{name}")
    def delete_schedule(name: str) -> dict[str, bool]:
        if not run_store.remove_schedule(name):
            raise HTTPException(status_code=404, detail=f"no schedule named {name!r}")
        return {"removed": True}

    # -- projects (VX Stage A) -----------------------------------------------

    @app.get("/api/projects")
    def get_projects() -> dict[str, Any]:
        return {"projects": [project_to_dict(project) for project in list_projects(run_store)]}

    @app.get("/api/projects/packs")
    def get_project_packs() -> dict[str, Any]:
        return {
            "packs": [
                _pack_view(pack)
                for pack in sorted(builtin_policy_packs().values(), key=lambda item: item.name)
            ]
        }

    @app.post("/api/projects/preview")
    def preview_project(body: ProjectPreviewRequest) -> dict[str, Any]:
        return preview_project_setup(
            root=root,
            run_store=run_store,
            project_id=body.project_id,
            name=body.name,
            strategy=body.strategy,
            phase=body.phase,
            pack_name=body.pack,
            repo_path=body.repo_path,
            repo_slug=body.repo_slug,
            template_names=body.template_names,
            policy_overrides=body.policy_overrides,
            seed_shell_commands=body.seed_shell_commands,
        )

    @app.post("/api/projects/setup", status_code=201)
    def setup_project(body: ProjectSetupRequest) -> dict[str, Any]:
        return setup_project_record(
            run_store=run_store,
            root=root,
            project_id=body.project_id,
            name=body.name,
            strategy=body.strategy,
            phase=body.phase,
            pack_name=body.pack,
            repo_path=body.repo_path,
            repo_slug=body.repo_slug,
            template_names=body.template_names,
            policy_overrides=body.policy_overrides,
            seed_default_schedules=body.seed_default_schedules,
            seed_shell_commands=body.seed_shell_commands,
        )

    @app.post("/api/projects/{project_id}/phase")
    def put_project_phase(project_id: str, body: ProjectPhaseRequest) -> dict[str, Any]:
        """v25-F1: the deck's /phase — same semantics as `skep project set-phase`."""
        return set_project_phase(run_store, project_id, body.phase)

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str) -> dict[str, Any]:
        project = project_from_store(run_store, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail=f"no project named {project_id!r}")
        return project_to_dict(project)

    @app.post("/api/projects", status_code=201)
    def put_project(body: ProjectRequest) -> dict[str, Any]:
        try:
            project = validate_project_definition(
                project_id=body.project_id,
                name=body.name,
                strategy=body.strategy,
                phase=body.phase,
                policy=body.policy,
                bindings=[
                    ProjectBinding(kind=binding.kind, value=binding.value)
                    for binding in body.bindings
                ],
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        for binding in project.bindings:
            _validate_project_binding(
                root=root,
                run_store=run_store,
                kind=binding.kind,
                value=binding.value,
            )
        return _persist_project(run_store, project)

    @app.delete("/api/projects/{project_id}")
    def delete_project(project_id: str) -> dict[str, bool]:
        if not run_store.remove_project_policy(project_id):
            raise HTTPException(status_code=404, detail=f"no project named {project_id!r}")
        return {"removed": True}

    # -- learned skills (v4 lifecycle: the two gates stay fail-closed) -------

    @app.get("/api/skills")
    def list_skills() -> dict[str, Any]:
        return {"skills": [_candidate_view(c) for c in run_store.list_candidates()]}

    @app.post("/api/skills/propose")
    def propose_skills() -> dict[str, Any]:
        drafts = propose(run_store, holder.current.audit_dir)
        return {"proposed": [_candidate_view(c) for c in drafts]}

    @app.post("/api/skills/{name}/test")
    def test_skill(name: str, body: SkillTestRequest) -> dict[str, Any]:
        """The G10 test gate. Runs a real candidate task — the request blocks
        until the test run finishes (FastAPI sync handlers run on a threadpool,
        so the daemon stays responsive)."""
        repo = resolve_repo_arg(body.repo, root, run_store)
        if not (repo / ".git").exists():
            raise HTTPException(status_code=400, detail=f"{repo} is not a git repository")
        try:
            candidate, result = run_candidate_test(
                run_store, holder.current, name, repo=repo, params=body.params, ref=body.ref
            )
        except SkillError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"candidate": _candidate_view(candidate), "passed": result.passed}

    @app.post("/api/skills/{name}/approve")
    def approve_skill(name: str, body: SkillDecision) -> dict[str, Any]:
        try:
            candidate, target = approve(
                run_store, name, actor=body.actor, note=body.note, registry_name=body.as_name
            )
        except SkillError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"candidate": _candidate_view(candidate), "template": target}

    @app.post("/api/skills/{name}/reject")
    def reject_skill(name: str, body: SkillDecision) -> dict[str, Any]:
        try:
            candidate = reject(run_store, name, actor=body.actor, note=body.note)
        except SkillError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"candidate": _candidate_view(candidate)}

    # -- skill distribution (v31): signed export + import preview ------------
    # Admission into the registry stays the CLI human gate (`skep skill import
    # --approve`, a file-local, explicit action). These routes let the daemon
    # hand out a signed bundle and disclose an incoming bundle's grants.

    @app.get("/api/skills/{name}/export")
    def export_skill(name: str) -> dict[str, Any]:
        from ..skill_bundle import bundle_skill, sign_bundle, skill_signing_key

        template = run_store.get_template(name)
        if template is None:
            raise HTTPException(status_code=404, detail=f"no skill/template named {name!r}")
        return sign_bundle(bundle_skill(template), skill_signing_key(holder.current.home))

    @app.post("/api/skills/import/preview")
    def preview_import(body: dict[str, Any]) -> dict[str, Any]:
        """Verify a bundle's signature and DISCLOSE its full grant surface —
        registers nothing (admission is the CLI --approve human gate)."""
        from ..skill_bundle import (
            grants_summary,
            skill_from_bundle,
            skill_grants,
            skill_signing_key,
            verify_bundle,
        )

        try:
            template = skill_from_bundle(body)
        except (ValueError, TemplateError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        verification = verify_bundle(body, skill_signing_key(holder.current.home))
        grants = skill_grants(template)
        return {
            "skill": template.name,
            "worker_kind": template.worker_kind,
            "verification": verification,
            "grants": grants,
            "grants_summary": grants_summary(grants),
            "admit_with": f"skep skill import <file> --approve  (verification: {verification})",
        }

    # -- repos (A7): clone by URL into the data volume -----------------------

    @app.get("/api/repos")
    def list_repos() -> dict[str, Any]:
        # v81-F10: same list the chat tool answers with — clones + workon dirs.
        repos = known_repos(root, run_store)
        for item in repos:
            if item["source"] != "clone":
                continue
            probe = subprocess.run(
                ["git", "-C", item["path"], "config", "--get", "remote.origin.url"],
                capture_output=True,
                text=True,
            )
            item["url"] = probe.stdout.strip()
        return {"repos": repos}

    @app.post("/api/repos", status_code=201)
    def add_repo(body: RepoRequest) -> dict[str, Any]:
        return register_repo(root, url=body.url, name=body.name)

    @app.delete("/api/repos/{name}")
    def delete_repo(name: str) -> dict[str, bool]:
        # v57-F8: shared with the carded chat verb; in-flight runs refuse it.
        return remove_registered_repo(run_store, root, name)

    # -- provider settings (A6): the worker's model, never the key itself ----

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        home = holder.current.home.parent
        if not profile_path(home).is_file():
            return {"configured": False}
        provider = load_profile(home).provider
        return {
            "configured": True,
            "provider": provider.name,
            "model": provider.model,
            "endpoint": provider.endpoint,
            "api_key_env": provider.api_key_env,
        }

    @app.put("/api/settings")
    def put_settings(body: ProviderSettings) -> dict[str, Any]:
        # Only the env-var *name* is ever stored (G2 posture); the secret value
        # reaches the worker through the allowlisted environment.
        run_personal_setup(
            holder.current.home.parent,
            provider=body.provider,
            model=body.model,
            endpoint=body.endpoint,
            api_key_env=body.api_key_env,
        )
        return get_settings()

    # -- channels (v26-F1): the operator surface v16 never had -----------------
    # Config + secrets were built and tested in v16 but nothing reachable
    # called them. Secrets are write-only: stored as 0600 files beside the
    # serve token, reported only as "configured", never returned.

    secrets_home = holder.current.home

    @app.get("/api/channels")
    def get_channels() -> dict[str, Any]:
        from .settings import channel_config_view

        return {"channels": channel_config_view(run_store, secrets_home)}

    @app.put("/api/channels/{channel}")
    def put_channel(channel: str, body: ChannelConfigRequest) -> dict[str, Any]:
        from .channels import CHANNELS, ChannelConfig, store_channel_secret
        from .settings import channel_config_view

        if channel not in CHANNELS:
            raise HTTPException(
                status_code=404, detail=f"unknown channel {channel!r}; known: {sorted(CHANNELS)}"
            )
        if body.signing_secret is not None and channel != "slack":
            raise HTTPException(
                status_code=400, detail="signing_secret only applies to slack webhooks"
            )
        from .channels import NOTIFICATION_LEVELS

        if body.notification_level is not None and body.notification_level not in (
            NOTIFICATION_LEVELS
        ):
            raise HTTPException(
                status_code=400,
                detail=(f"notification_level must be one of {', '.join(NOTIFICATION_LEVELS)}"),
            )
        current = run_store.get_channel_config(channel) or ChannelConfig(channel=channel)
        run_store.upsert_channel_config(
            ChannelConfig(
                channel=channel,
                enabled=current.enabled if body.enabled is None else body.enabled,
                channel_can_confirm=(
                    current.channel_can_confirm
                    if body.channel_can_confirm is None
                    else body.channel_can_confirm
                ),
                allowed_identities=(
                    current.allowed_identities
                    if body.allowed_identities is None
                    else tuple(body.allowed_identities)
                ),
                require_mention=(
                    current.require_mention
                    if body.require_mention is None
                    else body.require_mention
                ),
                auto_thread=(current.auto_thread if body.auto_thread is None else body.auto_thread),
                allowed_users=(
                    current.allowed_users
                    if body.allowed_users is None
                    else tuple(body.allowed_users)
                ),
                notification_level=(
                    current.notification_level
                    if body.notification_level is None
                    else body.notification_level
                ),
            )
        )
        if body.secret is not None:
            store_channel_secret(secrets_home, channel, body.secret)
        if body.signing_secret is not None:
            store_channel_secret(secrets_home, channel, body.signing_secret, part="signing")
        view: dict[str, Any] = channel_config_view(run_store, secrets_home)[channel]
        return view
