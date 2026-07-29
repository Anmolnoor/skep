---
name: claude-code
description: delegate a task to the Claude Code CLI backend
---

# Delegate to Claude Code

Tools: dispatch_run, get_run, effective_policy

skep runs Claude Code as a worker BACKEND — same contract, worktree,
sandbox, and patch-approval landing as every run.

1. Pick it when the task is large, multi-file, or benefits from a
   frontier coding model, and the operator has the CLI + credentials
   installed on this host.
2. `dispatch_run` with `backend='claude_code'` (check
   `effective_policy` first — the project must allow the backend and
   its network needs).
3. The brief matters MORE with a strong model: state the acceptance
   check up front; a vague brief returns a confident wrong patch.
4. Landing is unchanged (G10 re-verify, patch approval) — the backend
   never gets a shortcut around the walls. `get_run` and quote the
   supervisor-side verification, not the backend's own claim.
