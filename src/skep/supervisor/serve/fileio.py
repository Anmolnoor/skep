"""Queen-side governed file reads (v51-F2, ADR 0023).

The Queen is an operator-trust process — the OS sandbox is for workers. What
governs Queen file access is ``policy_v3.decide()`` (ADR 0059 P3-2), the
same decision REST and the CLI give with no repo: the secret floor first,
then explicit ``filesystem`` rules (a deny is a hard deny — no card), then
the operator roots — the skep home, the registered-repos root, and every
workon-bound project path — as generated ``global/operator-root`` allows.
Inside a root a read executes in the turn; outside, the call becomes a
confirmation card naming the exact resolved path (the ``call_mcp_tool``
precedent, v40-F10).

Writes intentionally do not exist here: no observed demand, and a Queen
writing into a repo's working tree needs a dirty-worktree design answer
first (plans/v51 — deferred).
"""

from __future__ import annotations

import os
import subprocess
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from ..autonomy import AutonomyDecision
from ..policy_v3 import OPERATOR_ROOT, Request, floor_verdict, queen_decide
from ..roles import Role
from ..secret_floor import secret_floor_reason
from ..store import RunStore
from .registry import repos_root
from .settings import ConfigHolder

DEFAULT_READ_LINES = 200
MAX_READ_LINES = 1000
MAX_LINE_CHARS = 500
MAX_SEARCH_HITS = 100
SEARCH_TIMEOUT_SECONDS = 30


def operator_roots(store: RunStore, holder: ConfigHolder) -> list[Path]:
    """Where the operator already works: the skep home, the repos root, and
    every workon-bound (``repo_path``) project directory."""
    candidates = [holder.current.home, repos_root(holder)]
    for project in store.list_project_policies():
        for binding in store.project_bindings(project.project_id):
            if binding.binding_kind == "repo_path":
                candidates.append(Path(binding.binding_value))
    roots: list[Path] = []
    for candidate in candidates:
        try:
            roots.append(candidate.expanduser().resolve())
        except OSError:  # pragma: no cover - an unreadable root just drops out
            continue
    return roots


def queen_filesystem_decision(
    store: RunStore,
    holder: ConfigHolder,
    *,
    action: str,
    path: str,
    role: Role | None = None,
) -> AutonomyDecision:
    """One Queen-side filesystem access, decided by ``policy_v3.decide()`` as
    ``filesystem.<action>`` with the chat's role (ADR 0059 P3-2).

    Path resolution happens BEFORE the decision (symlinks cannot smuggle a
    read out of a root); the secret floor judges the requested path and its
    realpath before any document is read (ES-F1)."""
    if not path.strip():
        return AutonomyDecision(verdict="deny", reason="filesystem.deny.missing_path", detail=path)
    try:
        target = Path(path).expanduser().resolve()
    except OSError as exc:
        return AutonomyDecision(
            verdict="deny", reason="filesystem.deny.unresolvable_path", detail=f"{path}: {exc}"
        )
    floor = floor_verdict("filesystem", path, action=action, home=holder.current.home)
    if floor is not None:
        return AutonomyDecision(
            verdict="deny",
            reason="filesystem.deny.secret_floor",
            detail=floor["reason"],
            decided_by=floor["decided_by"],
        )
    verdict = queen_decide(holder, store, Request("filesystem", action, str(target)), role=role)
    unmatched = verdict.layer == "default" or verdict.layer.startswith("role:")
    if verdict.verdict == "require_approval" and unmatched and not target.exists():
        # v59-F9: a nonexistent path no rule covers gets no card — the card
        # protects reading a real file, and a probe of an invented path (field
        # test 2026-07-18: ~15 SKEP_HOME/… cards) interrupts the operator while
        # protecting nothing. A validity check, so it sits outside decide().
        return AutonomyDecision(
            verdict="deny",
            reason="filesystem.deny.no_such_path",
            detail=str(target),
            decided_by="default/no-such-path",
        )
    if verdict.rule_id == OPERATOR_ROOT:
        reason = "filesystem.allow.operator_root"
    elif verdict.layer == "default":
        reason = "filesystem.require_approval.outside_operator_roots"
    elif verdict.layer.startswith("role:"):
        reason = f"filesystem.{verdict.verdict}.role_layer"
    else:
        reason = f"filesystem.{verdict.verdict}.scope_rule"
    return AutonomyDecision(
        verdict=verdict.verdict, reason=reason, detail=str(target), decided_by=verdict.decided_by
    )


