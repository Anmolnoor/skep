"""v90-F2: the confirmation card, summarized for a human.

The card used to render three things and say one: a fixed sentence ("The
assistant proposes X — nothing runs until you decide"), the MODEL-facing tool
description (deliberately long prose — the ``await_runs`` one is six lines), and
a raw dump of every argument. Three restatements of the same fact, and no risk
line anywhere.

This builds the three lines a human actually needs:

- ``headline`` — the thing itself: the argv, the URL, the branch. Not a
  sentence about it.
- ``purpose`` — one line on what the verb is for (the tool description's first
  sentence; the rest moves behind a details disclosure).
- ``risk`` — the specific reason this needs attention, or **None**. An invented
  risk is as bad as a buried one, so benign verbs get no line at all.

The risk text is derived from the guard classes the policy engine itself
consults (``shell_prefixes``), never re-worded per tool — a card that says
something the engine would not is worse than no card.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

from ..shell_prefixes import (
    is_ops_mutating_command,
    is_outbound_content_prefix,
    queen_shell_refusal,
)

# The "thing" a card is about, in priority order. One generic rule beats a
# 69-entry per-tool table: every mutating tool names its subject with one of
# these keys, and a tool that adds a new one falls back to its own name rather
# than to a wrong guess.
_SUBJECT_KEYS: tuple[str, ...] = (
    "argv",
    "command",
    "shell_command",
    "code",
    "url",
    "domain",
    "branch",
    "path",
    "repo",
    "task_id",
    "review_id",
    "name",
    "project_id",
    "query",
)

# Risk classes. Membership is the claim; the text is written once per class so
# it cannot drift tool by tool.
_SHELL_TOOLS = frozenset({"run_shell", "run_code", "start_process", "diagnose_run"})
_PUBLISHING_TOOLS = frozenset({"push_branch", "push_baseline", "open_pr", "merge_pr", "close_pr"})
_LANDING_TOOLS = frozenset({"land_run", "approve_review"})
_POLICY_TOOLS = frozenset(
    {
        "allow_command_review",
        "allow_env_bootstrap",
        "allow_fetch_domain",
        "allow_mcp_tool",
        "allow_shell_command",
        "apply_policy_preset",
        "copy_project_policy",
        "promote_skill_pack",
        "promote_tool",
        "set_operator_policy",
        "set_policy",
        # v97-F3: attaching a group widens the project's future runs;
        # set_policy_group carries its own sharper line in risk().
        "attach_policy_group",
    }
)
_DESTRUCTIVE_TOOLS = frozenset(
    {
        "delete_branch",
        "delete_note",
        "delete_policy_group",  # v97-F3 (delete refuses while attached; builtins revert)
        "delete_schedule",
        "delete_skill",
        "delete_task",
        "discord_delete_message",
        "discord_timeout_member",
        "forget_memory",
        "stop_process",
        "suspend_skill_pack",
        "suspend_tool",
        "unregister_mcp_server",
        "unregister_repo",
    }
)
_NETWORK_TOOLS = frozenset({"read_url", "register_mcp_server", "call_mcp_tool", "setup_browser"})


def _argv_of(args: dict[str, Any]) -> list[str]:
    """The argv a shell-ish card is about, however the tool spells it."""
    raw = args.get("argv")
    if isinstance(raw, list) and raw:
        return [str(part) for part in raw]
    for key in ("command", "shell_command"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            try:
                return shlex.split(value)
            except ValueError:  # unbalanced quotes — the raw string is the subject
                return [value]
    return []


def _render_value(value: Any) -> str:
    if isinstance(value, list):
        return shlex.join([str(part) for part in value])
    if isinstance(value, str):
        return value
    return str(value)


def headline(tool: str, args: dict[str, Any]) -> str:
    """One line: the verb and the thing it acts on, verbatim."""
    # v106-F5 (v101-F16): a PR verb names WHICH PR. "close_pr — repo" made two
    # cards in one batch indistinguishable, and the first subject key (repo)
    # swallowed the number that mattered. One branch, not a per-tool table —
    # it disambiguates merge_pr/close_pr batches generally.
    repo, pr = args.get("repo"), args.get("pr")
    if repo not in (None, "") and pr not in (None, ""):
        return f"{tool} — {_render_value(repo).strip()}#{_render_value(pr).strip()}"
    for key in _SUBJECT_KEYS:
        value = args.get(key)
        if value in (None, "", [], {}):
            continue
        rendered = _render_value(value).strip().replace("\n", " ")
        if not rendered:
            continue
        if len(rendered) > 160:
            rendered = f"{rendered[:157]}…"
        return f"{tool} — {rendered}"
    return tool


def purpose(description: str) -> str | None:
    """The tool description's first sentence: what the verb is for.

    The rest is model-facing prose and belongs behind the details disclosure,
    not on the card.
    """
    text = " ".join(description.split())
    if not text:
        return None
    # The "PROPOSE …ing (requires user confirmation)" wrapper steers the Queen
    # (this verb cards); the human reading the card learns both from the
    # buttons. Strip it here rather than un-tuning the descriptions.
    text = re.sub(r"^PROPOSE\s+", "", text)
    text = re.sub(r"\s*\(requires user confirmation[^)]*\)", "", text, count=1)
    # First sentence, but "e.g. "/"i.e. " are abbreviations, not boundaries.
    boundary = re.search(r"(?<!e\.g)(?<!i\.e)\.\s", text)
    first = text[: boundary.start() + 1] if boundary else text
    if len(first) > 200:
        first = f"{first[:197]}…"
    if not first:
        return None
    return first[:1].upper() + first[1:]


def _host_of(args: dict[str, Any]) -> str | None:
    import urllib.parse

    raw = args.get("url") or args.get("domain")
    if not isinstance(raw, str) or not raw.strip():
        return None
    value = raw.strip()
    if "://" not in value:
        return value
    return urllib.parse.urlparse(value).hostname or value


def risk(tool: str, args: dict[str, Any]) -> str | None:
    """The specific reason this needs a human, or None when there is none.

    Never invents a line: a benign read renders no risk at all.
    """
    if tool in _SHELL_TOOLS:
        argv = _argv_of(args)
        if not argv:
            return "runs a command on this machine"
        if is_outbound_content_prefix(argv):
            return (
                "sends content outward — public and permanent, and never grantable, "
                "so this cards every time (ADR 0044)"
            )
        if argv[0] in {"sudo", "doas"}:
            return "privilege escalation — it would launder every command guard beneath it"
        if is_ops_mutating_command(argv):
            return "changes machine state (service, files, or backups) — approve-once only"
        refusal = queen_shell_refusal(argv)
        if refusal is not None:
            # Should never reach a card (the verb refuses it outright), but if a
            # path ever proposes one, say the engine's own reason, not a guess.
            return refusal
        return "runs a command on this machine"
    if tool == "set_policy_group":
        # v97-F3 (I8): the two shapes carry opposite blast radii — say which.
        if args.get("fork_from"):
            return (
                "forks a copy-on-write group — the source group and its other "
                "attached projects stay untouched"
            )
        return "a group edit reaches EVERY project attached to this group on its next dispatch"
    if tool == "close_pr" and args.get("delete_branch"):
        # v106-F5 (v101-F16): the field cascade — deleting the head ref made
        # GitHub close an UPSTREAM PR built on it, and the card never said the
        # ref was part of the deal. Static text: no network call on a card the
        # operator is already waiting behind.
        return (
            "closes the PR AND deletes its head ref — GitHub then closes every "
            "other PR built on that branch, including PRs on upstream repos, "
            "and the ref deletion is not undone by a revert"
        )
    if tool in _PUBLISHING_TOOLS:
        return "publishes to the remote — visible outside this machine, and not undone by a revert"
    if tool in _LANDING_TOOLS:
        return "applies a patch to a real branch — landing IS the commit"
    if tool in _POLICY_TOOLS:
        return "widens what future runs may do without asking you again"
    if tool in _DESTRUCTIVE_TOOLS:
        return "removes state that cannot be restored from here"
    if tool in _NETWORK_TOOLS:
        host = _host_of(args)
        target = f" from {host}" if host else ""
        return f"pulls remote content{target} into this conversation"
    return None


def card_summary(tool: str, args: Any, description: str = "") -> dict[str, Any]:
    """The human-facing three lines for one proposed action.

    ``risk`` is omitted entirely when there is nothing notable, so the card
    stays quiet about quiet things.
    """
    safe_args: dict[str, Any] = args if isinstance(args, dict) else {}
    summary: dict[str, Any] = {"headline": headline(tool, safe_args)}
    why = purpose(description)
    if why is not None:
        summary["purpose"] = why
    danger = risk(tool, safe_args)
    if danger is not None:
        summary["risk"] = danger
    return summary
