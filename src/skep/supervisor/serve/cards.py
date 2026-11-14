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

import json
import re
import shlex
import urllib.parse
from typing import Any

from ..shell_prefixes import (
    is_ops_mutating_command,
    is_outbound_content_prefix,
    queen_command_line_refusal,
    queen_shell_refusal,
)
from ..store import ChatTaint, RunStore

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
    "rule_id",  # v109-F9: revoke_policy_rule — the card names WHICH grant goes
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
        "remove_project",  # ES-F235: the deck's /unregister
    }
)
_NETWORK_TOOLS = frozenset({"read_url", "register_mcp_server", "call_mcp_tool", "setup_browser"})

# P4-2 (ADR 0059 D6): the policy writes a HIGH card covers — every verb that
# widens or reshapes standing policy. ``revoke_policy_rule`` only narrows and
# stays an ordinary card; a card keep=always is the operator's choice on an
# ordinary card in the web UI or CLI (a channel confirm never names a keep).
_HIGH_POLICY_TOOLS = _POLICY_TOOLS | {
    "approve_policy_baseline",
    "detach_policy_group",  # a group can carry denies: detaching it widens
    "set_policy_group",
    "set_role",  # the chat's role is a ceiling on the Queen's own lanes
}
_DISPATCH_TOOLS = frozenset({"dispatch_run", "quick_edit", "setup_project", "batch_dispatch"})


def _wildcard_network(args: dict[str, Any]) -> bool:
    for key in ("network", "source_allowlist"):
        value = args.get(key)
        if isinstance(value, list) and any(str(host).strip() == "*" for host in value):
            return True
    return False


_TRIFECTA = "this chat has read private data and taken in outside content"


def _named_by_operator(store: RunStore, server: str, name: str) -> bool:
    """No per-server MCP trust flag exists. The one trust mark there is: the
    operator's ``allow_mcp_tool`` grant naming exactly this ``server:tool``
    (itself a HIGH card). A glob grant (``bank:*``) is not a name."""
    from ..policy_schema import POLICY_DOCUMENT_SETTINGS_KEY, document_from_settings

    document = document_from_settings(store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY))
    exact = f"{server}:{name}"
    return document is not None and any(
        rule.scope in ("mcp", "email", "browse") and rule.pattern == exact
        for rule in document.learned
    )


def _changes_provider(args: dict[str, Any], store: RunStore | None) -> bool:
    """W11: ``set_assistant_model`` that carries a base URL or another
    protocol (scope 'chat' applies neither). No store: fail closed."""
    if args.get("scope") == "chat":
        return False
    if str(args.get("base_url") or "").strip():
        return True
    if args.get("protocol") in (None, ""):
        return False
    from .llm import LLM_PROTOCOL

    return store is None or args["protocol"] != store.get_setting(LLM_PROTOCOL)


def _doubly_tainted(taint: ChatTaint | None) -> bool:
    return taint is not None and taint.private is not None and taint.untrusted is not None


