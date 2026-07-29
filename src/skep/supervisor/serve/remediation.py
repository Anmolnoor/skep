"""Human-facing remediation hints for known failure classes (v19-F12).

``remediation_for`` maps a run's failure/verification detail string to a
one-line "what to do next". The table is ordered — first match wins — and pure
data, so a new class of failure is a one-line addition.
"""

from __future__ import annotations

# Each rule is (all-of-these-substrings-must-be-present, hint).
_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("is not in the task network allowlist",),
        "The worker could not reach the configured LLM provider. Re-check LLM settings "
        "(Settings → Provider); after fix v19-F2 this should not recur — if it does, file a bug.",
    ),
    (
        ("is already used by worktree",),
        "The agent tried to switch branches inside its isolated worktree. Skep lands changes "
        "as a patch — re-run the task; the worker no longer needs a branch.",
    ),
    (
        ("branch operations are managed by the skep supervisor",),
        "The worker cannot create branches; approve the run and land it with "
        "`skep review <id> --approve --branch <name>` (or the land_run action's "
        "branch option).",
    ),
    (
        ("nothing to commit",),
        "The work is already committed on the run's base branch — there is nothing new to "
        "commit. If the goal is a named branch, it may already exist (check repo_state); "
        "land completed work with the land_run action's branch option.",
    ),
    (
        ("staging and committing are managed by the skep supervisor",),
        "The worker cannot stage or commit; the landing approval is the commit. Approve the "
        "completed run and land it with `skep review <id> --approve --branch <name>` (or the "
        "land_run action's branch option).",
    ),
    (
        ("requires approval for command",),
        "The run is waiting for your approval. Approving resumes it automatically.",
    ),
    (
        ("stdout did not match expected output",),
        "The agent guessed a command's exact output and guessed wrong. Re-run; verification "
        "is exit-code based as of v19-F6.",
    ),
    (
        ("provider calls require a task network allowlist",),
        "No network allowlist was set for this run; configure the provider or pass a network list.",
    ),
    (
        ("exit 128", "fatal:"),
        "Git rejected a command inside the worktree. See the command log for the exact "
        "fatal message.",
    ),
)


def remediation_for(details: str | None) -> str | None:
    """The first matching one-line remediation hint for ``details``, or None."""
    if not details:
        return None
    for needles, hint in _RULES:
        if all(needle in details for needle in needles):
            return hint
    return None