def enclosing_git_repo(target: Path) -> Path | None:
    """The nearest ancestor (or the path itself) that is a git repo root."""
    for candidate in (target, *target.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _landing_branch_for_repo(store: RunStore, repo_dir: Path) -> str | None:
    """v79-F3: the repo's configured auto_apply_branch, if a project binds it."""
    for kind, value in (("repo_slug", repo_dir.name), ("repo_path", str(repo_dir))):
        project = store.project_for_binding(kind, value)
        if project is not None:
            branch = project.policy.get("auto_apply_branch")
            return str(branch) if branch else None
    return None


def read_file_at_ref(
    repo_dir: Path,
    relpath: str,
    ref: str,
    *,
    offset: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """v79-F3: read one file from a git ref (``git show``), numbered like
    read_file_result. Read-only — no checkout moves, no worktree writes."""
    try:
        show = subprocess.run(
            ["git", "-C", str(repo_dir), "show", f"{ref}:{relpath}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"git show timed out reading {relpath!r} at {ref!r}"}
    if show.returncode != 0:
        detail = show.stderr.strip()[:200]
        return {"error": f"cannot read {relpath!r} at {ref!r}: {detail}"}
    count = min(int(limit or DEFAULT_READ_LINES), MAX_READ_LINES)
    start = max(int(offset or 1), 1)
    lines: list[str] = []
    total = 0
    for number, line in enumerate(show.stdout.splitlines(), start=1):
        total = number
        if number < start or len(lines) >= count:
            continue
        text = line
        if len(text) > MAX_LINE_CHARS:
            text = text[:MAX_LINE_CHARS] + " …"
        lines.append(f"{number}\t{text}")
    return {
        "path": str(repo_dir / relpath),
        "ref": ref,
        "content": "\n".join(lines),
        "lines_shown": len(lines),
        "total_lines": total,
        "offset": start,
    }


def read_file_branch_aware(
    store: RunStore,
    path: str,
    *,
    ref: str | None = None,
    offset: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """v79-F3: the branch-aware Queen read.

    Work lands on ``skep/`` branches while the base clone stays on its default
    checkout, so a plain filesystem read answered "not a file" for files that
    exist (field test 2026-07-21: 7 consecutive misses, two false "patch not
    landed" claims). An explicit ``ref`` reads via ``git show``; a miss with
    no ref falls back to the project's landing branch; a final miss teaches
    which branch to ask for (I9). The policy guard already ran on the path —
    the boundary does not move (I5)."""
    target = Path(path).expanduser().resolve()
    repo_dir = enclosing_git_repo(target)
    if ref is not None:
        if repo_dir is None:
            return {
                "error": f"{target} is not inside a git repository, so ref={ref!r} "
                "cannot be resolved"
            }
        relpath = target.relative_to(repo_dir).as_posix()
        return read_file_at_ref(repo_dir, relpath, ref, offset=offset, limit=limit)
    result = read_file_result(path, offset=offset, limit=limit)
    if "error" not in result or repo_dir is None or target.exists():
        return result
    relpath = target.relative_to(repo_dir).as_posix()
    landing = _landing_branch_for_repo(store, repo_dir)
    if landing is not None:
        fallback = read_file_at_ref(repo_dir, relpath, landing, offset=offset, limit=limit)
        if "error" not in fallback:
            fallback["note"] = (
                f"not on the checked-out branch; served from {landing!r} "
                "(the project's landing branch)"
            )
            return fallback
    checked_out = (
        subprocess.run(
            ["git", "-C", str(repo_dir), "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        or "(detached)"
    )
    skep_branches = subprocess.run(
        [
            "git",
            "-C",
            str(repo_dir),
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/heads/skep/",
        ],
        capture_output=True,
        text=True,
    ).stdout.split()
    hint = (
        f" — landed work lives on {', '.join(skep_branches[:5])}; pass ref to read from one"
        if skep_branches
        else ""
    )
    return {"error": (f"not a file on the checked-out branch ({checked_out!r}): {target}{hint}")}


def resolves_elsewhere(path: str) -> str | None:
    """W38: where ``path`` really leads when a link makes it differ from the
    path as written (``~`` expanded, made absolute) — None when it does not."""
    try:
        written = Path(os.path.abspath(Path(path).expanduser()))
        real = written.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    return None if real == written else str(real)


# W38: how many symlinks a search lists, and how many directory entries it
# walks to find them.
MAX_SYMLINKS = 20
SYMLINK_WALK_LIMIT = 20_000


def _symlinks(root: Path, name_glob: str | None, skep_home: Path | None) -> list[dict[str, str]]:
    """The symlinks under ``root`` (not followed — rg skips them too), each
    with where it leads; a link into a credential file is left out.
    ponytail: a bounded os.walk that ignores .gitignore; skips dot dirs."""
    found: list[dict[str, str]] = []
    seen = 0
    for directory, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in [*files, *dirs]:
            seen += 1
            link = Path(directory) / name
            if not link.is_symlink() or (name_glob and not fnmatch(name, name_glob)):
                continue
            if secret_floor_reason(link, skep_home=skep_home) is None:
                found.append({"path": str(link), "target": str(link.resolve())})
            if len(found) >= MAX_SYMLINKS:
                return found
        if seen > SYMLINK_WALK_LIMIT:
            break
    return found


def read_file_result(
    path: str, *, offset: int | None = None, limit: int | None = None
) -> dict[str, Any]:
    """Read one file as numbered lines, bounded for a small model's context."""
    target = Path(path).expanduser().resolve()
    if not target.is_file():
        return {"error": f"not a file: {target}"}
    count = min(int(limit or DEFAULT_READ_LINES), MAX_READ_LINES)
    start = max(int(offset or 1), 1)
    lines: list[str] = []
    total = 0
    try:
        with target.open("r", encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle, start=1):
                total = number
                if number < start or len(lines) >= count:
                    continue
                text = line.rstrip("\n")
                if len(text) > MAX_LINE_CHARS:
                    text = text[:MAX_LINE_CHARS] + " …"
                lines.append(f"{number}\t{text}")
    except OSError as exc:
        return {"error": f"read failed: {exc}"}
    return {
        "path": str(target),
        "content": "\n".join(lines),
        "lines_shown": len(lines),
        "total_lines": total,
        "offset": start,
    }


def search_files_result(
    pattern: str,
    *,
    path: str,
    target: str = "content",
    file_glob: str | None = None,
    skep_home: Path | None = None,
) -> dict[str, Any]:
    """ripgrep under one directory: content matches (``rg -n``) or file names.

    ES-F1: hits in secret-floor files are dropped and the result's ``note``
    says so — the search still answers, it just never quotes a credential."""
    root = Path(path).expanduser().resolve()
    if not root.exists():
        return {"error": f"no such path: {root}"}
    if target == "files":
        argv = ["rg", "--files"]
        if file_glob:
            argv += ["--glob", file_glob]
        argv.append(str(root))
    else:
        # --null: a NUL after each path, so the floor filter splits it exactly.
        argv = ["rg", "--null", "--line-number", "--no-heading", "--max-count", "10"]
        if file_glob:
            argv += ["--glob", file_glob]
        argv += ["-e", pattern, str(root)]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=SEARCH_TIMEOUT_SECONDS)
    except FileNotFoundError:
        return {"error": "ripgrep (rg) is not installed on the host"}
    except subprocess.TimeoutExpired:
        return {"error": f"search timed out after {SEARCH_TIMEOUT_SECONDS}s"}
    if proc.returncode not in (0, 1):  # 1 = clean no-match
        return {"error": proc.stderr.strip()[:400] or f"rg exit {proc.returncode}"}
    hits = proc.stdout.splitlines()
    if target == "files" and pattern.strip():
        # ponytail: rg --files has no name filter — fnmatch the tail here.
        from fnmatch import fnmatch

        wanted = pattern if any(ch in pattern for ch in "*?[") else f"*{pattern}*"
        hits = [hit for hit in hits if fnmatch(Path(hit).name, wanted)]
    found = len(hits)
    if target == "files":
        hits = [hit for hit in hits if secret_floor_reason(hit, skep_home=skep_home) is None]
    else:
        kept: list[str] = []
        for hit in hits:
            name, sep, rest = hit.partition("\0")
            if sep and secret_floor_reason(name, skep_home=skep_home) is not None:
                continue
            kept.append(f"{name}:{rest}" if sep else hit)
        hits = kept
    skipped = found - len(hits)
    result: dict[str, Any] = {
        "path": str(root),
        "target": target,
        "matches": hits[:MAX_SEARCH_HITS],
        "truncated": len(hits) > MAX_SEARCH_HITS,
    }
    notes: list[str] = []
    if skipped:
        notes.append(
            f"{skipped} hit(s) in credential files were left out — the secret floor "
            "(.env files, ~/.ssh and friends, skep's own tokens) is never searched or read"
        )
    if root.is_dir():
        name_glob: str | None
        if target == "files" and pattern.strip():
            name_glob = pattern if any(ch in pattern for ch in "*?[") else f"*{pattern}*"
        else:
            name_glob = file_glob
        links = _symlinks(root, name_glob, skep_home)
        if links:
            # W38: a link is listed, never followed — the model must not report
            # "no such file" for one, nor read through it without a card
            result["symlinks"] = links
            notes.append(
                "symlinks are listed with their targets, not followed or searched — "
                "read_file a link to read it (a card names where it resolves)"
            )
    if notes:
        result["note"] = "; ".join(notes)
    return result
