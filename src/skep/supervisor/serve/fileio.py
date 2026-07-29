"""Queen-side governed file reads (v51-F2, ADR 0023).

The Queen is an operator-trust process — the OS sandbox is for workers. What
governs Queen file access is the policy engine: explicit ``filesystem``-scope
rules in the stored policy document win (a deny is a hard deny — no card),
and an unmatched path falls back to the operator roots — the skep home, the
registered-repos root, and every workon-bound project path. Inside a root a
read executes in the turn; outside, the call becomes a confirmation card
naming the exact resolved path (the ``call_mcp_tool`` precedent, v40-F10).

Writes intentionally do not exist here: no observed demand, and a Queen
writing into a repo's working tree needs a dirty-worktree design answer
first (plans/v51 — deferred).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from ..autonomy import AutonomyDecision
from ..policy_resolver import resolve_operator_policy
from ..policy_schema import DEFAULT_DENY_RULE_ID
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
    store: RunStore, holder: ConfigHolder, *, action: str, path: str
) -> AutonomyDecision:
    """Decide one Queen-side filesystem access against the filesystem scope.

    Path resolution happens BEFORE the decision (symlinks cannot smuggle a
    read out of a root), explicit scope rules win, and the unmatched
    fallback is: operator roots allow, everything else cards.
    """
    if not path.strip():
        return AutonomyDecision(verdict="deny", reason="filesystem.deny.missing_path", detail=path)
    try:
        target = Path(path).expanduser().resolve()
    except OSError as exc:
        return AutonomyDecision(
            verdict="deny", reason="filesystem.deny.unresolvable_path", detail=f"{path}: {exc}"
        )
    # v52-F3: the standing operator policy decides — the global document's
    # rules keep their effect, composed with the Queen-only operator overlay.
    policy = resolve_operator_policy(store)
    decision = policy.decision("filesystem", action, str(target))
    if decision.rule_id != DEFAULT_DENY_RULE_ID:
        return AutonomyDecision(
            verdict=decision.verdict,
            reason=f"filesystem.{decision.verdict}.scope_rule",
            detail=str(target),
            decided_by=decision.decided_by,
        )
    label = policy.template or "policy"
    for root in operator_roots(store, holder):
        if target == root or root in target.parents:
            return AutonomyDecision(
                verdict="allow",
                reason="filesystem.allow.operator_root",
                detail=str(target),
                decided_by=f"{label}/operator-root",
            )
    # v59-F9: a nonexistent path outside the roots gets no card — the card
    # protects reading a real file, and a probe of an invented path (field
    # test 2026-07-18: ~15 SKEP_HOME/… cards, some auto-denied minutes later)
    # interrupts the operator while protecting nothing. Fail fast instead.
    if not target.exists():
        return AutonomyDecision(
            verdict="deny",
            reason="filesystem.deny.no_such_path",
            detail=str(target),
            decided_by=f"{label}/no-such-path",
        )
    return AutonomyDecision(
        verdict="require_approval",
        reason="filesystem.require_approval.outside_operator_roots",
        detail=str(target),
        decided_by=f"{label}/outside-operator-roots",
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
) -> dict[str, Any]:
    """ripgrep under one directory: content matches (``rg -n``) or file names."""
    root = Path(path).expanduser().resolve()
    if not root.exists():
        return {"error": f"no such path: {root}"}
    if target == "files":
        argv = ["rg", "--files"]
        if file_glob:
            argv += ["--glob", file_glob]
        argv.append(str(root))
    else:
        argv = ["rg", "--line-number", "--no-heading", "--max-count", "10"]
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
    return {
        "path": str(root),
        "target": target,
        "matches": hits[:MAX_SEARCH_HITS],
        "truncated": len(hits) > MAX_SEARCH_HITS,
    }