def high_tier(
    tool: str,
    args: Any,
    *,
    taint: ChatTaint | None = None,
    store: RunStore | None = None,
    manifest: dict[str, Any] | None = None,
) -> str | None:
    """P4-2 (ADR 0059 D6): why this action is a HIGH card, or None.

    The one place the tier is decided — the card renders it and every
    confirm path re-asks it (``chat.verdict_events``). A HIGH card looks
    different, holds once, never folds, is never auto-allowed and is never
    confirmable from a messenger channel. Without ``store`` an MCP call
    cannot be shown to be a read, so it is HIGH (fail closed). A dispatch's
    ``manifest`` (``actions.dispatch_manifest``) is HIGH when its run would
    reach any host, whatever the call's own args said."""
    safe: dict[str, Any] = args if isinstance(args, dict) else {}
    if _doubly_tainted(taint):
        return _TRIFECTA
    if tool == "propose_schedule":
        if safe.get("caste") == "script":
            return "a script schedule runs a command as you, on this host, unattended"
        if safe.get("chain"):
            return "a chained schedule feeds another schedule's output into this one"
        existing = None if store is None else store.get_schedule(str(safe.get("name") or ""))
        if existing is not None and existing.worker_kind == "script":
            return f"it replaces your script schedule {existing.name!r}"
        return None
    if tool == "call_mcp_tool":
        from ..mcp_client import mcp_call_is_read

        server, name = str(safe.get("server_id") or ""), str(safe.get("tool") or "")
        if store is not None and (
            mcp_call_is_read(store, server, name) or _named_by_operator(store, server, name)
        ):
            return None
        return "an MCP tool that acts (not a read), and you have not allowed it by name"
    if tool == "register_mcp_server" or tool == "setup_browser":
        return "it adds an MCP server whose tools can act as you"
    if tool == "promote_tool":
        return "it installs a forged tool: its code runs on this host"
    if tool == "promote_skill_pack":
        return "it activates a skill pack's scripts"
    if tool in _HIGH_POLICY_TOOLS:
        return "it changes standing policy"
    if tool in ("add_provider", "use_provider") or (
        tool == "set_assistant_model" and _changes_provider(safe, store)
    ):
        # W11: every later turn, private data included, goes to that endpoint
        return "it points the assistant at another LLM endpoint: every later turn goes there"
    if tool in ("set_schedule_enabled", "run_schedule_now") and (
        tool == "run_schedule_now" or safe.get("enabled") is not False  # executor: bool(...)
    ):
        existing = None if store is None else store.get_schedule(str(safe.get("name") or ""))
        if existing is None or existing.worker_kind == "script":  # unknown: fail closed
            return f"a script schedule {SCRIPT_SENTENCE}"
    overrides = safe.get("policy_overrides")
    if tool == "setup_project" and (
        safe.get("unlocked") is True
        or (isinstance(overrides, dict) and overrides.get("unlocked") is True)
    ):
        # ES-F142 (ADR 0061): a whole project's worker asks stop asking
        return "it turns unlocked mode on: the project's sandboxed workers stop asking"
    if tool == "setup_project" and (overrides or safe.get("groups")):
        # the overlay spells any knob (coding_engine, default_network, …) and
        # groups= is attach_policy_group: both write the project's policy
        return "it writes the project's policy (policy_overrides or groups)"
    if tool == "setup_project" and safe.get("codex_chatgpt_login") not in (None, False):
        # ES-F121 follow-up: the same policy write through its own sugar arg —
        # every later codex run spends the operator's plan on chatgpt.com
        return (
            "it lets the project's codex runs use your ChatGPT plan login "
            "(repo content goes to chatgpt.com)"
        )
    if manifest is not None and any(
        "*" in (m.get("network") or []) for m in [manifest, *(manifest.get("members") or [])]
    ):  # D23: a batch is HIGH when any of its runs may reach any host
        return "the run may reach any host on the internet (network '*')"
    if tool in _DISPATCH_TOOLS:
        tasks = safe.get("tasks") if tool == "batch_dispatch" else [safe]
        for task in tasks if isinstance(tasks, list) else []:
            if not isinstance(task, dict):
                continue
            if _wildcard_network(task):
                return "the run may reach any host on the internet (network '*')"
            if task.get("engine") not in (None, ""):
                return f"it picks the coding agent itself (engine {task['engine']!r})"
    if tool == "start_research" and _wildcard_network(safe):
        return "the run may reach any host on the internet (network '*')"
    return None


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


def headline(tool: str, args: dict[str, Any], *, limit: int | None = 160) -> str:
    """One line: the verb and the thing it acts on, verbatim. ``limit`` None:
    never shortened (a folded card's items — each is what one approval runs)."""
    # v106-F5 (v101-F16): a PR verb names WHICH PR. "close_pr — repo" made two
    # cards in one batch indistinguishable, and the first subject key (repo)
    # swallowed the number that mattered. One branch, not a per-tool table —
    # it disambiguates merge_pr/close_pr batches generally.
    repo, pr = args.get("repo"), args.get("pr")
    if repo not in (None, "") and pr not in (None, ""):
        return f"{tool} — {_render_value(repo).strip()}#{_render_value(pr).strip()}"
    # v109-F3: a review card's subject is the approval's reason, not its UUID.
    # The gate mirror carries the reason in args (run_status.py), and since the
    # landing reason now names the task ('land "…" → branch'), the operator
    # reads WHAT is waiting instead of `approve_review — <36 hex chars>`.
    if tool in {"approve_review", "deny_review"}:
        reason = str(args.get("reason") or "").strip().replace("\n", " ")
        if reason:
            if len(reason) > 160:
                reason = f"{reason[:157]}…"
            return f"{tool} — {reason}"
    if tool == "call_mcp_tool":
        # L16: which tool, on which server, with what — an amount is the point
        rendered = f"{args.get('server_id')}:{args.get('tool')}"
        if args.get("arguments"):
            rendered += " " + json.dumps(args["arguments"], ensure_ascii=False)
        rendered = rendered.replace("\n", " ")
        if limit is not None and len(rendered) > limit:
            rendered = f"{rendered[: limit - 3]}…"
        return f"{tool} — {rendered}"
    for key in _SUBJECT_KEYS:
        value = args.get(key)
        if value in (None, "", [], {}):
            continue
        rendered = _render_value(value).strip().replace("\n", " ")
        if not rendered:
            continue
        if limit is not None and len(rendered) > limit:
            rendered = f"{rendered[: limit - 3]}…"
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
        command_line = args.get("command") or args.get("shell_command")
        refusal = (
            queen_command_line_refusal(command_line)
            if isinstance(command_line, str) and command_line.strip()
            else queen_shell_refusal(argv)
        )
        if refusal is not None:
            # Should never reach a card (the verb refuses it outright), but if a
            # path ever proposes one, say the engine's own reason, not a guess.
            return refusal
        return "runs a command on this machine"
    if tool == "propose_schedule" and args.get("caste") == "script":
        # ES (lane F3): one click grants recurring host shell as the operator;
        # the card has to say so, with the exact command and interval.
        command = _render_value(args.get("instructions") or "").strip()
        return (
            f"runs `{command}` every {args.get('every')} as you on this host, "
            "unattended, until disabled"
        )
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
    if tool == "deny_review":
        return None  # refusing applies nothing
    reason = str(args.get("reason") or "")
    if tool == "approve_review" and reason and not reason.startswith("land "):
        # L9: a worker gate, not a landing (landing reasons read 'land "…"')
        return "lets the paused run go past this gate and do what it names, once"
    if tool in _LANDING_TOOLS:
        return "applies a patch to a real branch — landing IS the commit"
    if tool == "set_operator_policy" and args.get("verdict") in ("deny", "require_approval"):
        # L12: a deny or ask narrows; the _POLICY_TOOLS line would be a lie
        return "adds a standing rule that makes matching actions ask or be refused"
    if tool == "revoke_policy_rule":
        # v109-F9: the one policy verb that NARROWS — the _POLICY_TOOLS line
        # ("widens…") would be a lie on this card.
        return (
            "removes a standing grant — the next matching action cards again "
            "instead of auto-running"
        )
    if tool in _POLICY_TOOLS:
        return "widens what future runs may do without asking you again"
    if tool in _DESTRUCTIVE_TOOLS:
        return "removes state that cannot be restored from here"
    if tool == "call_mcp_tool":
        return (
            f"runs {args.get('server_id')}:{args.get('tool')} as you with these arguments; "
            "its reply comes into this conversation"
        )
    if tool == "register_mcp_server":
        return "starts an MCP server whose tools can act as you"
    if tool in _NETWORK_TOOLS:
        host = _host_of(args)
        target = f" from {host}" if host else ""
        return f"pulls remote content{target} into this conversation"
    return None


SCRIPT_SENTENCE = "runs as you, on this host, unattended, until disabled"


def schedule_facts(args: dict[str, Any], store: RunStore | None = None) -> dict[str, Any] | None:
    """P4-2 (ADR 0059 D6, F3 lesson 1): what a script, chained or replacing
    schedule will really do — the command, the interval, the env names the
    script really gets (``scheduler.script_env``), the next three fire times
    (``scheduler.next_fire_times``), the chain it reads from, what it
    replaces. None for any other schedule. With no ``store`` (a resolved
    card) the chain is the one named, not walked."""
    from ..scheduler import next_fire_times, parse_interval, script_env

    caste = args.get("caste")
    existing = None if store is None else store.get_schedule(str(args.get("name") or ""))
    if caste != "script" and not args.get("chain") and existing is None:
        return None
    facts: dict[str, Any] = {"name": str(args.get("name") or ""), "caste": caste}
    if caste == "script":
        facts["command"] = str(args.get("instructions") or "")
        facts["env"] = sorted(script_env())
        facts["runs"] = SCRIPT_SENTENCE
    facts["every"] = str(args.get("every") or "")
    try:
        start = None if args.get("start_at") is None else str(args["start_at"])
        facts["next"] = next_fire_times(
            start, parse_interval(facts["every"]), once=bool(args.get("once"))
        )
    except ValueError as exc:
        facts["error"] = str(exc)  # the confirm refuses the same way
    chain: list[str] = []
    current = args.get("chain")
    while current and str(current) not in chain and len(chain) < 4:
        chain.append(str(current))
        source = None if store is None else store.get_schedule(str(current))
        current = None if source is None else source.chain
    if chain:
        facts["chain"] = chain  # this one reads chain[0]'s output, which reads chain[1]'s…
    if existing is not None:
        facts["replaces"] = f"your {existing.worker_kind} schedule {existing.name!r}"
    return facts


def recorded_card(
    tool: str,
    args: dict[str, Any],
    high: str | None,
    store: RunStore,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """What the server records on a card when it is proposed (P4-2): its
    HIGH tier, a dispatch's manifest, and for a schedule the facts the card
    shows."""
    record: dict[str, Any] = {} if high is None else {"tier": "high", "high": high}
    if manifest is not None:
        record["manifest"] = manifest
    if tool == "propose_schedule":
        facts = schedule_facts(args, store)
        if facts is not None:
            record["schedule"] = facts
    return record or None


def fold_subject(tool: str, args: Any, decided_by: str | None) -> str | None:
    """P4-2 (ADR 0059 D6): when two cards are one question — ``(tool, scope,
    matcher subject)`` plus the rule that asked, so a folded card's why-line
    is true of every item. None: this call never folds (when in doubt).

    - ``read_url``: the origin (scheme, host, port). Several pages of one host
      in a round are one card; another host, port or scheme is another card.
      A URL carrying credentials never folds.
    - ``search_web`` (a taint ask): the search lane itself — every query of a
      round asks the same thing ("may this chat send text out?").
    - everything else: never (a command, a path, a dispatch is its own
      question; identical calls are already deduplicated)."""
    safe: dict[str, Any] = args if isinstance(args, dict) else {}
    if tool == "search_web":
        return json.dumps([tool, "network.search", "ddgs", decided_by])
    if tool != "read_url":
        return None
    try:
        parsed = urllib.parse.urlsplit(str(safe.get("url") or ""))
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname or "@" in parsed.netloc:
        return None
    port = port or (443 if parsed.scheme == "https" else 80)
    origin = f"{parsed.scheme}://{parsed.hostname}:{port}"
    return json.dumps([tool, "network.fetch", origin, decided_by])


def unreadable_under_deny(recorded: dict[str, Any] | None) -> bool:
    """L13, D12: the card's recorded decision says skep cannot read the line
    and a deny or named ask exists — such a card holds once."""
    decision = (recorded or {}).get("decision") or {}
    return decision.get("reason") == "shell.require_approval.unreadable_under_deny"


def card_summary(
    tool: str,
    args: Any,
    description: str = "",
    *,
    taint: ChatTaint | None = None,
    taint_line: str | None = None,
    store: RunStore | None = None,
    recorded: dict[str, Any] | None = None,
    live_tier: bool = True,
) -> dict[str, Any]:
    """The human-facing three lines for one proposed action.

    ``risk`` is omitted entirely when there is nothing notable, so the card
    stays quiet about quiet things. P4-1 (ADR 0059 D5): in a tainted chat
    (``taint``) the card says why in plain words (``taint_line``), offers
    only the keeps that chat may store. P4-2 (D6): a HIGH action
    (``high_tier``, or HIGH as ``recorded`` when it was proposed) is
    ``tier: high`` with ``high`` saying why, and offers ``once`` only. A
    resolved card (``live_tier`` False) asks no server or schedule again.
    """
    safe_args: dict[str, Any] = args if isinstance(args, dict) else {}
    high = (recorded or {}).get("high")
    if high is None and live_tier:
        high = high_tier(tool, safe_args, taint=taint, store=store)
    elif high is None and _doubly_tainted(taint):
        high = _TRIFECTA
    # P4-2: a folded card is every call it answers — the confirm acts on
    # exactly these (the server's record), never on one more.
    folded = [safe_args] + [item.get("args") or {} for item in (recorded or {}).get("items") or []]
    summary: dict[str, Any] = {"headline": headline(tool, safe_args)}
    why = purpose(description)
    if why is not None:
        summary["purpose"] = why
    held = None
    if tool == "approve_review" and store is not None:
        # L9: landing or gate is the approval's, never the model's ``reason`` arg
        held = store.get_approval(str(safe_args.get("review_id") or ""))
    danger = risk(tool, safe_args if held is None else {**safe_args, "reason": held.reason})
    if unreadable_under_deny(recorded):
        # L13: the deny this line may get around
        danger = str(((recorded or {}).get("decision") or {}).get("detail"))
    push = (recorded or {}).get("push")
    if tool == "open_pr" and push:  # L30: which remote and branch, as recorded
        danger = f"{push} — {danger}"
    if danger is not None:
        summary["risk"] = danger
    if tool in _POLICY_TOOLS:
        # v114-F3: a standing change, not a one-off — clients that pre-confirm
        # (`skep chat --yes`) must leave it for a named confirm.
        summary["policy"] = True
    from .actions import REMEMBERABLE_TOOLS, keep_covers, keep_options

    if tool in REMEMBERABLE_TOOLS:
        # P3-4 (ADR 0059 D4): the keep choices the server will honour — the
        # card never offers a lifetime the confirm would refuse (B32).
        offered = [keep_options(tool, item, taint, high=high is not None) for item in folded]
        summary["keep"] = [keep for keep in offered[0] if all(keep in o for o in offered)]
        if unreadable_under_deny(recorded):
            summary["keep"] = ["once"]  # L13, D12: approve-once, never keepable
        covers = [found for item in folded if (found := keep_covers(tool, item)) is not None]
        if covers and summary["keep"] != ["once"]:
            # what "session"/"always" would allow — for every item of a fold
            summary["keep_covers"] = "; ".join(dict.fromkeys(covers))
    manifest = (recorded or {}).get("manifest")
    if manifest is not None:
        # P4-2 (D6): what the run gets, as compiled when the card was shown —
        # approving it answers the listed asks for that run.
        summary["manifest"] = manifest
    if tool == "propose_schedule":
        # a pending card says what confirming NOW does; a resolved one what
        # the server recorded when it was proposed
        facts = schedule_facts(safe_args, store) if live_tier else (recorded or {}).get("schedule")
        if facts is not None:
            summary["schedule"] = facts
    if len(folded) > 1:
        summary["items"] = [headline(tool, item, limit=None) for item in folded]
        summary["headline"] = f"{summary['headline']} (+{len(folded) - 1} more)"
    if taint_line:
        summary["taint"] = taint_line
    if high is not None:
        summary["tier"] = "high"
        summary["high"] = high
    return summary
