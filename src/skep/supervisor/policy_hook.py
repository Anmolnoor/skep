"""The Claude Code PreToolUse hook's two pure halves (P3, ADR 0053).

``hook_probe`` says which ``(scope, action, value)`` a tool call exercises
in skep's closed vocabulary (``policy_schema.SCOPE_ACTIONS``), or ``None``
when the engine has no rule for it — then the hook stays silent and Claude
Code's own permissions decide. ``hook_output`` renders a recorded decision
(``serve.actions.policy_decide``) in the hook's JSON.

Mapping, and why:

- ``Bash`` → ``shell.run <command>``: the command exactly as typed; the
  shell matcher is prefix-tokens, the same one the worker's allowlist uses.
- ``Edit``/``Write``/``MultiEdit``/``NotebookEdit`` → ``filesystem.write``:
  a path inside the hook's ``cwd`` (the checkout) is the run contract's
  symbolic ``workspace`` — the pattern every coding run is granted (``fs:
  workspace``); a path outside stays literal so only a trusted-root rule
  (``fs:root:<root>/*``) can admit it.
- ``WebFetch`` → ``network.fetch <host>``, ``WebSearch`` →
  ``network.search <query>``: the Queen-side lanes, decided by the operator
  document.
- ``Read``/``Glob``/``Grep`` and every other tool → no opinion: reads are a
  contract permission (``read: ["workspace"]``), not a document rule, and
  a hook that denied every read would be inventing policy (I5).

Verdict → hook decision: ``allow`` → ``allow``; ``deny`` → ``deny``;
``require_approval`` → ``ask``, and anything else → ``ask`` — never a silent
allow. ADR 0059 P3-1: "no rule matched" is ``decide()``'s own answer
(``default/unmatched``, ask for ``shell.run`` and every lane the hook maps),
so the hook no longer remaps a deny it cannot tell from an explicit one.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from .policy_paths import workspace_value

_WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
_HOOK_DECISIONS = {"allow": "allow", "require_approval": "ask", "deny": "deny"}


def hook_probe(payload: dict[str, Any]) -> tuple[str, str, str] | None:
    """``(scope, action, value)`` for a PreToolUse payload, or None."""
    tool = str(payload.get("tool_name") or "")
    inputs = payload.get("tool_input") or {}
    if not isinstance(inputs, dict):
        inputs = {}
    cwd = str(payload.get("cwd") or "")
    if tool == "Bash":
        return ("shell", "run", str(inputs.get("command") or ""))
    if tool in _WRITE_TOOLS:
        path = str(inputs.get("file_path") or inputs.get("notebook_path") or "")
        return ("filesystem", "write", workspace_value(path, cwd))
    if tool == "WebFetch":
        return ("network", "fetch", urlsplit(str(inputs.get("url") or "")).hostname or "")
    if tool == "WebSearch":
        return ("network", "search", str(inputs.get("query") or "*"))
    return None


def hook_output(record: dict[str, Any] | None, *, error: str | None = None) -> dict[str, Any]:
    """The PreToolUse hook JSON for a recorded decision — or ``ask`` with the
    reason when skep could not decide (never a silent allow)."""
    if record is None:
        decision, reason = "ask", f"skep could not decide: {error or 'unknown error'}"
    else:
        decision = _HOOK_DECISIONS.get(str(record.get("verdict")), "ask")
        reason = (
            f"skep: {record.get('verdict')} by {record.get('decided_by')} "
            f"({record.get('source')} policy)"
        )
        if record.get("layer") == "default":
            reason += " — no rule matches, so a human answers"
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }
