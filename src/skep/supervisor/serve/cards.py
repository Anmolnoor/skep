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
    escalates,
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


# phase-01 1.2 (I6, I9): the boundary a card guards, named from what approving
# it lets cross. Every card kind is listed (test_every_card_kind_has_its_
# boundary_label fails on a new one): an unmapped kind gets no label, never a
# guessed one. Run-starting cards, worker gates and script schedules are
# decided per card in ``boundary``.
BOUNDARIES = (
    "patch-landing",
    "network-egress",
    "outbound-content",
    "host-shell",
    "policy-change",
    "tainted-dispatch",
    "in-sandbox-locked",
)
_RUN_TOOLS = frozenset(
    {"dispatch_run", "quick_edit", "batch_dispatch", "start_research", "forge_tool"}
    | {"run_code", "resume_run"}
)
_GATE_TOOLS = frozenset({"approve_review", "deny_review"})
_BOUNDARY_GROUPS: dict[str, tuple[str, ...]] = {
    "patch-landing": (
        "land_run", "approve_review", "deny_review", "create_branch", "delete_branch",
        "merge_branch",
    ),
    "network-egress": ("read_url", "refresh_repo", "register_repo"),
    "outbound-content": (
        "push_branch", "push_baseline", "open_pr", "merge_pr", "close_pr", "call_mcp_tool",
        "search_web", "discord_delete_message", "discord_timeout_member", "add_provider",
        "use_provider", "set_assistant_model", "set_tts_provider",
    ),
    # acts on this host outside any sandbox: a command, a host file, a server
    "host-shell": (
        "run_shell", "start_process", "stop_process", "diagnose_run", "read_file",
        "search_files", "register_mcp_server", "setup_browser", "promote_tool",
        "promote_skill_pack", "sync_notes", "workon",
    ),
    "in-sandbox-locked": tuple(sorted(_RUN_TOOLS)),
    # skep's own standing state: policy, projects, schedules, memory, skills, settings
    "policy-change": (
        "allow_command_review", "allow_env_bootstrap", "allow_fetch_domain", "allow_mcp_tool",
        "allow_shell_command", "apply_policy_preset", "approve_policy_baseline",
        "attach_policy_group", "copy_project_policy", "delete_policy_group",
        "detach_policy_group", "revoke_policy_rule", "set_operator_policy", "set_policy",
        "set_policy_group", "set_role", "setup_project", "unregister_repo", "remove_project",
        "set_project_phase", "propose_schedule", "delete_schedule", "set_schedule_enabled",
        "run_schedule_now", "delete_note", "delete_task", "set_task_due", "forget_memory",
        "approve_memory_proposal", "reject_memory_proposal", "remember", "add_note",
        "add_task", "create_skill", "patch_skill", "delete_skill", "capture_skill",
        "set_skill_observer",
        "suspend_skill_pack", "suspend_tool", "unregister_mcp_server", "remove_provider",
        "set_persona", "set_personality", "set_run_completion_notify", "delegate_analysis",
    ),
}  # fmt: skip
_BOUNDARY = {tool: label for label, tools in _BOUNDARY_GROUPS.items() for tool in tools}
UNLOCK_REMEDIATION = "skep project setup {repo} --execution-mode sandbox --unlocked"


def _unlocked(store: RunStore | None, repo: Any) -> bool:
    """Is ``repo``'s project unlocked? False with no store or repo to say."""
    if store is None or not repo:
        return False
    from pathlib import Path

    from ..policy_resolver import project_bound_to_repo

    project = project_bound_to_repo(store, Path(str(repo)))
    return project is not None and project.policy.get("unlocked") is True


def boundary(
    tool: str,
    args: dict[str, Any],
    *,
    taint: ChatTaint | None = None,
    store: RunStore | None = None,
    manifest: dict[str, Any] | None = None,
) -> tuple[str | None, str | None]:
    """phase-01 1.2: ``(label, remediation)`` for one card. A run-starting
    card from a tainted chat is ``tainted-dispatch``; one whose run is on the
    host is ``host-shell``; one that asks to reach hosts is ``network-egress``;
    else its wall is the sandbox: ``in-sandbox-locked`` while the project is
    not unlocked. A worker gate is labelled by what the worker asked (its
    approval's action); only a ``shell.run`` gate in a locked sandbox carries
    the unlock line — the one ask unlocking answers (I8: never offer a no-op;
    it does not dry-run decide(), so a floor-denied line still shows it).
    An unlocked project's sandbox has no label in the fixed set: None."""
    if tool in _RUN_TOOLS:
        runs = [manifest, *(manifest.get("members") or [])] if manifest else [args]
        if (taint is not None and taint.any) or any(run.get("taint") for run in runs):
            return "tainted-dispatch", None
        resumed = (
            store.get_run(str(args.get("task_id") or ""))
            if tool == "resume_run" and store is not None
            else None
        )
        if resumed is not None:
            runs = [{"execution_mode": resumed.execution_mode, "repo": resumed.repo}]
        modes = {run.get("execution_mode") for run in runs} - {None}
        if modes - {"sandbox"}:
            return "host-shell", None
        asks = ("network_ask", "network_added") if manifest else ("network",)
        if any(run.get(key) for run in runs for key in asks) or any(map(_wildcard_network, runs)):
            return "network-egress", None  # R1 G5: it asks to reach hosts
        repos = [run.get("repo") for run in runs if run.get("repo")]
        if repos and all(_unlocked(store, repo) for repo in repos):
            return None, None  # R1 G4: an override carded it, not a locked sandbox
        return "in-sandbox-locked", None
    if tool in _GATE_TOOLS:
        gate = store.get_approval(str(args.get("review_id") or "")) if store is not None else None
        if gate is None:  # risk()'s rule: a reason not 'land …' is a worker gate
            reason = str(args.get("reason") or "")
            gated = reason and not reason.startswith("land ")
            return ("host-shell" if gated else _BOUNDARY[tool]), None
        if gate.action == "apply_patch":
            return "patch-landing", None
        if gate.action == "promote_skill":
            return "policy-change", None
        if gate.action.startswith("network."):
            return "network-egress", None
        run = store.get_run(gate.task_id) if store is not None else None
        if run is None or run.execution_mode != "sandbox":
            return "host-shell", None
        if _unlocked(store, run.repo):
            return None, None  # R1 G4: unlocked already declined this ask
        remedy = UNLOCK_REMEDIATION.format(repo=shlex.quote(str(run.repo)))
        return "in-sandbox-locked", remedy if gate.action == "shell.run" else None
    if tool == "propose_schedule" and args.get("caste") == "script":
        return "host-shell", None
    if tool == "acp_permission":  # ADR 0058: the ask is inside the run's sandbox
        return ("network-egress" if args.get("scope") == "network" else "in-sandbox-locked"), None
    return _BOUNDARY.get(tool), None


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
    from .llm import assistant_llm

    # Phase 05 R1: the protocol the assistant actually speaks (the active
    # profile), not the llm_protocol mirror.
    return store is None or args["protocol"] != assistant_llm(store)[2]


def _to_codex_login(tool: str, args: dict[str, Any], store: RunStore | None) -> bool:
    """v118: does this provider change point the Queen at chatgpt.com?"""
    from ..codex_login import uses_codex_login
    from ..provider_presets import PROVIDER_PRESETS

    url = str(args.get("base_url") or "")
    preset = PROVIDER_PRESETS.get(str(args.get("preset") or ""))
    if tool == "add_provider" and not url and preset is not None:
        url = preset.base_url or ""
    if tool == "use_provider" and store is not None:
        profile = store.get_provider_profile(str(args.get("provider_id") or ""))
        url = profile.base_url if profile is not None else ""
    return uses_codex_login(url)


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
            return (
                f"a script schedule runs a command as you, on this host, unattended, {SCRIPT_WALL}"
            )
        if safe.get("chain"):
            return "a chained schedule feeds another schedule's output into this one"
        existing = None if store is None else store.get_schedule(str(safe.get("name") or ""))
        if existing is not None and existing.worker_kind == "script":
            return f"it replaces your script schedule {existing.name!r}"
        return None
    if tool == "call_mcp_tool":
        from ..mcp_client import mcp_call_is_read, mcp_call_sends_mail

        server, name = str(safe.get("server_id") or ""), str(safe.get("tool") or "")
        if store is not None and mcp_call_sends_mail(store, server, name):
            return "it sends mail as you: every send needs your verdict, once (I6)"
        if store is not None and (
            mcp_call_is_read(store, server, name) or _named_by_operator(store, server, name)
        ):
            return None
        return "an MCP tool that acts (not a read), and you have not allowed it by name"
    if tool == "register_mcp_server" or tool == "setup_browser":
        from ..mcp_sandbox import HOST_HIGH, registration_high  # ADR 0069 1.5

        return HOST_HIGH if tool == "setup_browser" else registration_high(safe)
    if tool == "promote_tool":
        return "it installs a forged tool: its code runs on this host"
    if tool == "promote_skill_pack":
        return "it activates a skill pack's scripts"
    if tool in _HIGH_POLICY_TOOLS:
        return "it changes standing policy"
    if tool in ("add_provider", "use_provider", "set_assistant_model") and _to_codex_login(
        tool, safe, store
    ):
        # v118 (ADR 0063, I6): say what this opt-in spends and where it goes
        return (
            "it uses your ChatGPT plan login (Codex): every Queen prompt leaves "
            "this machine for chatgpt.com"
        )
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
    if tool == "setup_project" and store is not None:
        # v133-F28 (J9, I1, I6): the lane that lands with no human, turned on
        # by this write — asked for or derived from a phase default
        from .tools import setup_card_facts

        landing = (setup_card_facts(store, safe) or {}).get("auto_landing") or {}
        if landing.get("new") is True and landing.get("old") is not True:
            return (
                f"it turns auto-landing on: G10-confirmed patches land on {landing['branch']} "
                "with no human approval"
            )
    if tool == "setup_project" and (
        safe.get("unlocked") is True
        or (isinstance(overrides, dict) and overrides.get("unlocked") is True)
    ):
        # ES-F142 (ADR 0061): a whole project's worker asks stop asking
        return (
            "it turns unlocked mode on: the project's sandboxed workers stop asking "
            "about commands no rule names (hosts no rule names stay refused or asked)"
        )
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
    if store is not None and (label := landing_label(store, tool, safe)) is not None:
        # v133-F12 (J1, I1, I2): a landing G10 did not confirm stays the
        # human's to make — one card, HIGH, saying what G10 found
        from .actions import g10_line

        failed = [store.reverification_for(task_id) for task_id in _landed_runs(store, tool, safe)]
        return f"{label}: " + "; ".join(g10_line(r) for r in failed if r and not r.confirmed)
    if tool in ("run_shell", "start_process"):
        from ..secret_floor import UNRESOLVED_READ, shell_read_target_unresolved

        cwd = safe.get("cwd")
        command = str(safe.get("command") or "")
        if command and shell_read_target_unresolved(command, cwd=None if cwd is None else str(cwd)):
            return UNRESOLVED_READ  # L3 (D44): the floor could not read it
    return None


def _landed_runs(store: RunStore, tool: str, args: dict[str, Any]) -> list[str]:
    from .console import card_runs

    return card_runs(store, tool, args) if tool in _LANDING_TOOLS else []


def landing_label(store: RunStore, tool: str, args: dict[str, Any]) -> str | None:
    """v133-F12 (J1): a land_run / approve_review (landing) card's action
    label when G10 did not confirm a run it lands — None otherwise."""
    from .actions import g10_landing_label

    for task_id in _landed_runs(store, tool, args):
        label = g10_landing_label(store.reverification_for(task_id))
        if label is not None:
            return label
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
    if tool == "acp_permission":  # ADR 0058: what the run's agent asked, in skep's words
        asked = " ".join(str(args.get(k) or "") for k in ("scope", "action", "value")).strip()
        asked = asked or str(args.get("title") or "")
        return f"{tool} — {asked}" if asked else tool
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


def _shell_risk(args: dict[str, Any]) -> str:
    argv = _argv_of(args)
    if not argv:
        return "runs a command on this machine"
    if is_outbound_content_prefix(argv):
        return (
            "sends content outward — public and permanent, and never grantable, "
            "so this cards every time (ADR 0044)"
        )
    if escalates(argv):
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


# v133-F29 (F-I6): the Queen's own host shell — no sandbox, the operator's login
_HOST_SHELL_TOOLS = frozenset({"run_shell", "start_process"})
HOST_SHELL = "on this machine as your account, outside the sandbox"


def _credential_dirs() -> tuple[str, ...]:
    """The home-relative paths where the secret floor's credentials live
    (``HOME_SECRETS``): each listed path, and the folder of a listed file
    (``.claude/.credentials.json`` → ``.claude``). Longest first."""
    from ..secret_floor import HOME_SECRETS

    found = set(HOME_SECRETS)
    for entry in HOME_SECRETS:
        folder, _, leaf = entry.rpartition("/")
        if folder and "." in leaf[1:]:
            found.add(folder)
    return tuple(sorted(found, key=len, reverse=True))


def _credential_dir_named(command: str) -> str | None:
    homes = r"(?:~|\$HOME|\$\{HOME\}|/(?:home|Users)/[^/\s'\"]+)"
    names = "|".join(re.escape(name) for name in _credential_dirs())
    found = re.search(rf"{homes}/({names})(?=[/\s'\"]|$)", command)
    return None if found is None else f"~/{found[1]}"


def host_shell_risk(args: dict[str, Any], line: str) -> str:
    """v133-F29 (F-I6, I8, I12): a run_shell / start_process card says it runs on
    this machine as your account, outside the sandbox; one that starts an
    agent CLI or names a credential directory says that too, in plain words."""
    from ..engines import CODING_ENGINES
    from ..shell_prefixes import command_line_segments, program_name

    generic = line == "runs a command on this machine"
    parts = [f"runs {HOST_SHELL}" if generic else f"{line} — it runs {HOST_SHELL}"]
    command = str(args.get("command") or "")
    agents = {engine.binary for engine in CODING_ENGINES.values() if engine.binary}
    started = [
        program_name(segment[0])
        for segment in command_line_segments(command) or []
        if segment and program_name(segment[0]) in agents
    ]
    if started:
        parts.append(
            f"it starts the agent CLI `{started[0]}` with your own login, not a sandboxed run"
        )
    folder = _credential_dir_named(command)
    if folder is not None:
        parts.append(f"it names {folder}, a credential directory")
    return "; ".join(parts)


def risk(tool: str, args: dict[str, Any]) -> str | None:
    """The specific reason this needs a human, or None when there is none.

    Never invents a line: a benign read renders no risk at all.
    """
    if tool in _HOST_SHELL_TOOLS:
        # v133-F29 (F-I6, I12): a host shell says where it runs and as whom
        return host_shell_risk(args, _shell_risk(args))
    if tool in _SHELL_TOOLS:
        return _shell_risk(args)
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
    if tool == "acp_permission":
        return (
            "lets this run's ACP agent do it once, inside its sandbox — the patch still "
            "lands only through your approval"
        )
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


# Phase 09 R1 (I8): what 9D's wall (wall.py) lets a script schedule do —
# reads beyond the system and its toolchains: only the files its command
# names (scheduler.script_inputs); egress: only its `network` hosts.
SCRIPT_WALL = (
    "inside the wall: besides the system and its toolchains it reads only the files "
    "its command names (the live files, read-only), and it reaches only its listed hosts "
    "(none unless listed)"
)
SCRIPT_SENTENCE = f"runs as you, on this host, unattended, until disabled, {SCRIPT_WALL}"


def schedule_facts(
    args: dict[str, Any],
    store: RunStore | None = None,
    role_line: str | None = None,
    role: str | None = None,
) -> dict[str, Any] | None:
    """P4-2 (ADR 0059 D6, F3 lesson 1): what a script, chained, replacing or
    (v123-F1) role-bound schedule will really do — the command, the interval,
    the env names the script really gets (``scheduler.script_env``), the next
    three fire times (``scheduler.next_fire_times``), the chain it reads
    from, what it replaces (and, v123 review, the role it had when ``role``
    differs), the role it runs as (``role_line``). None for any
    other schedule. With no ``store`` (a resolved card) the chain is the one
    named, not walked."""
    from ..scheduler import next_fire_times, parse_interval, script_env
    from ..spawner import proxy_env

    caste = args.get("caste")
    existing = None if store is None else store.get_schedule(str(args.get("name") or ""))
    if caste != "script" and not args.get("chain") and existing is None and role_line is None:
        return None
    facts: dict[str, Any] = {"name": str(args.get("name") or ""), "caste": caste}
    if caste == "script":
        facts["command"] = str(args.get("instructions") or "")
        facts["hosts"] = [str(host) for host in args.get("network") or []]
        # a schedule with hosts reaches them through the wall's proxy vars
        facts["env"] = sorted({*script_env(), *(proxy_env(0) if facts["hosts"] else ())})
        facts["runs"] = SCRIPT_SENTENCE
    if role_line is not None:
        facts["runs"] = role_line
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
        if existing.role != role:  # never a silent role change (I8)
            facts["replaces"] += f" (was {_role_words(existing.role)} → {_role_words(role)})"
    return facts


def _role_words(role: str | None) -> str:
    return "no role" if role is None else f"role {role}"


def pr_review_pin_line(args: dict[str, Any], manifest: dict[str, Any] | None) -> str | None:
    """v133-F27 (A5, I2, I9): a dispatch that reviews a PR head on a project
    that pins no verify_command — G10 re-verifies a PR head only with the pin
    (reverify.py, v88-F4), so the card says so before the run, with the line
    that pins one. The review is still allowed. None otherwise."""
    from ..github import PR_HEAD_REF

    found = PR_HEAD_REF.fullmatch(str(args.get("ref") or ""))
    if found is None or manifest is None or "error" in manifest or manifest.get("verify_command"):
        return None
    repo = shlex.quote(str(manifest.get("repo") or args.get("repo") or "<repo>"))
    return (
        f"PR #{found[1]}: G10 cannot re-verify this review — the project pins no "
        f'verify_command; pin one first: `skep project setup {repo} --verify-command "<command>"` '
        "(the review can still run, unverified)"
    )


def setup_line(setup: dict[str, Any]) -> str | None:
    """v133-F28 (J9, F-I1): the setup card's line — the pin before and after
    (syntax-only marked) and any auto-landing change with its branch."""
    if setup.get("error"):
        return f"its effect could not be previewed: {setup['error']}"
    parts: list[str] = []
    pin = setup.get("verify_command") or {}
    if pin.get("old") != pin.get("new"):
        old, new = (f"`{pin[key]}`" if pin.get(key) else "none" for key in ("old", "new"))
        parts.append(f"verify_command: {old} → {new}")
    if pin.get("syntax_only"):
        parts.append(f"{pin['syntax_only']} — it cannot qualify a patch for auto-landing")
    landing = setup.get("auto_landing")
    if landing and landing.get("old") != landing.get("new"):
        asked = landing.get("asked")
        if landing.get("new"):  # unasked, only a phase default turns it on
            how = "asked for" if asked else "from the phase default"
            parts.append(f"auto-landing: off → ON ({how}), lands on {landing['branch']}")
        else:
            parts.append(f"auto-landing: on → off ({'asked for' if asked else 'derived'})")
    elif landing and landing.get("old_branch") != landing.get("branch"):
        # R3 (U3): a destination-only change, in plain words
        how = "asked for" if landing.get("asked") else "derived"
        parts.append(
            f"auto-landing stays on: lands on {landing['branch']} instead of "
            f"{landing['old_branch']} ({how})"
        )
    return "; ".join(parts) or None


def recorded_card(
    tool: str,
    args: dict[str, Any],
    high: str | None,
    store: RunStore,
    manifest: dict[str, Any] | None = None,
    schedule_role: str | None = None,
    schedule_role_name: str | None = None,
    *,
    merge: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """What the server records on a card when it is proposed (P4-2): its
    HIGH tier, a dispatch's manifest, and for a schedule the facts the card
    shows (v123-F1: with the role line, ``tools.schedule_role_line``, and
    the role it will store, ``tools.proposed_schedule_role``)."""
    record: dict[str, Any] = {} if high is None else {"tier": "high", "high": high}
    if manifest is not None:
        record["manifest"] = manifest
    if tool == "dispatch_run" and (pin_line := pr_review_pin_line(args, manifest)) is not None:
        record["pin_warning"] = pin_line
    if tool == "setup_project":  # v133-F28: the pin and auto-landing, as previewed
        from .tools import setup_card_facts

        setup = setup_card_facts(store, args)
        if setup is not None:
            record["setup"] = setup
    if schedule_role is not None:
        record["schedule_role"] = schedule_role
    if tool == "propose_schedule":
        record["schedule_role_name"] = schedule_role_name
        facts = schedule_facts(args, store, schedule_role, schedule_role_name)
        if facts is not None:
            record["schedule"] = facts
    if tool == "promote_tool":
        record["forge"] = promotion_card(store, args)
    if tool == "call_mcp_tool":  # ADR 0069 1.6: the supervisor's, never the model's
        from ..mcp_sandbox import card_incarnation

        record["incarnation"] = card_incarnation(store, args)
    if tool in ("register_mcp_server", "setup_browser"):  # ADR 0069 1.3, built with no launch
        from ..mcp_sandbox import card_grant

        record["mcp_grant"] = card_grant(tool, args, store)
    if tool == "capture_skill":
        record["capture"] = capture_card(store, args)
    if tool == "merge_pr":  # R3 (S6): the pin the proposal already validated
        record["merge"] = merge_pin(store, args) if merge is None else merge
    if tool in ("land_run", "approve_review", "open_pr"):
        # v133-F11/F12 (J1, J3, I7): the runs it lands or publishes, each
        # with its G10 facts, kept on the row; the confirm is bound to the
        # G10 record each was drawn on (actions.shown_g10_moved)
        from .actions import g10_fingerprint
        from .console import run_evidence

        runs = run_evidence(store, tool, args)
        # R3 (U2): kept even when empty — the card shows what it recorded
        record["evidence"] = {"runs": runs}
        if runs:
            record["g10_bound"] = {
                run["task_id"]: g10_fingerprint(store.reverification_for(run["task_id"]))
                for run in runs
            }
        label = landing_label(store, tool, args)
        if label is not None:
            record["action_label"] = label
    if tool == "open_pr" and args.get("branch"):
        # v133-F15 (J5, I7): the push is bound to the head this card shows
        from ..apply import resolve_commit
        from .console import _branch_repo

        repo = _branch_repo(store, args)
        head = None if repo is None else resolve_commit(repo, f"refs/heads/{args['branch']}")
        if head is not None:
            record["head"] = head
    return record or None


def _merge_target(store: RunStore, args: dict[str, Any]) -> Any:
    """The canonical PR a merge_pr call names (R1 D1: never the typed
    text) — a ``PrId`` — or ``{"error": why}``."""
    from pathlib import Path

    from fastapi import HTTPException

    from ..github import PrSelectorError, resolve_pr
    from .registry import resolve_repo_arg

    try:
        root = Path(store.db_path).parent.parent / "repos"  # registry.repos_root
        repo = resolve_repo_arg(str(args.get("repo") or ""), root, store)
        return resolve_pr(repo, args.get("pr", ""))
    except HTTPException as exc:
        return {"error": str(exc.detail)}
    except PrSelectorError as exc:
        return {"error": str(exc)}


def merge_target(store: RunStore, args: dict[str, Any]) -> tuple[str, int] | None:
    """R3 (S7): the (repo, PR) a merge_pr card names, or None — how a card
    that recorded no pin is matched to a new proposal for the same PR."""
    pid = _merge_target(store, args)
    return None if isinstance(pid, dict) else (pid.repo, pid.number)


def merge_pin(store: RunStore, args: dict[str, Any]) -> dict[str, Any]:
    """Phase 09 R1 (D3, I1, I8): what a merge_pr card pins, read ONCE, when
    it is proposed — the newest G10 verdict on the PR's canonical identity
    (origin owner/name, number). Only a clean, confirmed verdict pins:
    ``{repo, number, base_branch, base_commit, head_commit, merge_base,
    task_id}``. Anything else is ``{"error": why}`` and nothing merges.
    R3 (S6): a proposal calls this once and hands that pin to the
    propose-time check, the de-duplication and the saved card."""
    from ..github import pr_ref

    pid = _merge_target(store, args)
    if isinstance(pid, dict):
        return pid
    verdict = store.pr_head_verdict(pid.repo, pid.number)
    again = (
        f"refresh_repo pr={pid.number}, dispatch_run ref={pr_ref(pid.number)}, "
        "then propose merge_pr again"
    )
    if verdict is None or not verdict.pr:
        return {"error": f"no G10 verdict covers {pid.repo}#{pid.number}: {again}"}
    problem = _unclean(verdict)
    if problem is not None:
        return {
            "error": f"G10 did not cleanly confirm {pid.repo}#{pid.number} at head "
            f"{verdict.head_commit} ({problem}; run {verdict.task_id}) — an unverified head "
            f"never merges: fix the PR, then {again}"
        }
    keys = ("repo", "number", "base_branch", "base_commit", "head_commit", "merge_base")
    return {**{key: verdict.pr[key] for key in keys}, "task_id": verdict.task_id}


def _unclean(verdict: Any) -> str | None:
    """Why a PR verdict is not G10 passed + V1 clean, or None (D3)."""
    if verdict.tamper_flags:
        return "V1: " + ", ".join(verdict.tamper_flags)
    if verdict.outcome != "passed":
        return f"G10 {verdict.outcome}"
    if not verdict.confirmed:
        return "G10 passed, but not confirmed: the review worker itself did not pass"
    if (verdict.test_counts or {}).get("status") == "unparseable":
        return "V1 could not read the test counts"
    return None


def merge_line(pin: dict[str, Any]) -> str:
    if "error" in pin:
        return f"REFUSED — {pin['error']}"
    branch = pin["base_branch"]
    return (
        f"merges only {pin['repo']}#{pin['number']} at head {pin['head_commit'][:12]} — "
        f"G10 confirmed that exact tree and V1 found nothing (run {pin['task_id'][:13]}) — "
        f"onto {branch} {pin['base_commit'][:12]}; a push to the PR or to {branch} after "
        "this card refuses the merge (one landing while GitHub merges is reported)"
    )


def capture_card(store: RunStore, args: dict[str, Any]) -> dict[str, Any]:
    """Phase 08 (8.2, I8): the SKILL.md a capture_skill card shows, drafted
    when it is proposed — the confirm saves exactly this or nothing."""
    from pathlib import Path

    from ..skill_cmds import SkillError, draft_capture, taint_line

    try:
        candidate, text = draft_capture(
            store, Path(store.db_path).parent / "audit", str(args.get("source") or ""),
            name=str(args.get("name") or ""),
        )  # fmt: skip
    except SkillError as exc:
        return {"error": str(exc)}
    return {
        "skill_md": text,
        "source": candidate.source_task_ids[0],
        "taint_line": taint_line(candidate),
    }


def promotion_card(store: RunStore, args: dict[str, Any]) -> dict[str, Any]:
    """Phase 07 (7.2, I6): what a promote_tool card binds, recorded when it
    is proposed — ``(source_sha256, lock_sha256, approved_hosts)``, the
    locked wheel pins and the full source diff. The confirm installs only
    this triple (``forge.require_card_binding``); a tool that cannot be
    bound records why, and its confirm refuses the same way."""
    from pathlib import Path

    from .. import forge
    from .actions import applied_branch_for

    plugin_id = str(args.get("plugin_id") or "")
    record = forge.load_plugins(store).get(plugin_id)
    if record is None:
        return {"error": f"no forged plugin {plugin_id!r} — list_plugins shows them"}
    branch = None if record.provenance == "seed" else applied_branch_for(store, record.task_id)
    try:
        return forge.promotion_view(Path(store.db_path).parent.parent, record, branch)
    except ValueError as exc:
        return {"error": str(exc)}


def _promotion_line(forged: dict[str, Any]) -> str:
    if "error" in forged:
        return f"it cannot be installed as shown: {forged['error']}"
    hosts = ", ".join(forged.get("approved_hosts") or ()) or "none (deny-all)"
    lock = (
        f"lock sha256 {forged['lock_sha256'][:12]}… ({len(forged.get('locked') or ())} "
        "hash-pinned wheels)"
        if forged.get("lock_sha256")
        else "stdlib only, no lock"
    )
    return f"source sha256 {str(forged.get('source_sha256'))[:12]}…, {lock}, network: {hosts}"


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


def activates_memory(tool: str, recorded: dict[str, Any] | None) -> bool:
    """v128-F10 (operator decision): a `remember` the chat's taint carded —
    confirming it files AND activates the memory, one card instead of a
    second one on the same bytes. Read from the decision recorded when the
    card was proposed, so the card and the confirm agree (I8)."""
    reason = str(((recorded or {}).get("decision") or {}).get("reason") or "")
    return tool == "remember" and reason.startswith("taint.")


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
    if activates_memory(tool, recorded):
        why = "Approve files and activates this memory: it reaches future prompts."
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
    setup = (recorded or {}).get("setup")
    if tool == "setup_project" and isinstance(setup, dict):
        summary["setup"] = setup
        line = setup_line(setup)
        if line is not None:  # v133-F28: before the generic line, never instead
            danger = line if danger is None else f"{line} — {danger}"
    pin_warning = (recorded or {}).get("pin_warning")
    if pin_warning:  # v133-F27 (A5): before the review runs, not after G10
        danger = pin_warning if danger is None else f"{pin_warning} — {danger}"
    if tool == "approve_memory_proposal" and store is not None:
        # v126-F6: the approver sees what the filing chat (or note) had read
        from ..memory import taint_phrase, taint_words

        proposal = store.get_memory_proposal(str(safe_args.get("proposal_id") or ""))
        filed = taint_phrase(proposal.taint) if proposal is not None else None
        if proposal is not None:  # v128-F10: what it activates, not just an id
            content = " ".join(proposal.content.split())
            content = content if len(content) <= 160 else f"{content[:157]}…"
            summary["headline"] = f"{tool} — {content}"
        if filed:
            danger = filed if danger is None else f"{danger}; {filed}"
        if proposal is not None and proposal.taint:
            # Phase 08 (8.2): the badge names the note or task it came from
            origin = ", ".join(
                f"{s.kind} {s.source_id[:8]}"
                for s in proposal.sources
                if s.kind in ("note", "task")
            )
            badge = f"⚠ tainted — {origin or 'the chat that filed it'} carries: "
            taint_line = f"{badge}{taint_words(proposal.taint)}" + (
                f"; {taint_line}" if taint_line else ""
            )
    label, remedy = boundary(
        tool, safe_args, taint=taint, store=store, manifest=(recorded or {}).get("manifest")
    )
    if recorded and recorded.get("boundary"):  # as recorded (a mirrored gate)
        label, remedy = recorded["boundary"], recorded.get("remediation")
    if label is not None:
        summary["boundary"] = label
    if remedy is not None:
        summary["remediation"] = remedy
        unlock = f"this project is not unlocked, so it asks; to stop asks like this: {remedy}"
        danger = unlock if danger is None else f"{danger}; {unlock}"
    landing = held.task_id if held is not None else str(safe_args.get("task_id") or "")
    if label == "patch-landing" and store is not None and landing:
        # I8: the V1 verdict leads the card — a landing never hides it
        from .actions import tamper_note

        banner = tamper_note(store.reverification_for(landing))
        if banner is not None:
            summary["tamper"] = banner
            danger = banner if danger is None else f"{banner} — {danger}"
    grant = (recorded or {}).get("mcp_grant")
    if grant is None and store is not None and live_tier:
        from ..mcp_sandbox import card_grant

        grant = card_grant(tool, safe_args, store)
    if isinstance(grant, dict):  # ADR 0069 1.3: the effective grant leads the risk line
        from ..mcp_sandbox import grant_line

        summary["mcp_grant"] = grant
        danger = grant_line(grant)
    forged = (recorded or {}).get("forge")
    if tool == "promote_tool" and isinstance(forged, dict):
        # Phase 07 (7.2): the bound triple leads the risk line; the block
        # (with the full diff) rides the card for the UI to unfold.
        summary["forge"] = forged
        line = _promotion_line(forged)
        danger = line if danger is None else f"{danger} — {line}"
    pinned = (recorded or {}).get("merge")
    if tool == "merge_pr" and pinned is None:
        # R2 (N5): only an unsaved preview reads a live pin; a saved card
        # shows the pin it holds — or that it holds none (the confirm refuses).
        pinned = (
            merge_pin(store, safe_args)
            if recorded is None and store is not None and live_tier
            else {"error": "no pin was recorded when this was proposed — propose merge_pr again"}
        )
    if tool == "merge_pr" and isinstance(pinned, dict):
        summary["merge"] = pinned
        danger = f"{merge_line(pinned)} — {danger}"
    captured = (recorded or {}).get("capture")
    if tool == "capture_skill" and isinstance(captured, dict):
        name = safe_args.get("name")
        danger = (
            f"it cannot be drafted as shown: {captured['error']}"
            if "error" in captured
            else f"drafts skill {name!r} from {captured['source']} — inactive until you "
            f"approve it (skep skill approve {name})"
        )
        if captured.get("skill_md"):
            summary["skill_md"] = captured["skill_md"]
        if captured.get("taint_line"):
            badge = captured["taint_line"]
            taint_line = badge if not taint_line else f"{badge}; {taint_line}"
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
        facts = (
            schedule_facts(
                safe_args,
                store,
                (recorded or {}).get("schedule_role"),
                (recorded or {}).get("schedule_role_name"),
            )
            if live_tier
            else (recorded or {}).get("schedule")
        )
        if facts is not None:
            summary["schedule"] = facts
    if manifest is not None and manifest.get("base_ref"):
        # v133-F30 (F-I2): what the worker will start from, on the line read first
        commit = str(manifest.get("base_commit") or "")[:7]
        if commit and manifest.get("base_before_refresh"):  # R2 (Sol T8)
            commit += ", before the clone refreshes"
        summary["headline"] += f" @ {manifest['base_ref']}" + (f" ({commit})" if commit else "")
    if len(folded) > 1:
        summary["items"] = [headline(tool, item, limit=None) for item in folded]
        summary["headline"] = f"{summary['headline']} (+{len(folded) - 1} more)"
    if taint_line:
        summary["taint"] = taint_line
    if high is not None:
        summary["tier"] = "high"
        summary["high"] = high
    label = (
        landing_label(store, tool, safe_args)
        if recorded is None and store is not None and live_tier
        else (recorded or {}).get("action_label")
    )
    if label is not None:  # v133-F12 (J1): the button says what approving does
        summary["action_label"] = label
    evidence = (recorded or {}).get("evidence")
    if evidence is not None:
        # v133-F11/F16, R3 (U2): a card shows the runs it recorded when
        # proposed, pending or resolved — the confirm acts on those; a
        # recorded empty list stays empty
        if evidence.get("runs"):
            summary["runs"] = evidence["runs"]
    elif store is not None and live_tier:
        # R4 K1: the thread's card is where a landing or PR is approved — it
        # names every run it lands or publishes, each with its G10 and V1.
        # A card from before v133 R3 recorded none: it reads them live.
        from .console import run_evidence

        runs = run_evidence(store, tool, safe_args)
        if runs:
            summary["runs"] = runs
    return summary
